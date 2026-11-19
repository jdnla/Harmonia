#!/usr/bin/env python3
"""Harmonia IR bridge: expose the USB-UIRT IR transceiver to Home Assistant over
MQTT, for both RECEIVING remote presses and SENDING (blasting) IR commands —
including LEARNING brand-new codes on the fly.

Three capabilities, all driven over MQTT with HA auto-discovery:

  RECEIVE  Reads decoded button presses from the running lircd (via its control
           socket, alongside the existing irexec that drives the launcher — many
           readers are fine). Each press is published as an HA `event` entity
           (so automations can trigger on "KEY_1 on the Vizio remote") plus a
           diagnostic "Last IR Code" sensor.

  SEND     `avr-tv-bridge/ir/send/set` -> `irsend SEND_ONCE <remote> <key>`.
           Payload is JSON {"remote","key","repeats"} or a shorthand string
           "Remote/KEY" / "Remote/KEY x3". Every known key also gets a tap-to-
           send HA button.

  LEARN    `avr-tv-bridge/ir/learn/set` with {"remote","key"} (or "Remote/KEY").
           Captures a NEW code protocol-agnostically: pause the receiver, stop
           lircd to free the serial port, `mode2` raw-capture one burst, store
           it, regenerate a RAW_CODES lircd config, restart lircd. The new key is
           then sendable like any other and a button appears for it.

Design notes:
  * Runs as root (system service): learning must stop/start lircd and write
    /etc/lirc configs, and mode2 opens the FTDI serial device. Receive/send
    alone would work unprivileged (the lircd socket is world-rw), but learning
    is the deciding factor.
  * The learned-code source of truth is a per-remote JSON file under
    LEARNED_DIR; the lircd .conf is regenerated from it, so we never have to
    parse LIRC's own config format back in.
  * Stdlib + paho only (paho already vendored in the bridge venv).
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import signal
import socket
import subprocess
import sys
import threading
import time

import paho.mqtt.client as mqtt

try:
    import discovery as disc           # same src/ dir on the service path
    _BASE = disc.BASE
    _PREFIX = disc.DISCOVERY_PREFIX
    _MEDIA_CENTER_DEVICE = disc.DEVICE_ID
    _dumps = disc.dumps
except Exception:                      # standalone fallback (tests)
    _BASE = "avr-tv-bridge"
    _PREFIX = "homeassistant"
    _MEDIA_CENTER_DEVICE = f"{_BASE}_host"
    def _dumps(p: dict) -> str:
        return json.dumps(p, separators=(",", ":"))

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("ir_bridge")

BASE = _BASE
IR = f"{BASE}/ir"
DEVICE_ID = f"{BASE}_ir"

# IR HA device — its own device, nested under the Media Center via via_device so
# the ~dozen IR entities don't clutter the main device card.
IR_DEVICE = {
    "identifiers": [DEVICE_ID],
    "name": "IR Blaster",
    "model": "USB-UIRT (LIRC)",
    "manufacturer": "self-hosted",
    "via_device": _MEDIA_CENTER_DEVICE,
}


def env(key: str, default: str | None = None, required: bool = False) -> str:
    val = os.environ.get(key, default)
    if required and not val:
        log.error("Missing required env var %s", key)
        sys.exit(2)
    return val  # type: ignore


def sh(cmd: list[str], timeout: float = 15) -> tuple[int, str, str]:
    """Run a command, never raise. Returns (rc, stdout, stderr)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.SubprocessError) as e:
        return 127, "", str(e)


# ---------------------------------------------------------------------------
# lircd remote/key enumeration (for discovery + the event entity's event_types)
# ---------------------------------------------------------------------------
_KEYLINE = re.compile(r"^[0-9a-fA-F]+\s+(\S+)\s*$")


