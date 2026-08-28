#!/usr/bin/env python3
"""Harmonia bridge: expose a home-theater chain (AV receiver + display) to Home
Assistant over MQTT with auto-discovery, controlling each device via its native
network API through a pluggable driver layer.

The bridge is composed from config: pick a receiver driver, a display driver, or
"none" for either. TV-direct setups (no receiver) are first-class — the display
becomes the audio authority if it declares volume/mute. Scenes, the power-sync
watchdog, and HA discovery all adapt to which roles are present.

Run as a systemd service on the media PC. Configuration comes from environment
variables (see config.env), loaded by the systemd unit via EnvironmentFile.

Back-compat: the MQTT device-id slugs (default "denon"/"vizio") and all HA
unique_ids are unchanged, so existing installs keep their entities. New installs
can override the slugs via MQTT_RECEIVER_ID / MQTT_DISPLAY_ID.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time

import paho.mqtt.client as mqtt

import discovery as disc
import drivers
from drivers.registry import get_receiver, get_display
from drivers.base import CAP_POWER, CAP_VOLUME, CAP_MUTE, CAP_INPUT

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bridge")

BASE = disc.BASE
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "10"))


def env(key: str, default: str | None = None, required: bool = False) -> str:
    val = os.environ.get(key, default)
    if required and not val:
        log.error("Missing required env var %s", key)
        sys.exit(2)
    return val  # type: ignore


class ManagedDevice:
    """A driver instance plus its bridge-facing metadata (MQTT slug, role)."""

    def __init__(self, mqtt_id: str, role: str, driver, name: str,
                 model: str, manufacturer: str, inputs: list[str],
                 label: str):
        self.mqtt_id = mqtt_id          # topic slug, e.g. "denon" / "vizio"
        self.role = role                # "receiver" | "display"
        self.driver = driver
        self.name = name
        self.model = model
        self.manufacturer = manufacturer
        self.inputs = inputs
        self.label = label

    @property
    def capabilities(self) -> tuple[str, ...]:
        return tuple(self.driver.capabilities)

    def get_state(self) -> dict:
        return self.driver.get_state()


class Bridge:
    def __init__(self):
        self.mqtt_host = env("MQTT_HOST", required=True)
        self.mqtt_port = int(env("MQTT_PORT", "1883"))
        self.mqtt_user = env("MQTT_USER", required=True)
        self.mqtt_pass = env("MQTT_PASS", required=True)

        # Scene inputs (per role). Kept generic; the AVR/TV env names are honored
        # for back-compat.
        self.scene_avr_input = env("SCENE_AVR_INPUT", "MPLAY")
        self.scene_tv_input = env("SCENE_TV_INPUT", "HDMI-1")
        self.scene_power_timeout = float(env("SCENE_POWER_TIMEOUT", "15"))
        self.scene_poll_interval = float(env("SCENE_POLL_INTERVAL", "1.0"))
        self.scene_input_settle = float(env("SCENE_INPUT_SETTLE", "1.5"))

        # Power-sync watchdog: keep the receiver's power in sync with the display
        # (display is source of truth). Only meaningful when BOTH roles exist.
        self.sync_power = env("SYNC_POWER", "1") not in ("0", "false", "False", "")
        self.sync_persist_polls = int(env("SYNC_PERSIST_POLLS", "2"))
        self.sync_suppress_secs = float(env("SYNC_SUPPRESS_SECS", "30"))
        self._sync_suppress_until = 0.0
        self._mismatch_count = 0

        # ---- Compose devices from config (driver layer) ----
        # MQTT slugs default to denon/vizio so existing HA entities are preserved.
        self.receiver: ManagedDevice | None = None
        self.display: ManagedDevice | None = None
        self._build_devices()

        # The audio authority = the device whose volume/mute the hub master
        # controls. Receiver if present, else the display if it has volume.
        self.audio_authority = self._pick_audio_authority()

        self._stop = threading.Event()
        self._last_state: dict[str, dict] = {}
        self._published: dict[str, str] = {}

        self._paho_v2 = hasattr(mqtt, "CallbackAPIVersion")
        if self._paho_v2:
            self.client = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                client_id=f"{BASE}-{os.getpid()}", clean_session=True)
        else:
            self.client = mqtt.Client(client_id=f"{BASE}-{os.getpid()}",
                                      clean_session=True)
        self.client.username_pw_set(self.mqtt_user, self.mqtt_pass)
        self.client.will_set(f"{BASE}/bridge/availability", "offline", retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    # ---- Composition ----
    def _build_devices(self):
        recv_name = env("HARMONIA_RECEIVER", "denon")
        disp_name = env("HARMONIA_DISPLAY", "vizio")

        recv_cls = get_receiver(recv_name)
        if recv_cls is not None:
            drv = recv_cls.from_env(env)
            self.receiver = ManagedDevice(
                mqtt_id=env("MQTT_RECEIVER_ID", "denon"),
                role="receiver", driver=drv,
                name=getattr(drv, "name", "Receiver"),
                model=getattr(drv, "model", "AVR"),
                manufacturer=getattr(drv, "manufacturer", "Receiver"),
                inputs=getattr(drv, "inputs", []),
                label=getattr(drv, "label", "AVR"))
            log.info("receiver driver %r as mqtt id %r (caps %s)",
                     recv_name, self.receiver.mqtt_id, self.receiver.capabilities)
        else:
            log.info("no receiver (HARMONIA_RECEIVER=%s)", recv_name)

        disp_cls = get_display(disp_name)
        if disp_cls is not None:
            drv = disp_cls.from_env(env)
            self.display = ManagedDevice(
                mqtt_id=env("MQTT_DISPLAY_ID", "vizio"),
                role="display", driver=drv,
                name=getattr(drv, "name", "Display"),
                model=getattr(drv, "model", "TV"),
                manufacturer=getattr(drv, "manufacturer", "Display"),
                inputs=getattr(drv, "inputs", []),
                label=getattr(drv, "label", "TV"))
            log.info("display driver %r as mqtt id %r (caps %s)",
                     disp_name, self.display.mqtt_id, self.display.capabilities)
        else:
            log.info("no display (HARMONIA_DISPLAY=%s)", disp_name)

        if self.receiver is None and self.display is None:
            log.error("no receiver AND no display configured — nothing to do")
            sys.exit(2)

    def _pick_audio_authority(self) -> ManagedDevice | None:
        if self.receiver is not None and CAP_VOLUME in self.receiver.capabilities:
            return self.receiver
        if self.display is not None and CAP_VOLUME in self.display.capabilities:
            return self.display
        return None

    def _devices(self) -> list[ManagedDevice]:
        return [d for d in (self.receiver, self.display) if d is not None]

    def _by_id(self, mqtt_id: str) -> ManagedDevice | None:
        for d in self._devices():
            if d.mqtt_id == mqtt_id:
                return d
        return None

    # ---- MQTT lifecycle ----
    def _on_connect(self, client, userdata, flags, rc, properties=None):
        failed = getattr(rc, "is_failure", None)
        if (failed if failed is not None else (rc != 0)):
            log.error("MQTT connect failed rc=%s", rc)
            return
        log.info("Connected to MQTT %s:%s", self.mqtt_host, self.mqtt_port)
        client.publish(f"{BASE}/bridge/availability", "online", retain=True)
        # Prime the audio authority so volume ceiling (if any) is known before
        # discovery publishes the volume entity's max.
        if self.audio_authority is not None:
            try:
                self.audio_authority.get_state()
            except Exception:
                pass
        self._publish_discovery()
        client.subscribe(f"{BASE}/+/+/set")
        client.subscribe(f"{BASE}/scene/+/set")
        threading.Thread(target=self._poll_once, daemon=True).start()

    def _vol_params(self):
        """(vol_max, vol_step) from the audio authority (Denon exposes MVMAX)."""
        aa = self.audio_authority
        if aa is None:
            return 100, 1
        drv = aa.driver
        vmax = getattr(drv, "max_volume", 100)
        voff = getattr(drv, "vol_offset", 0)
        step = 0.5 if aa.role == "receiver" else 1
        return vmax + voff, step

    def _publish_discovery(self):
        entities = []
        vol_max, vol_step = self._vol_params()
        for d in self._devices():
            # A receiver exposes its full caps; a display exposes what it declares
            # (power+input by default, +volume/mute for TV-direct).
            vmax = vol_max if d is self.audio_authority else 100
            vstep = vol_step if d is self.audio_authority else 1
            entities += disc.build_entities(
                d.mqtt_id, d.name, d.model, d.manufacturer, d.inputs,
                capabilities=d.capabilities,
                vol_max=vmax, vol_step=vstep,
                label_prefix=d.label)
        # Hub: scene buttons + master power; master volume/mute proxy the audio
        # authority (omitted if there's none).
        aa_id = self.audio_authority.mqtt_id if self.audio_authority else None
        entities += disc.build_hub_entities(
            vol_max=vol_max, vol_step=vol_step, audio_authority=aa_id)
        for topic, payload in entities:
            self.client.publish(topic, disc.dumps(payload), retain=True)
        log.info("Published discovery for %d entities (incl. hub)", len(entities))

    # ---- Command handling ----
    def _on_message(self, client, userdata, msg):
        payload = msg.payload.decode("utf-8", "ignore").strip()
        parts = msg.topic.split("/")
        try:
            if parts[1] == "scene":
                if parts[2] == "media_center_on":
                    self._scene_media_center_on()
                elif parts[2] == "media_center_off":
                    self._scene_media_center_off()
                return
            if parts[1] == "hub":
                if parts[2] == "power":
                    if payload.upper() == "ON":
                        self._scene_media_center_on()
                    else:
                        self._scene_media_center_off()
                return
            # avr-tv-bridge/<dev>/<entity>/set
            _, dev, entity, _ = parts
            self._handle_command(dev, entity, payload)
        except Exception as e:
            log.exception("Error handling %s = %s: %s", msg.topic, payload, e)

    def _handle_command(self, dev: str, entity: str, payload: str):
        managed = self._by_id(dev)
        if managed is None:
            return
        target = managed.driver
        log.info("CMD %s %s = %s", dev, entity, payload)
        if entity == "power":
            self._suppress_sync()
            target.power(payload.upper() == "ON")
        elif entity == "mute":
            target.mute(payload.upper() == "ON")
        elif entity == "input":
            target.set_input(payload)
        elif entity == "volume":
            try:
                vol = float(payload)
            except ValueError:
                return
            target.set_volume(vol)
        time.sleep(0.4)
        self._poll_once()

    def _scene_media_center_on(self):
        recv = self.receiver
        disp = self.display
        log.info("SCENE media_center_on -> receiver=%s display=%s",
                 self.scene_avr_input if recv else "-",
                 self.scene_tv_input if disp else "-")
        self._suppress_sync()
        # Power on: receiver before display (audio path ready first).
        if recv is not None:
            recv.driver.power(True)
        if disp is not None:
            disp.driver.power(True)
        self._request_display_wake("media center on")

        if recv is not None:
            self._wait_powered_on(recv.mqtt_id, recv.driver)
            if CAP_INPUT in recv.capabilities:
                self._set_input_verified(recv.mqtt_id, recv.driver,
                                         self.scene_avr_input)
        if disp is not None:
            self._wait_powered_on(disp.mqtt_id, disp.driver)
            if CAP_INPUT in disp.capabilities:
                self._set_input_verified(disp.mqtt_id, disp.driver,
                                         self.scene_tv_input)

        self._publish_hub_power("ON")
        self._poll_once()

    def _scene_media_center_off(self):
        log.info("SCENE media_center_off -> powering everything down")
        self._suppress_sync()
        # Reverse order: display first, then receiver.
        if self.display is not None:
            self.display.driver.power(False)
        if self.receiver is not None:
            self.receiver.driver.power(False)
        self._publish_hub_power("OFF")
        self._poll_once()

    def _publish_hub_power(self, state: str):
        self._pub(f"{BASE}/hub/power/state", state)

    def _wait_powered_on(self, dev_id: str, client, timeout: float = None) -> bool:
        timeout = timeout if timeout is not None else self.scene_power_timeout
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                st = client.get_state()
            except Exception:
                st = {}
            if st.get("power") == "ON":
                log.info("%s powered on", dev_id)
                return True
            time.sleep(self.scene_poll_interval)
        log.warning("%s did not report power=ON within %.0fs; proceeding anyway",
                    dev_id, timeout)
        return False

    def _set_input_verified(self, dev_id: str, client, want_input: str,
                            attempts: int = 3) -> bool:
        for i in range(attempts):
            client.set_input(want_input)
            time.sleep(self.scene_input_settle)
            try:
                got = client.get_state().get("input")
            except Exception:
                got = None
            if got == want_input:
                log.info("%s input set to %s", dev_id, want_input)
                return True
            log.info("%s input is %r, wanted %s (attempt %d/%d) — retrying",
                     dev_id, got, want_input, i + 1, attempts)
        log.warning("%s input did not reach %s after %d attempts",
                    dev_id, want_input, attempts)
        return False

    # ---- Polling / state publish ----
    def _poll_once(self):
        recv = self.receiver
        disp = self.display
        prev_recv = self._last_state.get(recv.mqtt_id, {}).get("power") if recv else None
        prev_disp = self._last_state.get(disp.mqtt_id, {}).get("power") if disp else None

        if recv is not None:
            self._poll_device(recv)
        if disp is not None:
            self._poll_device(disp)

        r = self._last_state.get(recv.mqtt_id, {}) if recv else {}
        v = self._last_state.get(disp.mqtt_id, {}) if disp else {}

        # Any device transitioning to ON -> request a display wake (catches
        # power-on from any source). Guarded so it doesn't fire on first poll.
        woke = False
        if prev_recv not in (None, "ON") and r.get("power") == "ON":
            woke = True
        if prev_disp not in (None, "ON") and v.get("power") == "ON":
            woke = True
        if woke:
            self._request_display_wake("device powered on")

        # Display ON -> OFF: pause playback so media doesn't stream to a black
        # screen. Only for the display role.
        if disp is not None and prev_disp == "ON" and v.get("power") == "OFF":
            self._request_media_pause("display turned off")

        # Hub master-power = ON if any device is on.
        states = [s for s in (r, v) if s]
        if states:
            on = any(s.get("power") == "ON" for s in states)
            self._publish_hub_power("ON" if on else "OFF")

        # Power-sync only when both roles exist.
        if recv is not None and disp is not None:
            self._power_sync_check(r, v)

    def _power_sync_check(self, recv_state: dict, disp_state: dict):
        """Keep the receiver's power in sync with the display (display = truth)."""
        if not self.sync_power:
            return
        tv = disp_state.get("power")
        avr = recv_state.get("power")
        if tv not in ("ON", "OFF") or avr not in ("ON", "OFF"):
            self._mismatch_count = 0
            return
        if tv == avr:
            self._mismatch_count = 0
            return
        if time.time() < self._sync_suppress_until:
            return
        self._mismatch_count += 1
        log.info("Power mismatch: display=%s receiver=%s (%d/%d polls)",
                 tv, avr, self._mismatch_count, self.sync_persist_polls)
        if self._mismatch_count < self.sync_persist_polls:
            return
        want = (tv == "ON")
        log.info("Power-sync: setting receiver %s to match display",
                 "ON" if want else "OFF")
        self._suppress_sync()
        self.receiver.driver.power(want)

    def _suppress_sync(self):
        self._sync_suppress_until = time.time() + self.sync_suppress_secs
        self._mismatch_count = 0

    def _request_display_wake(self, reason: str):
        log.info("Requesting display wake (%s)", reason)
        self.client.publish(f"{BASE}/display/wake", "PRESS", retain=False)

    def _request_media_pause(self, reason: str):
        log.info("Requesting media pause (%s)", reason)
        self.client.publish(f"{BASE}/media/pause/set", "PRESS", retain=False)

    def _pub(self, topic: str, value, retain: bool = True):
        value = str(value)
        if self._published.get(topic) == value:
            return False
        self._published[topic] = value
        self.client.publish(topic, value, retain=retain)
        return True

    def _poll_device(self, managed: ManagedDevice):
        dev_id = managed.mqtt_id
        try:
            state = managed.get_state()
        except Exception as e:
            log.warning("poll %s failed: %s", dev_id, e)
            state = {"available": False}
        sb = disc.state_base(dev_id)
        caps = managed.capabilities or (CAP_POWER, CAP_VOLUME, CAP_MUTE, CAP_INPUT)
        avail = "online" if state.get("available") else "offline"
        self._pub(f"{sb}/availability", avail)
        if not state.get("available"):
            return
        if CAP_POWER in caps and "power" in state:
            self._pub(f"{sb}/power/state", state["power"])
        if CAP_VOLUME in caps and "volume" in state:
            self._pub(f"{sb}/volume/state", state["volume"])
        if CAP_MUTE in caps and "mute" in state:
            self._pub(f"{sb}/mute/state", "ON" if state["mute"] else "OFF")
        if CAP_INPUT in caps and "input" in state:
            self._pub(f"{sb}/input/state", state["input"])
        self._pub(f"{sb}/attributes", json.dumps(state, sort_keys=True))
        self._last_state[dev_id] = state

    def _poll_loop(self):
        while not self._stop.wait(POLL_INTERVAL):
            self._poll_once()

    # ---- Run ----
    def run(self):
        self.client.connect(self.mqtt_host, self.mqtt_port, keepalive=30)
        poller = threading.Thread(target=self._poll_loop, daemon=True)
        poller.start()
        self.client.loop_forever()

    def stop(self, *_):
        log.info("Shutting down")
        self._stop.set()
        try:
            self.client.publish(f"{BASE}/bridge/availability", "offline", retain=True)
            self.client.disconnect()
        except Exception:
            pass


def main():
    b = Bridge()
    signal.signal(signal.SIGTERM, b.stop)
    signal.signal(signal.SIGINT, b.stop)
    b.run()


if __name__ == "__main__":
    main()
