#!/usr/bin/env python3
"""Harmonia doctor — read-only health check.

Diagnoses the common silent failure points and prints a specific fix for each
red line. It never changes anything (no restarts, no config edits, no pairing) —
per the autonomy policy, resolution is offered as instructions, not performed.

    python3 setup/harmonia_doctor.py
    /opt/avr-tv-bridge/venv/bin/python setup/harmonia_doctor.py   # full checks

Exit code 0 if all green, 1 if any check failed.
"""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
sys.path.insert(0, SRC)
sys.path.insert(0, HERE)

import harmonia_common as hc  # noqa: E402

FAILED = 0


def fail():
    global FAILED
    FAILED += 1


def check_services():
    hc.head("Services")
    units = [
        ("avr-tv-bridge.service", "system"),
        ("avr-tv-bridge-launcher.service", "user"),
        ("avr-tv-bridge-cecnav.service", "user"),
    ]
    for unit, scope in units:
        cmd = (["systemctl", "is-active", unit] if scope == "system"
               else ["systemctl", "--user", "is-active", unit])
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            state = r.stdout.strip()
        except Exception as e:
            state = f"error: {e}"
        if state == "active":
            hc.ok(f"{unit} ({scope}) active")
        else:
            hc.bad(f"{unit} ({scope}) is {state}")
            restart = ("sudo systemctl restart " + unit if scope == "system"
                       else "systemctl --user restart " + unit)
            hc.info(f"fix: {restart}   (logs: journalctl "
                    f"{'' if scope=='system' else '--user '}-u {unit} -e)")
            fail()


def check_config():
    hc.head("Config")
    env = hc.read_env_file()
    if not env:
        hc.bad(f"no config at {hc.CONFIG_ENV}")
        hc.info("fix: run  python3 setup/harmonia_setup.py")
        fail()
        return env
    hc.ok(f"config.env present ({len(env)} keys)")
    # Required basics
    for k in ("MQTT_HOST", "MQTT_USER", "MQTT_PASS"):
        if not env.get(k) or env.get(k) in ("change-me", ""):
            hc.bad(f"{k} not set / placeholder")
            hc.info("fix: python3 setup/harmonia_setup.py  (or edit config.env)")
            fail()
    return env


def check_mqtt(env):
    hc.head("MQTT broker")
    host = env.get("MQTT_HOST"); port = int(env.get("MQTT_PORT", "1883") or 1883)
    if not host:
        hc.bad("MQTT_HOST unset"); fail(); return
    if not hc.port_open(host, port, timeout=3):
        hc.bad(f"cannot reach {host}:{port}")
        hc.info("fix: check the broker is running and reachable from this host")
        fail(); return
    hc.ok(f"{host}:{port} reachable (TCP)")
    # Try a real auth+publish if paho is available (venv python).
    try:
        import paho.mqtt.client as mqtt
        cli = (mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
               if hasattr(mqtt, "CallbackAPIVersion") else mqtt.Client())
        cli.username_pw_set(env.get("MQTT_USER", ""), env.get("MQTT_PASS", ""))
        cli.connect(host, port, keepalive=5)
        cli.disconnect()
        hc.ok("MQTT auth OK")
    except ModuleNotFoundError:
        hc.info("paho not importable — run with the venv python for the auth "
                "check: /opt/avr-tv-bridge/venv/bin/python setup/harmonia_doctor.py")
    except Exception as e:
        hc.bad(f"MQTT auth/connect failed: {e}")
        hc.info("fix: verify MQTT_USER/MQTT_PASS in config.env")
        fail()


