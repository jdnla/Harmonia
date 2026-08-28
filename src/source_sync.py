"""Ensure the TV + AVR are on the right input when launching an app.

When you launch an app via a remote number button, the TV or AVR may be on the
wrong source (e.g. someone switched the AVR to CD, or the TV to the tuner). This
nudges them back to the input that app expects — but ONLY when a device is
actually powered on and on the WRONG input. It never changes power, and it
publishes nothing when things are already correct, so it can't hammer devices or
fight the power-sync watchdog.

It talks to the running avr-tv-bridge over MQTT: the bridge publishes each
device's current input/power as RETAINED topics (avr-tv-bridge/<dev>/input/state,
/power/state) and accepts input changes on avr-tv-bridge/<dev>/input/set. We read
the retained state and, if wrong, publish a correction.

Implementation note: this uses the `mosquitto_sub`/`mosquitto_pub` CLIs via
subprocess rather than an in-process MQTT client. An in-process paho client
proved unreliable at delivering retained messages when called from the launcher
(a background loop thread would connect and subscribe but not surface the
retained payloads within the settle window). The CLIs are rock-solid for this
one-shot read/write pattern and already a project dependency (lircd aside, the
bridge host has them). If the CLIs are missing, this degrades silently — a launch
must never fail because source-sync couldn't run.

Config (MQTT creds) comes from ~/.config/avr-tv-bridge/config.env (same file the
bridge/launcher-service use).
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading

log = logging.getLogger("source_sync")

BASE = "avr-tv-bridge"


def _load_config_env() -> None:
    """Best-effort load of config.env into os.environ if MQTT_HOST isn't set.

    The launcher SERVICE gets these via systemd EnvironmentFile, but the CLI
    path (irexec number buttons) does not, so read the file ourselves.
    """
    if os.environ.get("MQTT_HOST"):
        return
    path = os.path.expanduser("~/.config/avr-tv-bridge/config.env")
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    except OSError:
        pass


def _mqtt_common(host: str, port: str, user: str, pw: str) -> list[str]:
    args = ["-h", host, "-p", port]
    if user:
        args += ["-u", user]
    if pw:
        args += ["-P", pw]
    return args


def _read_retained(common: list[str], topic: str, timeout: float) -> str | None:
    """Read a single retained message from `topic`, or None if none/timeout."""
    try:
        r = subprocess.run(
            ["mosquitto_sub", *common, "-t", topic, "-C", "1",
             "-W", str(max(1, int(timeout)))],
            capture_output=True, text=True, timeout=timeout + 2)
        out = r.stdout.strip()
        return out if out else None
    except (OSError, subprocess.SubprocessError):
        return None


def ensure_source(source: dict, timeout: float = 4.0) -> None:
    """Correct TV/AVR inputs to match `source` if (and only if) they're wrong.

    `source` may contain any of: {"avr": "<denon code>", "tv": "<vizio input>"}.
    Missing keys are left untouched. Runs synchronously; use ensure_source_async
    to avoid blocking.
    """
    if not source:
        return
    _load_config_env()
    host = os.environ.get("MQTT_HOST")
    if not host:
        log.debug("no MQTT_HOST; skipping source sync")
        return
    port = os.environ.get("MQTT_PORT", "1883")
    user = os.environ.get("MQTT_USER", "")
    pw = os.environ.get("MQTT_PASS", "")
    common = _mqtt_common(host, port, user, pw)

    # Map generic keys -> bridge device ids.
    wanted: dict[str, str] = {}
    if source.get("avr"):
        wanted["denon"] = source["avr"]
    if source.get("tv"):
        wanted["vizio"] = source["tv"]
    if not wanted:
        return

    # Read each device's retained power + input, then correct only if on and
    # wrong. Per-topic read timeout kept short so the whole thing stays snappy.
    per = max(1.5, timeout / (len(wanted) * 2))
    for dev, want in wanted.items():
        power = _read_retained(common, f"{BASE}/{dev}/power/state", per)
        if power != "ON":
            log.info("source-sync: %s not ON (%s) — leaving input alone",
                     dev, power)
            continue
        cur = _read_retained(common, f"{BASE}/{dev}/input/state", per)
        if cur is None:
            log.info("source-sync: %s input unknown — skipping", dev)
            continue
        if cur == want:
            log.info("source-sync: %s already on %s", dev, want)
            continue
        log.info("source-sync: %s on %s, correcting to %s", dev, cur, want)
        try:
            subprocess.run(
                ["mosquitto_pub", *common, "-t", f"{BASE}/{dev}/input/set",
                 "-m", want],
                capture_output=True, timeout=5)
        except (OSError, subprocess.SubprocessError) as e:
            log.debug("source-sync: publish for %s failed: %s", dev, e)


def ensure_source_async(source: dict, timeout: float = 4.0) -> threading.Thread:
    """Run ensure_source in a daemon thread so launching stays snappy."""
    t = threading.Thread(target=ensure_source, args=(source, timeout), daemon=True)
    t.start()
    return t
