#!/usr/bin/env python3
"""Launcher MQTT/Home Assistant service.

Runs as a *user* service (inside the KDE session, so it can launch/focus GUI
apps). It:
  - subscribes to launch-command topics on the MQTT broker,
  - publishes Home Assistant MQTT Discovery *buttons* so each app appears in HA
    (press to launch/focus on the host),
  - shares the same Launcher logic used by the irexec CLI path.

This complements the system-level avr-tv-bridge (AVR/TV control): that one runs
as a system service with no GUI access; this one runs in the user session
because launching apps requires the Wayland/D-Bus session environment.

Config comes from the same ~/.config/avr-tv-bridge/config.env file.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time

import paho.mqtt.client as mqtt

from launcher import Launcher, APPS
from display_wake import wake_display
import media_control
from camera_popup import CameraPopup
from idle_off import IdleOff
from notify_popup import NotifyPopup
from channel_sync import ChannelSync
from chime import ChimePlayer

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("launcher_service")

BASE = "avr-tv-bridge"
DISCOVERY_PREFIX = "homeassistant"
# Command topic: avr-tv-bridge/launch/<app>/set  (payload "PRESS" or app key)
CMD_WILDCARD = f"{BASE}/launch/+/set"
AVAIL_TOPIC = f"{BASE}/launcher/availability"


def env(key: str, default: str | None = None, required: bool = False) -> str:
    val = os.environ.get(key, default)
    if required and not val:
        log.error("Missing required env var %s", key)
        sys.exit(2)
    return val  # type: ignore


class LauncherService:
    def __init__(self):
        self.mqtt_host = env("MQTT_HOST", required=True)
        self.mqtt_port = int(env("MQTT_PORT", "1883"))
        self.mqtt_user = env("MQTT_USER", required=True)
        self.mqtt_pass = env("MQTT_PASS", required=True)
        self.launcher = Launcher()
        self._stop = threading.Event()
        # How often to refresh the media status sensor (seconds).
        self.status_interval = float(env("MEDIA_STATUS_INTERVAL", "5"))

        # Camera auto-popup (detection-count topic -> mpv overlay). Generic and
        # optional: only enabled if CAM_POPUP_STREAM_URL + CAM_POPUP_TRIGGER_TOPIC
        # are configured. See config.env.example for all CAM_POPUP_* settings.
        self.cam_trigger_topic = env("CAM_POPUP_TRIGGER_TOPIC", "")
        # Trigger mode:
        #   "count"  -> payload is an integer count (e.g. frigate/<cam>/<zone>/person)
        #   "frigate_review" -> payload is a Frigate frigate/reviews JSON event;
        #                       open on an ALERT for CAM_POPUP_FRIGATE_CAMERA,
        #                       close on its end. Already zone-scoped by Frigate's
        #                       review alert required_zones config.
        self.cam_trigger_mode = env("CAM_POPUP_TRIGGER_MODE", "count")
        self.cam_frigate_camera = env("CAM_POPUP_FRIGATE_CAMERA", "")
        self.cam_frigate_severity = env("CAM_POPUP_FRIGATE_SEVERITY", "alert")
        cam_url = env("CAM_POPUP_STREAM_URL", "")
        self.camera_popup = None
        if cam_url and self.cam_trigger_topic:
            self.camera_popup = CameraPopup(
                stream_url=cam_url,
                window_title=env("CAM_POPUP_TITLE", "CameraPopup"),
                grace_secs=float(env("CAM_POPUP_GRACE_SECS", "10")),
                width=int(env("CAM_POPUP_WIDTH", "960")),
                height=int(env("CAM_POPUP_HEIGHT", "360")),
                corner=env("CAM_POPUP_CORNER", "top-right"),
                margin=int(env("CAM_POPUP_MARGIN", "16")),
                hwdec=env("CAM_POPUP_HWDEC", "no"),
            )

        # Inactivity auto power-off: after IDLE_OFF_TIMEOUT of no input
        # (mouse/keyboard/remote, via swayidle + the Wayland idle protocol) AND
        # nothing playing, warn on-screen then fire the "Media Center Off" scene
        # (TV+AVR off; PC stays on). Enabled by default; HA toggle + IDLE_OFF_*
        # env to tune. See config.env.example.
        # HA notification toasts (MQTT-driven; Bigscreen has no notification
        # server so the launcher renders its own layer-shell overlay). Always available;
        # only shows what HA explicitly publishes to NOTIFY_TOPIC.
        self.notify = NotifyPopup()
        self.notify_topic = env("NOTIFY_TOPIC", "avr-tv-bridge/notify")
        # Debounce the "Closed X" toast so a held-IR close (repeat frames) or a
        # double-tap doesn't stack multiple identical toasts.
        self._last_close_toast: dict[str, float] = {}

        # Receiver (Denon) power state, tracked from its retained MQTT topic.
        # Shared by the chime (skip waking the amp when it's already on) and the
        # adaptive idle timeout (a fresh OFF->ON power-on starts the short window).
        self._denon_id = env("MQTT_RECEIVER_ID", "denon")
        self._denon_power = None     # ON / OFF / None (unknown)
        # When the chime itself wakes the amp, the resulting OFF->ON must NOT be
        # treated as a user power-on (it would start the short idle window and
        # power everything off right after the ping). Set to a short deadline
        # around a chime-wake so the transition handler can tell them apart.
        self._chime_wake_until = 0.0

        # Attention chime: a soft sound played on the AVR to gently get someone's
        # attention from HA (a dashboard button). Plays ~4s then stops on its own;
        # re-pressing restarts it. If the amp is off when it fires we wake it (and
        # route to the PC input) so it's heard, and leave it on afterward.
        self.chime = None
        if env("CHIME_ENABLE", "1") not in ("0", "false", "False", ""):
            self.chime = ChimePlayer(
                sound=env("CHIME_SOUND",
                          "/usr/share/sounds/freedesktop/stereo/complete.oga"),
                repeat=int(env("CHIME_REPEAT", "3")),
                gap_ms=int(env("CHIME_GAP_MS", "400")),
                sink=env("CHIME_SINK", ""),
                player=env("CHIME_PLAYER", "paplay"),
                volume=int(env("CHIME_VOLUME", "32768")),  # ~50% = soft
            )
            self._chime_amp_on = env("CHIME_AMP_ON", "1") \
                not in ("0", "false", "False", "")
            self._chime_amp_input = env("CHIME_AMP_INPUT",
                                        env("SCENE_AVR_INPUT", "MPLAY"))
            # Also steer the amp onto CHIME_AMP_INPUT when it's already ON but on
            # another source (e.g. you switched the system on yourself and it's
            # sitting on TV/CD audio). Without this the chime plays into a source
            # nobody hears. Set 0 to leave the input alone unless we wake the amp.
            self._chime_input_sync = env("CHIME_AMP_INPUT_SYNC", "1") \
                not in ("0", "false", "False", "")
            # Max seconds to wait for the amp to CONFIRM power=ON (polled over
            # telnet, returns as soon as it's up) + a settle for the HDMI audio
            # link — so the chime never starts while the amp is still waking or
            # switching inputs (which would swallow the first notes).
            self._chime_amp_wait = float(env("CHIME_AMP_WAIT", "8.0"))
            self._chime_amp_settle = float(env("CHIME_AMP_SETTLE", "4.0"))
            # On a cold amp-wake, play extra repeats (the amp's HDMI audio can take
            # a few seconds to pass sound after it reports power=ON).
            self._chime_cold_extra = int(env("CHIME_COLD_EXTRA_REPEAT", "2"))
            # Suppress idle auto-off for this long after a chime, so a ping never
            # cascades into a power-off (the amp returns to standby on its own via
            # the power-sync watchdog if the TV stays off).
            self._chime_idle_defer = float(env("CHIME_IDLE_DEFER", "180"))
            # Direct Denon telnet client to confirm the amp is really ON (and on
            # the PC input) before playing — the retained MQTT state lags the
            # bridge's poll by up to POLL_INTERVAL, which made a just-powered-on
            # amp look cold and delayed the chime. Best-effort: without it we fall
            # back to the MQTT cache and a fixed wait.
            self._chime_denon = None
            denon_host = env("DENON_HOST", "")
            if denon_host:
                try:
                    from drivers.denon.client import DenonClient
                    self._chime_denon = DenonClient(denon_host)
                except Exception as e:
                    log.warning("chime: no direct Denon client (%s); using the "
                                "MQTT state + a fixed wait instead", e)
            else:
                log.warning("chime: DENON_HOST not set; using the MQTT state + a "
                            "fixed wait instead of a direct amp check")

        self.idle_off = None
        if env("IDLE_OFF_ENABLE", "1") not in ("0", "false", "False", ""):
            self.idle_off = IdleOff(
                publish_off=self._fire_media_center_off,
                timeout_secs=int(env("IDLE_OFF_TIMEOUT", "1800")),
                warn_secs=int(env("IDLE_OFF_WARN", "60")),
                notifier=self.notify,   # warning shown as a toast, not kdialog
                # Adaptive: power on untouched -> short window; first use -> full.
                short_timeout_secs=int(env("IDLE_OFF_SHORT_TIMEOUT", "300")),
                adaptive=env("IDLE_OFF_ADAPTIVE", "1")
                    not in ("0", "false", "False", ""),
            )

        # AVR channel-sync: match the HDMI output profile (stereo/5.1/7.1) to the
        # receiver's EDID so real surround isn't downmixed to stereo. Optional
        # Denon power cross-check (off by default; EDID availability is enough).
        # See channel_sync.py + config.env.example (CHANNEL_SYNC_*).
        self.channel_sync = None
        if env("CHANNEL_SYNC_ENABLE", "1") not in ("0", "false", "False", ""):
            denon_client = None
            if env("CHANNEL_SYNC_DENON_CHECK", "0") in ("1", "true", "True"):
                try:
                    from drivers.denon.client import DenonClient
                    denon_client = DenonClient(env("DENON_HOST", required=True))
                except Exception as e:
                    log.warning("channel-sync Denon cross-check unavailable: %s", e)
            # Cap target at the declared speaker layout. SPEAKER_CHANNELS is the
            # max channels the system can use (2 = stereo/TV-direct, 6 = 5.1,
            # 8 = 7.1). Defaults to 8 (no cap) to preserve prior behavior.
            self.channel_sync = ChannelSync(
                card_hint=env("CHANNEL_SYNC_CARD_HINT", "hdmi"),
                poll_secs=float(env("CHANNEL_SYNC_POLL", "15")),
                denon=denon_client,
                denon_crosscheck=denon_client is not None,
                prefer_surround=env("CHANNEL_SYNC_PREFER_SURROUND", "1")
                                not in ("0", "false", "False", ""),
                max_channels=int(env("SPEAKER_CHANNELS", "8")),
            )

        if hasattr(mqtt, "CallbackAPIVersion"):
            self.client = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                client_id=f"{BASE}-launcher-{os.getpid()}", clean_session=True)
        else:
            self.client = mqtt.Client(client_id=f"{BASE}-launcher-{os.getpid()}",
                                      clean_session=True)
        self.client.username_pw_set(self.mqtt_user, self.mqtt_pass)
        self.client.will_set(AVAIL_TOPIC, "offline", retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        failed = getattr(rc, "is_failure", None)
        if (failed if failed is not None else (rc != 0)):
            log.error("MQTT connect failed rc=%s", rc)
            return
        log.info("Connected to MQTT %s:%s", self.mqtt_host, self.mqtt_port)
        client.publish(AVAIL_TOPIC, "online", retain=True)
        self._publish_discovery()
        client.subscribe(CMD_WILDCARD)
        client.subscribe(f"{BASE}/close/+/set")   # close-one-app commands
        client.subscribe(f"{BASE}/display/wake")
        client.subscribe(f"{BASE}/home/set")
        client.subscribe(self.notify_topic)
        client.subscribe(f"{BASE}/media/+/set")
        # D-pad navigation (up/down/left/right/ok/back) from HA/phone — same
        # keystrokes cec_nav injects for the physical remote, via navkey.sh.
        client.subscribe(f"{BASE}/nav/+/set")
        # Publish media status right away.
        self._publish_media_status()
        # Idle auto-off: HA toggle + timeout number + publish state + start.
        if self.idle_off is not None:
            client.subscribe(f"{BASE}/idleoff/enabled/set")
            client.publish(f"{BASE}/idleoff/enabled/state",
                          "ON" if self.idle_off.enabled else "OFF", retain=True)
            # Adjustable timeout (minutes) from HA/phone.
            client.subscribe(f"{BASE}/idleoff/timeout/set")
            client.publish(f"{BASE}/idleoff/timeout/state",
                          str(self.idle_off.timeout // 60), retain=True)
            self.idle_off.start()
        # Camera popup: subscribe to the detection-count trigger + the HA toggle.
        if self.camera_popup is not None:
            client.subscribe(self.cam_trigger_topic)
            client.subscribe(f"{BASE}/campopup/enabled/set")
            client.subscribe(f"{BASE}/campopup/toggle")
            # Reflect current toggle state (default on).
            client.publish(f"{BASE}/campopup/enabled/state",
                          "ON" if self.camera_popup.enabled else "OFF",
                          retain=True)
        # AVR channel-sync: HA on/off toggle + publish state + start.
        if self.channel_sync is not None:
            client.subscribe(f"{BASE}/chansync/enabled/set")
            client.publish(f"{BASE}/chansync/enabled/state",
                          "ON" if self.channel_sync.enabled else "OFF",
                          retain=True)
            self.channel_sync.start()
        # Attention chime: momentary button + track the receiver's power state so
        # we only wake the amp when it's actually off.
        if self.chime is not None:
            client.subscribe(f"{BASE}/chime/set")
        # Track the receiver's power state for the chime (skip-wake) AND the
        # adaptive idle timeout (OFF->ON -> short window).
        if self.chime is not None or self.idle_off is not None:
            client.subscribe(f"{BASE}/{self._denon_id}/power/state")

    def _publish_discovery(self):
        # Attach the launch buttons to the SAME shared Media Center device the
        # bridge uses, so all controls live under one HA device.
        hub = {
            "identifiers": [f"{BASE}_host"],
            "name": "Media Center",
            "model": "avr-tv-bridge",
            "manufacturer": "self-hosted",
        }
        for key, spec in APPS.items():
            topic = f"{DISCOVERY_PREFIX}/button/{BASE}_launcher/{key}/config"
            payload = {
                "name": f"Launch {spec['label']}",
                "unique_id": f"{BASE}_launch_{key}",
                "command_topic": f"{BASE}/launch/{key}/set",
                "payload_press": "PRESS",
                "availability": [{"topic": AVAIL_TOPIC}],
                "device": hub,
                "icon": "mdi:play-box-multiple",
            }
            self.client.publish(topic, json.dumps(payload, separators=(",", ":")),
                               retain=True)
        # Manual "Wake Display" button.
        self.client.publish(
            f"{DISCOVERY_PREFIX}/button/{BASE}_launcher/wake_display/config",
            json.dumps({
                "name": "Wake Display",
                "unique_id": f"{BASE}_wake_display",
                "command_topic": f"{BASE}/display/wake",
                "payload_press": "PRESS",
                "availability": [{"topic": AVAIL_TOPIC}],
                "device": hub,
                "icon": "mdi:monitor-shimmer",
            }, separators=(",", ":")), retain=True)

        # "Home Screen" button — return the host to the Bigscreen home
        # screen (injects XF86HomePage via navkey.sh, same as the PREV remote
        # button). Works from the phone dashboard too.
        self.client.publish(
            f"{DISCOVERY_PREFIX}/button/{BASE}_launcher/home/config",
            json.dumps({
                "name": "Home Screen",
                "unique_id": f"{BASE}_home",
                "command_topic": f"{BASE}/home/set",
                "payload_press": "PRESS",
                "availability": [{"topic": AVAIL_TOPIC}],
                "device": hub,
                "icon": "mdi:home",
            }, separators=(",", ":")), retain=True)

        # Media transport buttons (play/pause, next, previous) + a status sensor.
        media_buttons = [
            ("playpause", "Play/Pause", "mdi:play-pause"),
            ("next", "Next", "mdi:skip-next"),
            ("previous", "Previous", "mdi:skip-previous"),
        ]
        for key, label, icon in media_buttons:
            self.client.publish(
                f"{DISCOVERY_PREFIX}/button/{BASE}_launcher/media_{key}/config",
                json.dumps({
                    "name": label,
                    "unique_id": f"{BASE}_media_{key}",
                    "command_topic": f"{BASE}/media/{key}/set",
                    "payload_press": "PRESS",
                    "availability": [{"topic": AVAIL_TOPIC}],
                    "device": hub,
                    "icon": icon,
                }, separators=(",", ":")), retain=True)
        # D-pad navigation buttons — inject the same keystrokes the physical
        # remote sends over CEC, so the phone/HA can navigate Bigscreen + apps.
        nav_buttons = [
            ("up", "Up", "mdi:chevron-up"),
            ("down", "Down", "mdi:chevron-down"),
            ("left", "Left", "mdi:chevron-left"),
            ("right", "Right", "mdi:chevron-right"),
            ("ok", "OK", "mdi:circle-slice-8"),
            ("back", "Back", "mdi:keyboard-return"),
        ]
        for key, label, icon in nav_buttons:
            self.client.publish(
                f"{DISCOVERY_PREFIX}/button/{BASE}_launcher/nav_{key}/config",
                json.dumps({
                    "name": f"Nav {label}",
                    "unique_id": f"{BASE}_nav_{key}",
                    "command_topic": f"{BASE}/nav/{key}/set",
                    "payload_press": "PRESS",
                    "availability": [{"topic": AVAIL_TOPIC}],
                    "device": hub,
                    "icon": icon,
                }, separators=(",", ":")), retain=True)
        # Media status sensor (playing / paused / stopped / idle) + now-playing.
        self.client.publish(
            f"{DISCOVERY_PREFIX}/sensor/{BASE}_launcher/media_status/config",
            json.dumps({
                "name": "Now Playing",
                "unique_id": f"{BASE}_media_status",
                "state_topic": f"{BASE}/media/status",
                "json_attributes_topic": f"{BASE}/media/attributes",
                "availability": [{"topic": AVAIL_TOPIC}],
                "device": hub,
                "icon": "mdi:play-circle",
            }, separators=(",", ":")), retain=True)
        # Discrete now-playing field sensors. HA composes these (via a Universal
        # Media Player + template) into a real media_player card, and the phone
        # notification reads them directly. Core HA has NO media_player MQTT
        # discovery platform, so we expose the fields as sensors instead.
        media_fields = [
            ("state", "Media State", f"{BASE}/media/state", "mdi:play-pause", None),
            ("title", "Media Title", f"{BASE}/media/title", "mdi:movie-open", None),
            ("subtitle", "Media Subtitle", f"{BASE}/media/subtitle", "mdi:subtitles", None),
            ("art", "Media Art", f"{BASE}/media/art", "mdi:image", None),
            ("position", "Media Position", f"{BASE}/media/position", "mdi:timer-outline", "s"),
            ("duration", "Media Duration", f"{BASE}/media/duration", "mdi:timer", "s"),
            ("source", "Media Source", f"{BASE}/media/source", "mdi:import", None),
            ("media_type", "Media Type", f"{BASE}/media/media_type", "mdi:shape", None),
        ]
        for key, label, topic, icon, unit in media_fields:
            cfg = {
                "name": label,
                "unique_id": f"{BASE}_media_{key}_field",
                "state_topic": topic,
                "availability": [{"topic": AVAIL_TOPIC}],
                "device": hub,
                "icon": icon,
                "enabled_by_default": False,  # helper entities; keep UI tidy
            }
            if unit:
                cfg["unit_of_measurement"] = unit
            self.client.publish(
                f"{DISCOVERY_PREFIX}/sensor/{BASE}_launcher/media_{key}/config",
                json.dumps(cfg, separators=(",", ":")), retain=True)

        # Camera auto-popup enable/disable toggle (only if configured).
        if self.camera_popup is not None:
            self.client.publish(
                f"{DISCOVERY_PREFIX}/switch/{BASE}_launcher/campopup/config",
                json.dumps({
                    "name": env("CAM_POPUP_TITLE", "CameraPopup") + " Auto-Popup",
                    "unique_id": f"{BASE}_campopup_enabled",
                    "state_topic": f"{BASE}/campopup/enabled/state",
                    "command_topic": f"{BASE}/campopup/enabled/set",
                    "payload_on": "ON", "payload_off": "OFF",
                    "availability": [{"topic": AVAIL_TOPIC}],
                    "device": hub,
                    "icon": "mdi:cctv",
                }, separators=(",", ":")), retain=True)
            # Momentary button that shows/hides the camera overlay ON THE TV
            # (same as the remote's "0" button -> campopup/toggle topic).
            self.client.publish(
                f"{DISCOVERY_PREFIX}/button/{BASE}_launcher/campopup_toggle/config",
                json.dumps({
                    "name": "Camera View",
                    "unique_id": f"{BASE}_campopup_toggle",
                    "command_topic": f"{BASE}/campopup/toggle",
                    "payload_press": "PRESS",
                    "availability": [{"topic": AVAIL_TOPIC}],
                    "device": hub,
                    "icon": "mdi:cctv",
                }, separators=(",", ":")), retain=True)

        # Idle auto-off enable/disable toggle.
        if self.idle_off is not None:
            self.client.publish(
                f"{DISCOVERY_PREFIX}/switch/{BASE}_launcher/idleoff/config",
                json.dumps({
                    "name": "Auto Power-Off (idle)",
                    "unique_id": f"{BASE}_idleoff_enabled",
                    "state_topic": f"{BASE}/idleoff/enabled/state",
                    "command_topic": f"{BASE}/idleoff/enabled/set",
                    "payload_on": "ON", "payload_off": "OFF",
                    "availability": [{"topic": AVAIL_TOPIC}],
                    "device": hub,
                    "icon": "mdi:timer-off-outline",
                }, separators=(",", ":")), retain=True)
            # Adjustable idle timeout (in MINUTES) as an HA number slider/box.
            self.client.publish(
                f"{DISCOVERY_PREFIX}/number/{BASE}_launcher/idleoff_timeout/config",
                json.dumps({
                    "name": "Auto Power-Off Timeout",
                    "unique_id": f"{BASE}_idleoff_timeout",
                    "state_topic": f"{BASE}/idleoff/timeout/state",
                    "command_topic": f"{BASE}/idleoff/timeout/set",
                    "min": 1, "max": 240, "step": 1,
                    "unit_of_measurement": "min",
                    "mode": "box",
                    "availability": [{"topic": AVAIL_TOPIC}],
                    "device": hub,
                    "icon": "mdi:timer-cog-outline",
                }, separators=(",", ":")), retain=True)
        # AVR channel-sync enable/disable toggle.
        if self.channel_sync is not None:
            self.client.publish(
                f"{DISCOVERY_PREFIX}/switch/{BASE}_launcher/chansync/config",
                json.dumps({
                    "name": "AVR Channel Sync",
                    "unique_id": f"{BASE}_chansync_enabled",
                    "state_topic": f"{BASE}/chansync/enabled/state",
                    "command_topic": f"{BASE}/chansync/enabled/set",
                    "payload_on": "ON", "payload_off": "OFF",
                    "availability": [{"topic": AVAIL_TOPIC}],
                    "device": hub,
                    "icon": "mdi:surround-sound",
                }, separators=(",", ":")), retain=True)
        # Attention chime momentary button.
        if self.chime is not None:
            self.client.publish(
                f"{DISCOVERY_PREFIX}/button/{BASE}_launcher/chime/config",
                json.dumps({
                    "name": "Attention Chime",
                    "unique_id": f"{BASE}_chime",
                    "command_topic": f"{BASE}/chime/set",
                    "payload_press": "PRESS",
                    "availability": [{"topic": AVAIL_TOPIC}],
                    "device": hub,
                    "icon": "mdi:bell-ring-outline",
                }, separators=(",", ":")), retain=True)
        log.info("Published discovery for %d launch buttons + wake + media",
                 len(APPS))

    def _on_message(self, client, userdata, msg):
        # Camera popup trigger topic (matched by exact topic).
        if self.camera_popup is not None and msg.topic == self.cam_trigger_topic:
            raw = msg.payload.decode("utf-8", "ignore").strip()
            count = self._parse_cam_trigger(raw)
            if count is None:
                return  # event not relevant (e.g. other camera / severity)
            log.info("Camera trigger %s -> count %d", msg.topic, count)
            try:
                self.camera_popup.on_count(count)
            except Exception as e:
                log.exception("camera popup on_count failed: %s", e)
            return

        parts = msg.topic.split("/")
        # Cache the receiver's power state (chime skip-wake) and detect a fresh
        # OFF->ON power-on to restart the adaptive short idle window.
        if (len(parts) == 4 and parts[1] == self._denon_id
                and parts[2] == "power" and parts[3] == "state"):
            newp = msg.payload.decode("utf-8", "ignore").strip().upper()
            prev = self._denon_power
            self._denon_power = newp
            if newp == "ON" and prev == "OFF" and self.idle_off is not None:
                if time.monotonic() < self._chime_wake_until:
                    # One-shot: consume the marker so a REAL power-on later in
                    # the 20s window still starts the short idle window.
                    self._chime_wake_until = 0.0
                    log.info("power-on was a chime wake — not starting short "
                             "idle window")
                else:
                    try:
                        self.idle_off.on_power_on()
                    except Exception as e:
                        log.debug("idle on_power_on failed: %s", e)
            return
        # avr-tv-bridge/chime/set  -> soft attention chime (wake amp if off, play)
        if (self.chime is not None and len(parts) == 3
                and parts[1] == "chime" and parts[2] == "set"):
            log.info("Attention chime")
            threading.Thread(target=self._play_chime, daemon=True).start()
            return
        # avr-tv-bridge/campopup/toggle  (remote "0" button: show if hidden, else hide)
        if (self.camera_popup is not None and len(parts) == 3
                and parts[1] == "campopup" and parts[2] == "toggle"):
            log.info("Camera popup toggle")
            try:
                self.camera_popup.toggle()
            except Exception as e:
                log.exception("camera popup toggle failed: %s", e)
            return
        # avr-tv-bridge/campopup/enabled/set  (HA toggle)
        if (self.camera_popup is not None and len(parts) == 4
                and parts[1] == "campopup" and parts[3] == "set"):
            on = msg.payload.decode("utf-8", "ignore").strip().upper() == "ON"
            self.camera_popup.set_enabled(on)
            client.publish(f"{BASE}/campopup/enabled/state",
                          "ON" if on else "OFF", retain=True)
            return
        # avr-tv-bridge/home/set  -> Bigscreen home screen
        if len(parts) == 3 and parts[1] == "home" and parts[2] == "set":
            log.info("Home screen request")
            try:
                self._go_home()
            except Exception as e:
                log.exception("Home screen failed: %s", e)
            return
        # avr-tv-bridge/nav/<button>/set  -> D-pad navigation keystroke
        if len(parts) == 4 and parts[1] == "nav" and parts[3] == "set":
            button = parts[2]
            log.info("Nav command: %s", button)
            try:
                self._nav(button)
            except Exception as e:
                log.exception("Nav %s failed: %s", button, e)
            return
        # avr-tv-bridge/display/wake
        if len(parts) == 3 and parts[1] == "display" and parts[2] == "wake":
            log.info("Display wake request")
            try:
                wake_display()
            except Exception as e:
                log.exception("Display wake failed: %s", e)
            return
        # avr-tv-bridge/media/<action>/set
        if len(parts) == 4 and parts[1] == "media" and parts[3] == "set":
            action = parts[2]
            log.info("Media command: %s", action)
            try:
                media_control.control(action)
                # State changes after a transport action — republish shortly.
                threading.Timer(0.6, self._publish_media_status).start()
            except Exception as e:
                log.exception("Media %s failed: %s", action, e)
            return
        # avr-tv-bridge/idleoff/enabled/set  (HA toggle)
        if (self.idle_off is not None and len(parts) == 4
                and parts[1] == "idleoff" and parts[2] == "enabled"
                and parts[3] == "set"):
            on = msg.payload.decode("utf-8", "ignore").strip().upper() == "ON"
            self.idle_off.set_enabled(on)
            client.publish(f"{BASE}/idleoff/enabled/state",
                          "ON" if on else "OFF", retain=True)
            return
        # avr-tv-bridge/idleoff/timeout/set  (HA number, in MINUTES)
        if (self.idle_off is not None and len(parts) == 4
                and parts[1] == "idleoff" and parts[2] == "timeout"
                and parts[3] == "set"):
            raw = msg.payload.decode("utf-8", "ignore").strip()
            try:
                minutes = int(float(raw))
            except (ValueError, TypeError):
                log.warning("bad idle timeout payload: %r", raw)
                return
            minutes = max(1, min(240, minutes))
            self.idle_off.set_timeout(minutes * 60)
            client.publish(f"{BASE}/idleoff/timeout/state",
                          str(minutes), retain=True)
            return
        # avr-tv-bridge/chansync/enabled/set  (HA toggle)
        if (self.channel_sync is not None and len(parts) == 4
                and parts[1] == "chansync" and parts[2] == "enabled"
                and parts[3] == "set"):
            on = msg.payload.decode("utf-8", "ignore").strip().upper() == "ON"
            self.channel_sync.set_enabled(on)
            client.publish(f"{BASE}/chansync/enabled/state",
                          "ON" if on else "OFF", retain=True)
            return
        # HA notification toast (exact topic match on NOTIFY_TOPIC)
        if msg.topic == self.notify_topic:
            try:
                self.notify.show_json(msg.payload.decode("utf-8", "ignore"))
            except Exception as e:
                log.exception("notify failed: %s", e)
            return
        # avr-tv-bridge/launch/<app>/set
        if len(parts) == 4 and parts[1] == "launch" and parts[3] == "set":
            app = parts[2]
            log.info("Launch command: %s", app)
            try:
                result = self.launcher.activate(app)
                client.publish(f"{BASE}/launch/{app}/state", result, retain=False)
            except Exception as e:
                log.exception("Launch %s failed: %s", app, e)
            return
        # avr-tv-bridge/close/<app>/set — close ONE app (dashboard button or the
        # IR hold-to-close). Idempotent: repeated presses (or held-IR repeats)
        # after it's gone are harmless no-ops.
        if len(parts) == 4 and parts[1] == "close" and parts[3] == "set":
            app = parts[2]
            log.info("Close command: %s", app)
            try:
                result = self.launcher.close(app)
                client.publish(f"{BASE}/close/{app}/state", result, retain=False)
                # On-screen confirmation, only when we actually closed something
                # (not for a no-op 'not-running' — and dedupe held-IR repeats so
                # a hold doesn't stack toasts).
                if result == "closed":
                    label = APPS.get(app, {}).get("label", app.title())
                    now = time.monotonic()
                    if now - self._last_close_toast.get(app, 0) > 3:
                        self._last_close_toast[app] = now
                        try:
                            self.notify.show("Media Center", f"Closed {label}",
                                             "", timeout=2.5)
                        except Exception as e:
                            log.debug("close toast failed: %s", e)
            except Exception as e:
                log.exception("Close %s failed: %s", app, e)
            return

    # D-pad buttons HA/phone can request. navkey.sh maps these to the same
    # keycodes cec_nav uses for the physical remote AND consults nav_profiles
    # (so e.g. IPTVnator's Tab-remap still applies), giving one source of truth.
    _NAV_BUTTONS = {"up", "down", "left", "right", "ok", "back"}

    def _nav(self, button: str):
        """Inject a D-pad keystroke via navkey.sh (same path as _go_home and the
        physical remote). Whitelisted so an arbitrary MQTT payload can't run
        other navkey targets."""
        button = (button or "").strip().lower()
        if button not in self._NAV_BUTTONS:
            log.warning("ignoring unknown nav button: %r", button)
            return
        navkey = os.path.join(os.path.dirname(os.path.abspath(__file__)), "navkey.sh")
        subprocess.run([navkey, button], timeout=5,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _go_home(self):
        """Bring the host to the Bigscreen home screen by injecting
        XF86HomePage, reusing navkey.sh so the keycode has one source of truth
        (same path the PREV remote button uses)."""
        navkey = os.path.join(os.path.dirname(os.path.abspath(__file__)), "navkey.sh")
        subprocess.run([navkey, "home"], timeout=5,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _fire_media_center_off(self):
        """Publish the bridge's Media Center Off scene (TV off, then AVR off).
        Used by the idle auto-off watcher. The bridge owns the actual power
        sequencing; we just request the scene."""
        self.client.publish(f"{BASE}/scene/media_center_off/set", "PRESS",
                            retain=False)

    def _play_chime(self):
        """Soft attention chime. If the amp is off, wake it; in every case make
        sure it's on the PC input (otherwise there is no audio path and the
        chime is silent), WAIT until it confirms it's fully on + a settle, then
        play; leaves the amp on afterward. Runs in its own thread (the amp wake
        can take a few seconds)."""
        try:
            # A ping must never trigger an idle power-off.
            if self.idle_off is not None:
                self.idle_off.defer(self._chime_idle_defer)
            # Prefer the amp's LIVE state: the retained MQTT topic is only
            # refreshed every POLL_INTERVAL (10s), so right after you power the
            # system on by hand it still says OFF — which used to make the chime
            # re-send power+input and then stall CHIME_AMP_SETTLE seconds.
            live = self._amp_live_state()
            power = live.get("power") or self._denon_power
            cold = self._chime_amp_on and power != "ON"
            # Route the PC's audio to the amp so the chime is actually heard:
            # both when waking it, and when it's already on but sitting on
            # another source (e.g. TV/CEC audio after a manual power-on).
            want_input = ""
            if self._chime_amp_on and self._chime_amp_input:
                if cold or self._chime_input_sync:
                    want_input = self._chime_amp_input
            # One telnet read for both decisions (get_state is ~0.8s, and the
            # chime should start promptly after the button press).
            changed = self._route_amp_input(live.get("input"), want_input)
            if cold:
                # Mark this as a chime-wake so the OFF->ON transition it causes is
                # NOT treated as a user power-on (which would start the short idle
                # window and power everything off right after the ping).
                self._chime_wake_until = time.monotonic() + 20
                log.info("chime: amp %s — powering on (input %s), waiting to be ready",
                         power or "unknown", want_input or "-")
                self.client.publish(f"{BASE}/{self._denon_id}/power/set", "ON",
                                    retain=False)
                # Re-assert + verify the input once the amp is awake: an SI sent
                # during standby is often dropped, so it could still be on its
                # previous source when the first note plays.
                self._wait_amp_ready(want_input)
            elif want_input and changed is not False:
                # Already on, but we just switched its input (or couldn't tell
                # what it was): give the HDMI audio link a moment to relock, or
                # the first notes play into dead air.
                if self._confirm_amp_input(want_input):
                    time.sleep(min(self._chime_amp_settle, 1.5))
            self.chime.play(extra=self._chime_cold_extra if cold else 0)
        except Exception as e:
            log.exception("chime failed: %s", e)

    def _amp_live_state(self) -> dict:
        """Amp state straight from telnet, bypassing the laggy retained MQTT
        state. Returns {} when there is no direct client, and power/input come
        back as None when the amp can't be reached (DenonClient.get_state()
        reports unreachable amps as {"available": False})."""
        if self._chime_denon is None:
            return {}
        try:
            return self._chime_denon.get_state()
        except Exception as e:
            log.debug("chime: live amp state failed: %s", e)
            return {}

    def _route_amp_input(self, cur, want: str):
        """Select `want` on the amp so the chime has an audio path.

        `cur` is the input read in this pass (None = unknown). Returns True if we
        changed it, False if it already was `want` (so the caller can play
        straight away), or None if `cur` was unreadable (we still asked, but the
        caller must verify)."""
        if not want:
            return False
        if cur == want:
            log.debug("chime: amp already on input %s", want)
            return False
        log.info("chime: amp input %s -> %s (so the chime is audible)",
                 cur or "?", want)
        if self._chime_denon is not None:
            try:
                self._chime_denon.set_input(want)
            except Exception as e:
                log.warning("chime: input set failed (%s); asking the bridge", e)
                self.client.publish(f"{BASE}/{self._denon_id}/input/set", want,
                                    retain=False)
        else:
            self.client.publish(f"{BASE}/{self._denon_id}/input/set", want,
                                retain=False)
        return True if cur is not None else None

    def _confirm_amp_input(self, want: str, attempts: int = 4) -> bool:
        """Check the amp really selected `want`; re-send + retry if not.

        The AVR ignores SI commands sent while it is still booting, so a single
        input set on a cold wake could leave it on its previous source and the
        chime would play into a dead input."""
        if not want or self._chime_denon is None:
            return True     # nothing to verify (or no direct client)
        for _ in range(attempts):
            if self._amp_live_state().get("input") == want:
                return True
            try:
                self._chime_denon.set_input(want)
            except Exception as e:
                log.debug("chime: input re-send failed: %s", e)
                return False
            time.sleep(0.5)
        log.warning("chime: amp did not reach input %s (%d attempts)",
                    want, attempts)
        return False

    def _wait_amp_ready(self, want_input: str = ""):
        """Block until the amp confirms power=ON (polled over telnet so it isn't
        gated on the laggy retained MQTT state), up to CHIME_AMP_WAIT; re-assert
        the PC input now that it's awake, then a settle for the HDMI audio link
        — so the chime isn't swallowed while the amp is still waking / switching
        inputs."""
        deadline = time.monotonic() + self._chime_amp_wait
        confirmed = False
        if self._chime_denon is not None:
            while time.monotonic() < deadline:
                if self._amp_live_state().get("power") == "ON":
                    confirmed = True
                    break
                time.sleep(0.5)
            if confirmed:
                log.info("chime: amp confirmed ON")
            else:
                log.warning("chime: amp not confirmed ON in %.0fs; playing anyway",
                            self._chime_amp_wait)
        else:
            time.sleep(self._chime_amp_wait)   # no telnet client; fixed wait
        # Only worth verifying the input if the amp actually came up (it's ~0.8s
        # per read, and pointless when the amp is unreachable).
        if confirmed and want_input:
            self._confirm_amp_input(want_input)
        # Settle: let the Denon lock the HDMI audio input before the first note.
        time.sleep(self._chime_amp_settle)

    def _parse_cam_trigger(self, raw: str):
        """Translate a trigger payload into a popup count (>0 open, 0 close).

        Returns None if the message isn't relevant (wrong camera/severity), so
        the caller ignores it.
        """
        if self.cam_trigger_mode == "frigate_review":
            # Frigate frigate/reviews JSON: {type: new|update|end, after:{...}}
            try:
                evt = json.loads(raw)
            except (ValueError, TypeError):
                return None
            etype = evt.get("type")
            after = evt.get("after", {}) or {}
            camera = after.get("camera", "")
            severity = after.get("severity", "")
            # Only care about our camera + configured severity (default alert).
            if self.cam_frigate_camera and camera != self.cam_frigate_camera:
                return None
            if self.cam_frigate_severity and severity != self.cam_frigate_severity:
                # An 'end' event may downgrade severity; still treat end as close.
                if etype != "end":
                    return None
            if etype in ("new", "update"):
                return 1
            if etype == "end":
                return 0
            return None
        # Default: integer count (or ON/OFF).
        try:
            return int(raw)
        except ValueError:
            return 1 if raw.upper() in ("ON", "TRUE") else 0

    def _publish_media_status(self):
        """Publish current media transport status + rich now-playing details.

        Uses the unified snapshot (Plex API when Plex is the active player, else
        MPRIS for Netflix/Chrome). Publishes both the legacy sensor topics
        (status + attributes, unchanged for back-compat) AND discrete field
        topics the HA Universal Media Player composes into a real media_player
        entity: state, title, subtitle, art URL, position, duration, media_type.
        """
        try:
            snap = media_control.now_playing_snapshot()
        except Exception as e:
            log.warning("media snapshot failed: %s", e)
            return
        st = snap["state"]                    # playing/paused/idle
        title = snap.get("title", "")
        subtitle = snap.get("subtitle", "")
        art = snap.get("art", "")
        pos = snap.get("position", 0)
        dur = snap.get("duration", 0)
        source = snap.get("source", "none")
        media_type = snap.get("media_type", "")

        # Human display line (for the legacy text sensor).
        display = title if (title and st in ("playing", "paused")) else st
        if subtitle and st in ("playing", "paused"):
            display = f"{title} — {subtitle}" if title else subtitle

        # Legacy topics (kept so existing sensor/dashboards don't break).
        self.client.publish(f"{BASE}/media/status", display, retain=True)
        self.client.publish(
            f"{BASE}/media/attributes",
            json.dumps({"status": st, "title": title, "artist": subtitle,
                        "album": "", "source": source, "art": art,
                        "position": pos, "duration": dur,
                        "media_type": media_type}),
            retain=True)

        # Discrete field topics for the media_player (retained so HA restores
        # state on restart). state maps to HA media_player states.
        self.client.publish(f"{BASE}/media/state", st, retain=True)
        self.client.publish(f"{BASE}/media/title", title, retain=True)
        self.client.publish(f"{BASE}/media/subtitle", subtitle, retain=True)
        self.client.publish(f"{BASE}/media/art", art, retain=True)
        self.client.publish(f"{BASE}/media/position", str(pos), retain=True)
        self.client.publish(f"{BASE}/media/duration", str(dur), retain=True)
        self.client.publish(f"{BASE}/media/source", source, retain=True)
        self.client.publish(f"{BASE}/media/media_type", media_type, retain=True)

    def _status_loop(self):
        while not self._stop.wait(self.status_interval):
            self._publish_media_status()

    def run(self):
        self.client.connect(self.mqtt_host, self.mqtt_port, keepalive=30)
        poller = threading.Thread(target=self._status_loop, daemon=True)
        poller.start()
        self.client.loop_forever()

    def stop(self, *_):
        log.info("Shutting down launcher service")
        self._stop.set()
        if self.idle_off is not None:
            self.idle_off.stop()
        if self.channel_sync is not None:
            self.channel_sync.stop()
        try:
            self.client.publish(AVAIL_TOPIC, "offline", retain=True)
            self.client.disconnect()
        except Exception:
            pass


def main():
    svc = LauncherService()
    signal.signal(signal.SIGTERM, svc.stop)
    signal.signal(signal.SIGINT, svc.stop)
    svc.run()


if __name__ == "__main__":
    main()