def check_devices(env):
    hc.head("Devices")
    try:
        import drivers  # noqa: F401
        from drivers.registry import get_receiver, get_display
    except Exception as e:
        hc.info(f"driver registry unavailable ({e}); skipping device checks. "
                "Run with the venv python for full checks.")
        return

    def envget(key, default=None, required=False):
        return env.get(key, default)

    for role, getter, name_key in (
            ("receiver", get_receiver, "HARMONIA_RECEIVER"),
            ("display", get_display, "HARMONIA_DISPLAY")):
        drv_name = env.get(name_key, "denon" if role == "receiver" else "vizio")
        try:
            cls = getter(drv_name)
        except KeyError as e:
            hc.bad(f"{role}: {e}"); fail(); continue
        if cls is None:
            hc.info(f"{role}: none (skipped)")
            continue
        try:
            good, detail = cls.selftest(envget)
        except Exception as e:
            good, detail = False, f"selftest error: {e}"
        if good:
            hc.ok(f"{role} [{drv_name}]: {detail}")
        else:
            hc.bad(f"{role} [{drv_name}]: {detail}")
            hc.info("fix: check the device IP/power and config.env; for Vizio "
                    "re-pair with setup/pair_vizio.py")
            fail()


def check_audio(env):
    hc.head("Audio / channel-sync")
    if env.get("CHANNEL_SYNC_ENABLE", "1") in ("0", "false", "False", ""):
        hc.info("channel-sync disabled in config; skipping")
        return
    if not hc.have_cmd("pactl"):
        hc.bad("pactl not found (PipeWire/Pulse) — channel-sync can't run")
        hc.info("fix: apt install pipewire-pulse  (or pulseaudio-utils)")
        fail(); return
    try:
        out = subprocess.run(["pactl", "list", "sinks", "short"],
                             capture_output=True, text=True, timeout=6).stdout
    except Exception as e:
        hc.bad(f"pactl failed: {e}"); fail(); return
    hdmi = [l for l in out.splitlines() if "hdmi" in l.lower()]
    if not hdmi:
        hc.warn("no HDMI sink active (receiver/TV off?)")
        return
    for l in hdmi:
        cols = l.split()
        spec = " ".join(cols[3:6]) if len(cols) >= 6 else l
        cap = int(env.get("SPEAKER_CHANNELS", "8") or 8)
        hc.ok(f"HDMI sink: {cols[1] if len(cols)>1 else l}")
        hc.info(f"  format: {spec}   (SPEAKER_CHANNELS cap = {cap})")


def check_dpad_extension():
    hc.head("Chrome D-pad extension")
    ext_id = "jjfoinfehejcchijlfaikgcdmhmnjmhh"
    crx = "/opt/harmonia-dpad-ext/harmonia-dpad.crx"
    pol = "/etc/opt/chrome/policies/managed/harmonia-dpad.json"
    if not (hc.have_cmd("google-chrome") or hc.have_cmd("google-chrome-stable")):
        hc.info("Chrome not installed; D-pad extension not applicable.")
        return
    ok_all = True
    if os.path.exists(crx):
        hc.ok("packed extension present")
    else:
        hc.bad(f"missing {crx}")
        hc.info("fix: re-run install.sh (packs + force-installs the extension)")
        ok_all = False
        fail()
    if os.path.exists(pol) and ext_id in open(pol).read():
        hc.ok("force-install policy present")
    else:
        hc.bad(f"missing/incomplete policy {pol}")
        hc.info("fix: re-run install.sh")
        ok_all = False
        fail()
    if ok_all:
        hc.info("Netflix/Sportsnet/TSN get D-pad spatial navigation. Verify in a "
                "real Chrome at chrome://extensions (installed by policy).")


def main():
    print(hc.c("\nHarmonia doctor — read-only diagnostics\n", "bold"))
    if os.geteuid() == 0:
        hc.warn("Running as root. Harmonia's config lives in the bridge user's "
                "home and the launcher/cecnav are --user services, so run this "
                "as that user (not sudo) for accurate results:")
        hc.info("  /opt/avr-tv-bridge/venv/bin/python setup/harmonia_doctor.py")
    env = check_config()
    check_services()
    if env:
        check_mqtt(env)
        check_devices(env)
        check_audio(env)
    check_dpad_extension()
    hc.head("Summary")
    if FAILED == 0:
        hc.ok("All checks passed.")
        sys.exit(0)
    hc.bad(f"{FAILED} check(s) failed — see fixes above.")
    sys.exit(1)


if __name__ == "__main__":
    main()
