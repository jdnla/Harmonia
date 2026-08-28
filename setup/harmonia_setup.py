#!/usr/bin/env python3
"""Harmonia interactive setup wizard.

Goal: turn the manual "edit config.env by hand, find IPs, run the pairing script,
guess the speaker layout" steps into one guided flow. Autonomy policy = safe/
reversible auto, confirm anything else:

  * Auto (no prompt): SSDP scan, reading device state, detecting the speaker
    layout from the receiver, backing up config before writing.
  * Confirm first: writing config.env, pairing the TV (on-screen PIN), enabling/
    starting services.

Run it after install.sh (which lays down packages + the venv). It uses the
driver registry's discover() hooks, so any driver that implements discovery
shows up here automatically — no wizard changes needed to support new hardware.

    python3 setup/harmonia_setup.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
sys.path.insert(0, SRC)
sys.path.insert(0, HERE)

import harmonia_common as hc  # noqa: E402

TEMPLATE = os.path.join(os.path.dirname(HERE), "config.env.example")


def _load_drivers():
    try:
        import drivers  # noqa: F401  (registers via import)
        from drivers import registry
        from drivers.base import Device  # noqa: F401
        return registry
    except Exception as e:
        hc.bad(f"could not load driver registry: {e}")
        hc.info("Run install.sh first (it creates the venv with deps), then run "
                "this with that venv's python:")
        hc.info("  /opt/avr-tv-bridge/venv/bin/python setup/harmonia_setup.py")
        sys.exit(2)


def step_dependencies():
    hc.head("1. Checking dependencies")
    # We don't yet know the final config, so check the common + likely-needed.
    checks = hc.check_dependencies(need_cec=True, need_ir=False, need_audio=True)
    missing = [(l, hint) for (l, present, hint) in checks if not present]
    for label, present, hint in checks:
        (hc.ok if present else hc.warn)(
            label if present else f"{label} — missing: {hint}")
    if missing:
        hc.info("Missing tools are only needed for the features that use them "
                "(CEC display, IR remote, audio channel-sync). Install the ones "
                "you need with the commands above; the wizard continues either "
                "way.")


def step_discover(registry):
    hc.head("2. Discovering devices on your network")
    hc.info("SSDP scan (~3s)…")
    ssdp = hc.ssdp_scan(timeout=3.0)
    hc.info(f"got {len(ssdp)} SSDP responses; asking each driver to identify "
            "candidates")

    # Ask every registered receiver + display driver to filter the SSDP results.
    from drivers.registry import _RECEIVERS, _DISPLAYS  # internal tables
    candidates = []
    for table in (_RECEIVERS, _DISPLAYS):
        for name, cls in table.items():
            try:
                candidates += cls.discover(ssdp)
            except Exception as e:
                hc.info(f"driver {name}.discover() error: {e}")

    # Dedupe by (driver, host)
    uniq = {}
    for d in candidates:
        uniq[(d.driver_name, d.host)] = d
    return list(uniq.values())


def step_choose(candidates):
    hc.head("3. Confirm devices")
    if not candidates:
        hc.warn("No devices auto-discovered. You can enter them manually.")
        return _manual_entry()

    chosen = {"receiver": None, "display": None}
    for role in ("receiver", "display"):
        opts = [d for d in candidates if d.role == role]
        if not opts:
            continue
        hc.info(f"{role}s found:")
        for i, d in enumerate(opts, 1):
            print(f"      {i}) {d.label}")
        print(f"      0) none / skip")
        sel = hc.ask(f"Pick a {role}", "1")
        try:
            idx = int(sel)
        except ValueError:
            idx = 1
        if idx == 0:
            continue
        if 1 <= idx <= len(opts):
            chosen[role] = opts[idx - 1]
    return chosen


def _manual_entry():
    chosen = {"receiver": None, "display": None}
    from drivers.base import DiscoveredDevice
    if hc.confirm("Add a receiver manually?", default=True):
        drv = hc.ask("receiver driver (denon/none)", "denon")
        if drv not in ("", "none"):
            host = hc.ask("receiver IP")
            if host:
                chosen["receiver"] = DiscoveredDevice(
                    drv, "receiver", host, f"{drv} @ {host}",
                    {"HARMONIA_RECEIVER": drv, "DENON_HOST": host})
    if hc.confirm("Add a display manually?", default=True):
        drv = hc.ask("display driver (vizio/cec/none)", "vizio")
        if drv not in ("", "none"):
            host = hc.ask("display IP")
            env = {"HARMONIA_DISPLAY": drv}
            if drv == "vizio":
                env["VIZIO_HOST"] = host
            chosen["display"] = DiscoveredDevice(
                drv, "display", host, f"{drv} @ {host}", env,
                followup="pair" if drv == "vizio" else "")
    return chosen


def step_speaker_layout(chosen):
    hc.head("4. Detecting speaker layout")
    recv = chosen.get("receiver")
    if not recv or recv.driver_name != "denon":
        hc.info("No Denon receiver — defaulting SPEAKER_CHANNELS by prompt.")
        ch = hc.ask("Speaker channels (2=stereo/TV, 6=5.1, 8=7.1)", "2")
        return {"SPEAKER_CHANNELS": ch}
    # Ask the Denon its speaker config over telnet (safe read).
    try:
        import socket as _s
        import time as _t
        def txn(cmds, rt=1.5):
            out = []
            c = _s.create_connection((recv.host, 23), 2); c.settimeout(2)
            for cmd in cmds:
                c.sendall((cmd + "\r").encode()); _t.sleep(0.08)
            dl = _t.time() + rt; buf = b""
            while _t.time() < dl:
                try:
                    c.settimeout(max(0.05, dl - _t.time())); ch = c.recv(1024)
                    if not ch:
                        break
                    buf += ch
                except Exception:
                    break
            c.close()
            return [l.strip() for l in buf.decode("ascii", "ignore").split("\r")
                    if l.strip()]
        lines = txn(["SSSPC ?"])
        conf = {l[:8]: l.split()[-1] for l in lines if l.startswith("SSSPC")}
        has_center = conf.get("SSSPCCEN") == "YES"
        has_sub = conf.get("SSSPCSWF") == "YES"
        has_back = conf.get("SSSPCSBK", "NO") not in ("NO",)
        has_surr = conf.get("SSSPCSUA") == "YES"
        # Map to the HDMI LPCM channel TIER needed, not the physical speaker
        # count. HDMI layouts are fixed: 7.1=8ch (FL FR C LFE SL SR SBL SBR),
        # 5.1=6ch (FL FR C LFE SL SR — NO surround-back), 2.0=2ch. Surround-back
        # speakers ONLY exist in the 8ch tier, so if the AVR has back speakers we
        # must drive 8ch even without a center/sub (the AVR folds those). Likewise
        # any surround needs at least the 6ch tier.
        if has_back:
            channels = 8
        elif has_surr:
            channels = 6
        else:
            channels = 2
        hc.ok(f"Denon reports: center={has_center} sub={has_sub} "
              f"surround={has_surr} surround_back={has_back} "
              f"-> SPEAKER_CHANNELS={channels}")
        if channels == 8 and not has_center:
            hc.info("Surround-back speakers need the 7.1 (8ch) profile; with no "
                    "center speaker the receiver folds the phantom center into "
                    "front L/R (Harmonia sends the standard 8ch layout).")
        elif not has_center and channels >= 6:
            hc.info("No center speaker: the receiver folds the phantom center "
                    "into front L/R.")
        return {"SPEAKER_CHANNELS": str(channels)}
    except Exception as e:
        hc.warn(f"could not read Denon speaker config ({e}); prompting")
        ch = hc.ask("Speaker channels (2/6/8)", "8")
        return {"SPEAKER_CHANNELS": ch}


def step_mqtt():
    hc.head("5. MQTT broker (Home Assistant)")
    existing = hc.read_env_file()
    env = {}
    env["MQTT_HOST"] = hc.ask("MQTT host", existing.get("MQTT_HOST", "192.168.1.7"))
    env["MQTT_PORT"] = hc.ask("MQTT port", existing.get("MQTT_PORT", "1883"))
    env["MQTT_USER"] = hc.ask("MQTT user", existing.get("MQTT_USER", "harmonia"))
    env["MQTT_PASS"] = hc.ask("MQTT password", existing.get("MQTT_PASS", ""))
    return env


def step_write(all_env):
    hc.head("6. Writing config")
    hc.info("Config will be written to " + hc.CONFIG_ENV
            + " (a .bak of the previous file is kept).")
    for k, v in all_env.items():
        shown = "••••" if "PASS" in k and v else v
        print(f"      {k}={shown}")
    if not hc.confirm("Write these settings?", default=True):
        hc.warn("Skipped writing config.")
        return False
    hc.merge_env(all_env, template=TEMPLATE)
    hc.ok("config.env written.")
    return True


def step_pair(chosen):
    disp = chosen.get("display")
    if not disp or disp.followup != "pair":
        return
    hc.head("7. Pair the TV")
    hc.info("Vizio SmartCast needs a one-time PIN pairing.")
    if not hc.confirm(f"Pair with {disp.host} now?", default=True):
        hc.info("You can pair later: "
                "python3 setup/pair_vizio.py " + disp.host)
        return
    pair_script = os.path.join(HERE, "pair_vizio.py")
    os.system(f'{sys.executable} "{pair_script}" {disp.host}')


def main():
    print(hc.c("\nHarmonia setup wizard", "bold"))
    print(hc.c("Safe/reversible steps run automatically; anything that changes "
               "config, pairs, or touches services asks first.\n", "dim"))
    registry = _load_drivers()
    step_dependencies()
    candidates = step_discover(registry)
    chosen = step_choose(candidates)

    all_env = {}
    for role in ("receiver", "display"):
        d = chosen.get(role)
        if d:
            all_env.update(d.env_updates)
            hc.ok(f"{role}: {d.label}")
        else:
            # Explicitly record "none" so the bridge omits the role.
            key = "HARMONIA_RECEIVER" if role == "receiver" else "HARMONIA_DISPLAY"
            all_env[key] = "none"
            hc.info(f"{role}: none")

    all_env.update(step_speaker_layout(chosen))
    all_env.update(step_mqtt())
    wrote = step_write(all_env)
    if wrote:
        step_pair(chosen)
        hc.head("Done")
        hc.info("Start the bridge:  sudo systemctl restart avr-tv-bridge.service")
        hc.info("Check health:      python3 setup/harmonia_doctor.py")


if __name__ == "__main__":
    main()
