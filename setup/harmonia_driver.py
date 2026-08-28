#!/usr/bin/env python3
"""Harmonia driver tooling — make writing a new plugin trivial.

Two subcommands:

  list                       Show all registered drivers + which optional hooks
                             (discover / selftest) each implements.
  new <role> <name>          Scaffold a ready-to-edit driver module in
                             src/drivers/<name>_driver.py with the right base
                             class, registration, from_env, and TODO markers.

Examples:
  python3 setup/harmonia_driver.py list
  python3 setup/harmonia_driver.py new receiver yamaha
  python3 setup/harmonia_driver.py new display lg
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)
sys.path.insert(0, HERE)

import harmonia_common as hc  # noqa: E402


def _implements(cls, method: str) -> bool:
    """True if cls overrides `method` rather than inheriting the base default."""
    from drivers import base
    base_cls = {"receiver": base.AVReceiver, "display": base.Display}
    # Find the base that defines the default
    for b in (base.Device, base.AVReceiver, base.Display):
        if hasattr(b, method) and getattr(cls, method, None) is not None:
            return getattr(cls, method).__qualname__.split(".")[0] == cls.__name__
    return False


def cmd_list():
    import drivers  # noqa: F401
    from drivers.registry import _RECEIVERS, _DISPLAYS, _AUDIO
    for title, table in (("Receivers", _RECEIVERS),
                         ("Displays", _DISPLAYS),
                         ("Audio", _AUDIO)):
        hc.head(title)
        if not table:
            hc.info("(none)")
            continue
        for name, cls in sorted(table.items()):
            hooks = []
            if _implements(cls, "discover"):
                hooks.append("discover")
            if _implements(cls, "selftest"):
                hooks.append("selftest(custom)")
            caps = ",".join(getattr(cls, "capabilities", ()) or ()) or "-"
            extra = f"  hooks: {', '.join(hooks)}" if hooks else ""
            print(f"  {hc.c(name,'cyan'):22} caps=[{caps}]{extra}")


_TEMPLATE = '''\
"""{Title} {role} driver for Harmonia.

TODO: implement the network protocol for this device. Fill in the methods
marked TODO; delete hooks you don't need (discover/selftest have safe defaults
in the base class). Register name: "{name}" (config: HARMONIA_{ROLE}={name}).
"""
from __future__ import annotations

from .base import ({base_cls}, DiscoveredDevice,
                   CAP_POWER, CAP_VOLUME, CAP_MUTE, CAP_INPUT)
from .registry import register_{role}


@register_{role}("{name}")
class {ClassName}({base_cls}):
    role_id = "{role}"
    # Declare only what the device really exposes; HA discovery follows this.
    capabilities = ({caps})

    def __init__(self, host, inputs, name, label):
        self.host = host
        self.inputs = inputs
        self.name = name
        self.label = label
        self.model = "{Title}"
        self.manufacturer = "{Title}"

    @classmethod
    def from_env(cls, env):
        # `env(key, default=None, required=False)` reads config.env values.
        host = env("{ENVPREFIX}_HOST", required=True)
        inputs = [s for s in (i.strip()
                  for i in env("{ENVPREFIX}_INPUTS", "").split(",")) if s]
        return cls(
            host=host,
            inputs=inputs,
            name=env("{ENVPREFIX}_NAME", "{Title}"),
            label=env("{ENVPREFIX}_LABEL", "{LABEL}"),
        )

    # ---- required: normalized state read ----
    def get_state(self) -> dict:
        # TODO: talk to the device; return keys you support:
        #   {{"available": bool, "power": "ON"/"OFF", "input": str,
        #     "volume": float 0-100, "mute": bool}}
        raise NotImplementedError

    # ---- required commands ----
    def power(self, on: bool) -> None:
        raise NotImplementedError  # TODO

    def set_input(self, src: str) -> None:
        raise NotImplementedError  # TODO
{extra_methods}
    # ---- optional: auto-discovery hook (delete if not supported) ----
    @classmethod
    def discover(cls, ssdp_results, timeout: float = 3.0):
        found = []
        for ip, headers in ssdp_results:
            h = headers.lower()
            if "{name}" in h:  # TODO: match this brand's SSDP signature
                found.append(DiscoveredDevice(
                    driver_name="{name}", role="{role}", host=ip,
                    label=f"{Title} @ {{ip}}",
                    env_updates={{"HARMONIA_{ROLE}": "{name}",
                                 "{ENVPREFIX}_HOST": ip}}))
        return found
'''

_RECEIVER_METHODS = '''
    def set_volume(self, vol: float) -> None:
        raise NotImplementedError  # TODO

    def mute(self, on: bool) -> None:
        raise NotImplementedError  # TODO
'''


def cmd_new(role: str, name: str):
    if role not in ("receiver", "display"):
        hc.bad("role must be 'receiver' or 'display'")
        sys.exit(2)
    name = name.lower()
    dest = os.path.join(SRC, "drivers", f"{name}_driver.py")
    if os.path.exists(dest):
        hc.bad(f"{dest} already exists")
        sys.exit(2)
    base_cls = "AVReceiver" if role == "receiver" else "Display"
    caps = ("CAP_POWER, CAP_VOLUME, CAP_MUTE, CAP_INPUT" if role == "receiver"
            else "CAP_POWER, CAP_INPUT")
    body = _TEMPLATE.format(
        Title=name.capitalize(), role=role, ROLE=role.upper(),
        name=name, ClassName=f"{name.capitalize()}{base_cls}",
        base_cls=base_cls, caps=caps,
        ENVPREFIX=name.upper(), LABEL=("AVR" if role == "receiver" else "TV"),
        extra_methods=_RECEIVER_METHODS if role == "receiver" else "",
    )
    with open(dest, "w") as f:
        f.write(body)
    hc.ok(f"created {dest}")
    hc.info("Next: implement the TODO methods. The module auto-loads (any "
            "*_driver.py here is picked up), so no __init__.py edit is needed —"
            " verify with:  python3 setup/harmonia_driver.py list")
    hc.info(f"Select it with:  HARMONIA_{role.upper()}={name}  and "
            f"{name.upper()}_HOST=<ip> in config.env")


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("list", "new"):
        print(__doc__)
        sys.exit(1)
    if sys.argv[1] == "list":
        cmd_list()
    else:
        if len(sys.argv) != 4:
            hc.bad("usage: harmonia_driver.py new <receiver|display> <name>")
            sys.exit(2)
        cmd_new(sys.argv[2], sys.argv[3])


if __name__ == "__main__":
    main()
