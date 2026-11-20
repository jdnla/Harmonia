#!/usr/bin/env python3
"""Generic device tooling for Harmonia.

Commands dispatch to the configured display or receiver driver rather than to a
brand-specific client:

  list                            Show configured drivers and the registry.
  pair [host]                     One-time pairing through the selected driver;
                                  --save writes the driver and returned values
                                  into config.env.
  state [--role R] [--driver D]   Read normalized device state.
  profile [--role R] [--driver D] Read driver diagnostics (display-oriented).
  inputs [--role R] [--driver D]  List named inputs and visibility.
  name-input --input I --name N   Show/plan an input rename; --apply writes
                                  only when the driver marks writes as safe.
  hide-input --input I [--apply]
  show-input --input I [--apply]
  key <sequence...>               Send a driver-defined key sequence.

The driver comes from HARMONIA_DISPLAY / HARMONIA_RECEIVER in config.env, or
from --driver. Brand details live in src/drivers/<name>/; this tool
only calls the optional hooks documented on drivers.base.Device. Add a new TV
or receiver by registering a driver, not by editing this CLI.

Examples:
    harmonia pair --driver vizio vizio.example --save
    harmonia device list
    harmonia device profile --driver vizio
    harmonia device inputs
    harmonia device hide-input --input HDMI-3 --apply
    harmonia device key "MENU 1 9 9 9"
    harmonia device state --role receiver
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")
if not os.path.isdir(SRC):
    SRC = ROOT
sys.path.insert(0, SRC)
sys.path.insert(0, HERE)

import harmonia_common as hc  # noqa: E402

# Importing the package registers every driver module in src/drivers/.
import drivers  # noqa: F401,E402
from drivers.registry import available, get_display, get_receiver  # noqa: E402

ROLES = {
    "display": ("HARMONIA_DISPLAY", "vizio", get_display),
    "receiver": ("HARMONIA_RECEIVER", "denon", get_receiver),
}


def _envget(env):
    def get(key, default=None, required=False):
        value = env.get(key, default)
        if required and not value:
            raise RuntimeError(f"{key} is required in config.env")
        return value
    return get


def _class_for(role: str, driver_name: str | None):
    role = role or "display"
    key, default, getter = ROLES[role]
    env = hc.read_env_file()
    name = driver_name or env.get(key, default)
    try:
        cls = getter(name)
    except KeyError as e:
        hc.bad(str(e))
        raise SystemExit(2)
    if cls is None:
        hc.bad(f"{role} is disabled ({key}={name})")
        raise SystemExit(2)
    return cls, name, role, env


def _select(role: str, driver_name: str | None):
    cls, name, role, env = _class_for(role, driver_name)
    try:
        return cls.from_env(_envget(env)), name, role
    except Exception as e:
        hc.bad(f"cannot initialize {role} driver {name!r}: {e}")
        raise SystemExit(2)


def _call(driver, name: str):
    """Return a bound optional hook or None when the driver lacks it."""
    fn = getattr(driver, name, None)
    return fn if callable(fn) else None


def _run(fn, *args):
    try:
        return fn(*args)
    except NotImplementedError as e:
        hc.bad(str(e) or "driver does not implement this command")
        raise SystemExit(2)
    except Exception as e:
        hc.bad(str(e))
        raise SystemExit(1)


def _fmt(value) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    return str(value)


def cmd_list(_args) -> int:
    env = hc.read_env_file()
    hc.head("Configured devices")
    for role, (key, default, getter) in ROLES.items():
        name = env.get(key, default)
        try:
            cls = getter(name)
        except KeyError:
            cls = None
        state = "disabled" if cls is None else "registered"
        caps = ",".join(getattr(cls, "capabilities", ()) or ()) if cls else "-"
        print(f"  {role:10} {name:16} {state:10} caps=[{caps}]  ({key})")

    hc.head("Registered drivers")
    catalog = available()
    for role in ("displays", "receivers", "audio"):
        print(f"  {role:10} {', '.join(catalog.get(role, [])) or '(none)'}")
    return 0


def cmd_pair(args) -> int:
    cls, name, role, env = _class_for(args.role, args.driver)
    fn = getattr(cls, "pair", None)
    from drivers.base import Device
    if not callable(fn) or getattr(fn, "__func__", None) is Device.pair:
        hc.bad(f"driver {name!r} does not support pairing")
        return 2
    updates = _run(fn, _envget(env), args.host, args.token_file) or {}
    if args.save:
        key = "HARMONIA_DISPLAY" if role == "display" else "HARMONIA_RECEIVER"
        values = {key: name}
        values.update(updates)
        hc.merge_env(values)
        hc.ok(f"selected {role} driver {name!r} in config.env")
    hc.ok(f"paired {role} driver {name!r}")
    return 0


def cmd_state(args) -> int:
    driver, name, role = _select(args.role, args.driver)
    state = _run(driver.get_state)
    if args.json:
        print(json.dumps(state, indent=2, sort_keys=True))
    else:
        hc.head(f"{role} [{name}] state")
        for key in sorted(state):
            print(f"  {key:20} {_fmt(state[key])}")
    return 0 if state.get("available", True) else 1


def cmd_profile(args) -> int:
    driver, name, role = _select(args.role, args.driver)
    fn = _call(driver, "get_profile")
    from drivers.base import Device
    if fn is not None and getattr(fn, "__func__", None) is Device.get_profile:
        fn = _call(driver, "get_tv_profile") or fn
    if fn is None:
        hc.bad(f"driver {name!r} does not expose a profile")
        return 2
    profile = _run(fn)
    if args.json:
        print(json.dumps(profile, indent=2, sort_keys=True))
        return 0 if profile.get("available", True) else 1
    hc.head(f"{role} [{name}] profile")
    for key in sorted(profile):
        print(f"  {key:20} {_fmt(profile[key])}")
    return 0 if profile.get("available", True) else 1


def _named_inputs(driver, name):
    fn = _call(driver, "list_named_inputs")
    if fn is None:
        hc.bad(f"driver {name!r} does not expose named inputs")
        return None
    return _run(fn)


def cmd_inputs(args) -> int:
    driver, name, role = _select(args.role, args.driver)
    names = _named_inputs(driver, name)
    if names is None:
        return 2
    visibility = {}
    vis_fn = _call(driver, "get_input_visibility")
    if vis_fn is not None:
        try:
            visibility = vis_fn() or {}
        except NotImplementedError:
            visibility = {}
        except Exception as e:
            hc.bad(str(e))
            return 1
    cname_fn = _call(driver, "input_cname")
    hc.head(f"{role} [{name}] inputs")
    for hardware, friendly in names:
        cname = cname_fn(hardware) if cname_fn else hardware
        print(f"  {hardware:12} {friendly:24} {visibility.get(cname, '?')}")
    if names and all(friendly == hardware for hardware, friendly in names):
        hc.info("No input has a friendly/CEC name. Rename the used input in the "
                "TV UI and point SCENE_TV_INPUT_MATCH at that label (or keep "
                "SCENE_TV_INPUT as the fallback).")
    return 0


def cmd_name_input(args) -> int:
    driver, name, role = _select(args.role, args.driver)
    names = _named_inputs(driver, name)
    if names is None:
        return 2
    hc.head(f"{role} [{name}] input names")
    for hardware, friendly in names:
        print(f"  {hardware:12} {friendly}")

    if not args.apply:
        hc.info(f"would set {args.input} -> {args.name!r}; add --apply to write")
        return 0
    if not getattr(driver, "input_name_writes_safe", True):
        hint = getattr(driver, "input_name_hint", "")
        hc.warn(hint or f"driver {name!r} reports input-name writes are unsafe")
        return 1
    fn = _call(driver, "set_input_name")
    if fn is None:
        hc.bad(f"driver {name!r} does not implement input-name writes")
        return 2
    if not _run(fn, args.input, args.name):
        hc.bad("write/readback failed")
        return 1
    hc.ok(f"set {args.input} -> {args.name!r}")
    return 0


def cmd_visibility(args, visible: bool) -> int:
    driver, name, role = _select(args.role, args.driver)
    if not args.apply:
        hc.info(f"would set {args.input} -> {'Visible' if visible else 'Hidden'}; "
                "add --apply to write")
        return 0
    fn = _call(driver, "set_input_visibility")
    if fn is None:
        hc.bad(f"driver {name!r} does not implement input visibility")
        return 2
    if not _run(fn, args.input, visible):
        hc.bad("write/readback failed")
        return 1
    hc.ok(f"set {args.input} -> {'Visible' if visible else 'Hidden'}")
    return 0


def cmd_key(args) -> int:
    driver, name, _role = _select(args.role, args.driver)
    sequence = " ".join(args.sequence)
    fn = _call(driver, "send_key_sequence")
    if fn is None:
        hc.bad(f"driver {name!r} does not implement key sequences")
        return 2
    if not _run(fn, sequence):
        hc.bad(f"failed to send {sequence!r}")
        return 1
    hc.ok(f"sent {sequence!r}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--role", choices=tuple(ROLES), default="display",
                        help="device role (default: display)")
    common.add_argument("--driver",
                        help="registered driver name; defaults to HARMONIA_DISPLAY "
                             "or HARMONIA_RECEIVER in config.env")

    sub.add_parser("list", help="show configured drivers").set_defaults(
        func=cmd_list)

    p_pair = sub.add_parser("pair", parents=[common],
                            help="one-time pairing through the selected driver")
    p_pair.add_argument("host", nargs="?",
                        help="device address; defaults to the driver's config")
    p_pair.add_argument("--token-file",
                        help="override the driver's credential path")
    p_pair.add_argument("--save", action="store_true",
                        help="write the selected driver and pairing values to "
                             "config.env")
    p_pair.set_defaults(func=cmd_pair)

    p_state = sub.add_parser("state", parents=[common],
                             help="read normalized device state")
    p_state.add_argument("--json", action="store_true")
    p_state.set_defaults(func=cmd_state)

    p_profile = sub.add_parser("profile", parents=[common],
                               help="read driver diagnostics")
    p_profile.add_argument("--json", action="store_true")
    p_profile.set_defaults(func=cmd_profile)

    sub.add_parser("inputs", parents=[common],
                   help="list inputs and visibility").set_defaults(
                       func=cmd_inputs)

    p_name = sub.add_parser("name-input", parents=[common],
                            help="show/plan an input rename")
    p_name.add_argument("--input", required=True,
                        help="hardware input name, e.g. HDMI-1")
    p_name.add_argument("--name", required=True)
    p_name.add_argument("--apply", action="store_true")
    p_name.set_defaults(func=cmd_name_input)

    for command, visible, help_text in (
            ("hide-input", False, "hide an input"),
            ("show-input", True, "show an input")):
        p = sub.add_parser(command, parents=[common], help=help_text)
        p.add_argument("--input", required=True,
                       help="hardware input name, e.g. HDMI-3")
        p.add_argument("--apply", action="store_true")
        p.set_defaults(func=lambda a, v=visible: cmd_visibility(a, v))

    p_key = sub.add_parser("key", parents=[common],
                           help="send a driver-defined key sequence")
    p_key.add_argument("sequence", nargs="+",
                       help="e.g. MENU 1 9 9 9, BACK, HOME")
    p_key.set_defaults(func=cmd_key)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