def enumerate_remotes() -> dict[str, list[str]]:
    """{remote_name: [KEY, ...]} for every remote lircd currently knows."""
    rc, out, _ = sh(["irsend", "LIST", "", ""])
    remotes = [ln.strip() for ln in out.splitlines() if ln.strip()] if rc == 0 else []
    result: dict[str, list[str]] = {}
    for rem in remotes:
        rc, out, _ = sh(["irsend", "LIST", rem, ""])
        if rc != 0:
            continue
        keys = []
        for ln in out.splitlines():
            m = _KEYLINE.match(ln.strip())
            if m:
                keys.append(m.group(1))
        if keys:
            result[rem] = keys
    return result


# ---------------------------------------------------------------------------
# Receiver: read decoded presses from the lircd control socket
# ---------------------------------------------------------------------------
class LircReceiver(threading.Thread):
    """Connects to the lircd control socket and calls on_button() for each press.

    lircd emits one line per event: "<code> <repeat> <symbol> <remote>". We fire
    on the first frame (repeat == 0) as the discrete press; held repeats update
    the 'last' readout but don't spam the event entity.

    Pausable (so a learn can stop lircd without us thrashing reconnects) and
    self-reconnecting (lircd restarts on display/power events)."""

    def __init__(self, sock_path: str, on_button):
        super().__init__(daemon=True)
        self.sock_path = sock_path
        self.on_button = on_button
        self._stop = threading.Event()
        self._resume = threading.Event()
        self._resume.set()
        self._sock: socket.socket | None = None

    def pause(self):
        self._resume.clear()
        s = self._sock
        if s is not None:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass

    def resume(self):
        self._resume.set()

    def stop(self):
        self._stop.set()
        self.pause()

    def run(self):
        while not self._stop.is_set():
            self._resume.wait()
            if self._stop.is_set():
                return
            try:
                self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self._sock.settimeout(5)
                self._sock.connect(self.sock_path)
            except OSError as e:
                if self._resume.is_set():
                    log.debug("lircd socket connect failed: %s (retry)", e)
                time.sleep(1.0)
                continue
            log.info("IR receiver connected to %s", self.sock_path)
            buf = b""
            while not self._stop.is_set() and self._resume.is_set():
                try:
                    chunk = self._sock.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break                       # lircd went away
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    self._handle(line.decode("utf-8", "ignore").strip())
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
            if self._resume.is_set():
                log.info("IR receiver lost lircd; reconnecting")
                time.sleep(0.5)

    def _handle(self, line: str):
        if not line:
            return
        parts = line.split()
        if len(parts) < 4:
            return                              # not a button line (e.g. BEGIN/SIGHUP)
        _code, repeat_hex, symbol, remote = parts[0], parts[1], parts[2], parts[3]
        try:
            repeat = int(repeat_hex, 16)
        except ValueError:
            return
        try:
            self.on_button(remote, symbol, repeat)
        except Exception:
            log.exception("on_button handler failed")


# ---------------------------------------------------------------------------
# Raw capture (mode2) for learning
# ---------------------------------------------------------------------------
_M2 = re.compile(r"^(pulse|space)\s+(\d+)")


def parse_mode2_line(line: str):
    """A single mode2 output line -> ('pulse'|'space', microseconds) or None."""
    m = _M2.match(line.strip())
    return (m.group(1), int(m.group(2))) if m else None


def fold_mode2(edges, gap_end: int = 12000, min_edges: int = 8) -> list[int]:
    """Fold a list of (kind, value) edges into a LIRC raw_codes duration list.

    A burst begins on the first 'pulse' and ends at the first long 'space'
    (>= gap_end) once we've seen at least min_edges. The result starts and ends
    on a pulse (a trailing space is dropped). Returns [] if too short to trust."""
    durations: list[int] = []
    started = False
    for kind, val in edges:
        if not started:
            if kind == "pulse":
                started = True
                durations.append(val)
            continue
        if kind == "space" and val >= gap_end and len(durations) >= min_edges:
            break
        durations.append(val)
    if durations and len(durations) % 2 == 0:
        durations = durations[:-1]
    return durations if len(durations) >= min_edges else []


def capture_raw(device: str, driver: str, timeout: float,
                gap_end: int = 12000, min_edges: int = 8) -> list[int]:
    """Capture ONE IR burst via mode2 and fold it into a raw duration list.
    Requires lircd stopped (exclusive serial access). [] if nothing usable was
    seen before the timeout."""
    try:
        proc = subprocess.Popen(
            ["mode2", "--driver", driver, "--device", device],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("mode2 launch failed: %s", e)
        return []

    if proc.stdout is None:
        proc.terminate()
        return []

    stdout = proc.stdout
    lines: queue.Queue = queue.Queue()

    def _pump() -> None:
        # Read mode2 on a helper thread so a quiet receiver can't block the
        # learn deadline. A select() on the buffered text stream can miss
        # lines already pulled into Python's buffer.
        for line in stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=_pump, daemon=True).start()

    edges: list[tuple[str, int]] = []
    started = False
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log.warning("IR learn timed out after %.1fs with no burst", timeout)
                break
            try:
                line = lines.get(timeout=remaining)
            except queue.Empty:
                log.warning("IR learn timed out after %.1fs with no burst", timeout)
                break
            if line is None:
                break
            e = parse_mode2_line(line)
            if e is None:
                continue
            kind, val = e
            if not started:
                if kind != "pulse":
                    continue
                started = True
            edges.append(e)
            # Stop as soon as the burst clearly ended.
            if kind == "space" and val >= gap_end and len(edges) >= min_edges + 1:
                break
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    return fold_mode2(edges, gap_end, min_edges)


# ---------------------------------------------------------------------------
# Learned-code store: JSON source of truth -> regenerated lircd RAW config
# ---------------------------------------------------------------------------
_SAFE_REMOTE = re.compile(r"[^A-Za-z0-9_-]")


def sanitize_remote(name: str) -> str:
    name = _SAFE_REMOTE.sub("-", name.strip()).strip("-")
    return name or "learned"


def sanitize_key(name: str) -> str:
    name = re.sub(r"\s+", "_", name.strip())
    name = re.sub(r"[^A-Za-z0-9_+-]", "", name)
    return name.upper() or "KEY_UNKNOWN"


class LearnedStore:
    def __init__(self, data_dir: str, conf_dir: str):
        self.data_dir = data_dir
        self.conf_dir = conf_dir
        os.makedirs(data_dir, exist_ok=True)

    def _json_path(self, remote: str) -> str:
        return os.path.join(self.data_dir, f"{remote}.json")

    def _conf_path(self, remote: str) -> str:
        return os.path.join(self.conf_dir, f"learned-{remote}.lircd.conf")

    def add(self, remote: str, key: str, durations: list[int]):
        remote, key = sanitize_remote(remote), sanitize_key(key)
        codes: dict[str, list[int]] = {}
        p = self._json_path(remote)
        if os.path.exists(p):
            try:
                with open(p) as f:
                    codes = json.load(f)
            except (OSError, ValueError):
                codes = {}
        codes[key] = durations
        tmp = p + ".tmp"
        with open(tmp, "w") as f:
            json.dump(codes, f)
        os.replace(tmp, p)
        self._write_conf(remote, codes)
        return remote, key

    def _write_conf(self, remote: str, codes: dict[str, list[int]]):
        lines = [
            "# Auto-generated by Harmonia ir_bridge. Do not edit by hand —",
            f"# source of truth is {self._json_path(remote)}.",
            "begin remote",
            f"  name  {remote}",
            "  flags RAW_CODES",
            "  eps            30",
            "  aeps          100",
            "  gap          108000",
            "      begin raw_codes",
        ]
        for key, durs in codes.items():
            lines.append(f"          name {key}")
            for i in range(0, len(durs), 8):
                lines.append("              " + " ".join(str(d) for d in durs[i:i + 8]))
        lines += ["      end raw_codes", "end remote", ""]
        tmp = self._conf_path(remote) + ".tmp"
        with open(tmp, "w") as f:
            f.write("\n".join(lines))
        os.replace(tmp, self._conf_path(remote))


# ---------------------------------------------------------------------------
# The MQTT bridge
# ---------------------------------------------------------------------------
class IrBridge:
    def __init__(self):
        self.host = env("MQTT_HOST", required=True)
        self.port = int(env("MQTT_PORT", "1883"))
        self.user = env("MQTT_USER", required=True)
        self.password = env("MQTT_PASS", required=True)

        self.sock_path = env("LIRC_SOCKET", "/var/run/lirc/lircd")
        self.device = env("UIRT_DEVICE",
                          "/dev/serial/by-id/usb-FTDI_USB-UIRT-if00-port0")
        self.driver = env("UIRT_DRIVER", "usb_uirt_raw")
        self.learn_timeout = float(env("IR_LEARN_TIMEOUT", "20"))
        self.expose_buttons = env("IR_EXPOSE_KEY_BUTTONS", "1") not in ("0", "false", "False", "")
        self.repeat_cap = int(env("IR_SEND_REPEAT_CAP", "20"))
        self.store = LearnedStore(
            env("IR_LEARNED_DIR", "/var/lib/avr-tv-bridge-ir"),
            env("IR_LEARNED_CONF_DIR", "/etc/lirc/lircd.conf.d"))

        self._stop = threading.Event()
        self._learn_lock = threading.Lock()
        self.remotes: dict[str, list[str]] = {}

        self.receiver = LircReceiver(self.sock_path, self._on_button)

        self._v2 = hasattr(mqtt, "CallbackAPIVersion")
        if self._v2:
            self.client = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                client_id=f"{BASE}-ir-{os.getpid()}", clean_session=True)
        else:
            self.client = mqtt.Client(client_id=f"{BASE}-ir-{os.getpid()}",
                                      clean_session=True)
        self.client.username_pw_set(self.user, self.password)
        self.client.will_set(f"{IR}/availability", "offline", retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    # ---- lifecycle ----
    def _on_connect(self, client, userdata, flags, rc, properties=None):
        failed = getattr(rc, "is_failure", None)
        if (failed if failed is not None else (rc != 0)):
            log.error("MQTT connect failed rc=%s", rc)
            return
        log.info("Connected to MQTT %s:%s", self.host, self.port)
        client.publish(f"{IR}/availability", "online", retain=True)
        self._publish_discovery()
        client.subscribe(f"{IR}/send/set")
        client.subscribe(f"{IR}/learn/set")

    def _on_message(self, client, userdata, msg):
        payload = msg.payload.decode("utf-8", "ignore").strip()
        try:
            if msg.topic == f"{IR}/send/set":
                self._cmd_send(payload)
            elif msg.topic == f"{IR}/learn/set":
                self._cmd_learn(payload)
        except Exception as e:
            log.exception("Error handling %s = %r: %s", msg.topic, payload, e)

    # ---- receive ----
    def _on_button(self, remote: str, symbol: str, repeat: int):
        # Diagnostic readout updates on every frame (incl. holds).
        self.client.publish(f"{IR}/last", f"{remote}/{symbol}")
        self.client.publish(f"{IR}/last/attr",
                            _dumps({"remote": remote, "button": symbol,
                                    "repeat": repeat, "ts": int(time.time())}))
        if repeat == 0:                         # discrete press -> fire event
            log.info("IR recv %s / %s", remote, symbol)
            self.client.publish(f"{IR}/event",
                                _dumps({"event_type": symbol, "remote": remote}))

    # ---- send ----
    @staticmethod
    def _parse_target(payload: str) -> tuple[str, str, int]:
        """Parse a send/learn target. Accepts JSON {"remote","key","repeats"} or
        a shorthand "Remote/KEY", "Remote KEY", optionally with " x3"."""
        remote = key = ""
        repeats = 1
        p = payload.strip()
        if p.startswith("{"):
            d = json.loads(p)
            remote = str(d.get("remote", "")).strip()
            key = str(d.get("key", "")).strip()
            repeats = int(d.get("repeats", 1) or 1)
        else:
            m = re.search(r"\sx(\d+)\s*$", p, re.I)
            if m:
                repeats = int(m.group(1))
                p = p[:m.start()].strip()
            sep = "/" if "/" in p else None
            if sep:
                remote, _, key = p.partition("/")
            else:
                remote, _, key = p.partition(" ")
            remote, key = remote.strip(), key.strip()
        return remote, key, max(1, repeats)

    def _cmd_send(self, payload: str):
        remote, key, repeats = self._parse_target(payload)
        if not remote or not key:
            log.warning("send: could not parse %r", payload)
            return
        repeats = min(repeats, self.repeat_cap)
        log.info("IR send %s/%s x%d", remote, key, repeats)
        # irsend SEND_ONCE accepts multiple key args; send the key N times.
        rc, _out, err = sh(["irsend", "SEND_ONCE", remote] + [key] * repeats)
        if rc != 0:
            log.warning("irsend failed rc=%s: %s", rc, err.strip())

    # ---- learn ----
    def _cmd_learn(self, payload: str):
        remote, key, _ = self._parse_target(payload)
        if not remote or not key:
            self._learn_status("error", "could not parse remote/key")
            return
        if not self._learn_lock.acquire(blocking=False):
            self._learn_status("busy", "a learn is already in progress")
            return
        threading.Thread(target=self._learn_worker, args=(remote, key),
                         daemon=True).start()

    def _learn_worker(self, remote: str, key: str):
        try:
            self._learn_status("arming",
                               f"press '{key}' on the remote, aimed at the USB-UIRT")
            self.receiver.pause()
            sh(["systemctl", "stop", "lircd.socket", "lircd.service"])
            time.sleep(0.5)
            self._learn_status("listening", f"capturing '{key}' ({self.learn_timeout:.0f}s)")
            durations = capture_raw(self.device, self.driver, self.learn_timeout)
            # Bring lircd back no matter what.
            if durations:
                rname, kname = self.store.add(remote, key, durations)
            sh(["systemctl", "start", "lircd.socket", "lircd.service"])
            time.sleep(1.0)
            self.receiver.resume()
            self._kick_irexec()
            if not durations:
                self._learn_status("failed", "no IR captured — try again, aim at the receiver")
                return
            self.remotes = enumerate_remotes()
            self._publish_discovery()
            self._learn_status("learned",
                               f"{rname}/{kname} captured ({len(durations)} edges)")
            log.info("learned %s/%s (%d edges)", rname, kname, len(durations))
        except Exception as e:
            log.exception("learn failed: %s", e)
            sh(["systemctl", "start", "lircd.socket", "lircd.service"])
            self.receiver.resume()
            self._learn_status("error", str(e))
        finally:
            self._learn_lock.release()

    def _kick_irexec(self):
        """Best-effort: bounce jdnla's user irexec service so the physical
        remote reconnects to the freshly-restarted lircd (irexec is Restart=
        on-failure and may not auto-reconnect on a clean EOF)."""
        uid = env("IREXEC_UID", "1000")
        user = env("IREXEC_USER", "jdnla")
        sh(["su", "-", user, "-c",
            f"XDG_RUNTIME_DIR=/run/user/{uid} systemctl --user restart irexec.service"],
           timeout=10)

    def _learn_status(self, state: str, detail: str = ""):
        self.client.publish(f"{IR}/learn/status", state, retain=True)
        self.client.publish(f"{IR}/learn/status/attr",
                            _dumps({"state": state, "detail": detail,
                                    "ts": int(time.time())}), retain=True)
        if detail:
            log.info("learn[%s]: %s", state, detail)

    # ---- discovery ----
    def _publish_discovery(self):
        self.remotes = self.remotes or enumerate_remotes()
        avail = [{"topic": f"{IR}/availability"}]
        all_keys = sorted({k for ks in self.remotes.values() for k in ks})
        entities: list[tuple[str, dict]] = []

        def cfg(kind, eid):
            return f"{_PREFIX}/{kind}/{DEVICE_ID}/{eid}/config"

        # Received-press event entity (automation triggers + last event).
        # No value_template: MQTT event entities expect a JSON object with an
        # `event_type` key. Rendering only the key name (`{{ value_json.event_type }}`)
        # is not valid event payload JSON, so HA discards it and the entity stays
        # `unknown` with `event_type: null`.
        entities.append((cfg("event", "received"), {
            "name": "IR Received",
            "unique_id": f"{DEVICE_ID}_received",
            "state_topic": f"{IR}/event",
            "event_types": all_keys or ["KEY_UNKNOWN"],
            "json_attributes_topic": f"{IR}/event",
            "availability": avail, "device": IR_DEVICE, "icon": "mdi:remote",
        }))
        # Last-received diagnostic sensor.
        entities.append((cfg("sensor", "last"), {
            "name": "Last IR Code",
            "unique_id": f"{DEVICE_ID}_last",
            "state_topic": f"{IR}/last",
            "json_attributes_topic": f"{IR}/last/attr",
            "availability": avail, "device": IR_DEVICE,
            "icon": "mdi:remote", "entity_category": "diagnostic",
        }))
        # Learn status diagnostic sensor.
        entities.append((cfg("sensor", "learn_status"), {
            "name": "IR Learn Status",
            "unique_id": f"{DEVICE_ID}_learn_status",
            "state_topic": f"{IR}/learn/status",
            "json_attributes_topic": f"{IR}/learn/status/attr",
            "availability": avail, "device": IR_DEVICE,
            "icon": "mdi:school", "entity_category": "diagnostic",
        }))
        # Direct send: type "Remote/KEY" (or "Remote/KEY x3").
        entities.append((cfg("text", "send"), {
            "name": "IR Send (Remote/KEY)",
            "unique_id": f"{DEVICE_ID}_send",
            "command_topic": f"{IR}/send/set",
            "availability": avail, "device": IR_DEVICE, "icon": "mdi:remote-tv",
        }))
        # Learn: type "Remote/KEY" then press the physical remote.
        entities.append((cfg("text", "learn"), {
            "name": "IR Learn (Remote/KEY)",
            "unique_id": f"{DEVICE_ID}_learn",
            "command_topic": f"{IR}/learn/set",
            "availability": avail, "device": IR_DEVICE, "icon": "mdi:school-outline",
        }))
        # Tap-to-send button per known key.
        if self.expose_buttons:
            for remote, keys in sorted(self.remotes.items()):
                for key in keys:
                    slug = _SAFE_REMOTE.sub("_", f"{remote}_{key}")
                    entities.append((cfg("button", f"snd_{slug}"), {
                        "name": f"IR {remote} {key}",
                        "unique_id": f"{DEVICE_ID}_snd_{slug}",
                        "command_topic": f"{IR}/send/set",
                        "payload_press": f"{remote}/{key}",
                        "availability": avail, "device": IR_DEVICE,
                        "icon": "mdi:gesture-tap-button",
                        "entity_category": "config",
                    }))

        for topic, payload in entities:
            self.client.publish(topic, _dumps(payload), retain=True)
        log.info("Published IR discovery: %d entities (%d remotes, %d keys)",
                 len(entities), len(self.remotes), len(all_keys))

    # ---- run ----
    def run(self):
        signal.signal(signal.SIGTERM, lambda *_: self._stop.set())
        signal.signal(signal.SIGINT, lambda *_: self._stop.set())
        self.remotes = enumerate_remotes()
        self.receiver.start()
        while not self._stop.is_set():
            try:
                self.client.connect(self.host, self.port, keepalive=60)
                self.client.loop_start()
                break
            except OSError as e:
                log.warning("MQTT connect retry: %s", e)
                time.sleep(3)
        while not self._stop.is_set():
            time.sleep(0.5)
        log.info("Stopping IR bridge")
        self.receiver.stop()
        try:
            self.client.publish(f"{IR}/availability", "offline", retain=True)
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    IrBridge().run()
