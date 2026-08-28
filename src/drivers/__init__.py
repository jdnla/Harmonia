"""Harmonia device drivers.

A driver is a concrete implementation of one of the abstract device roles
defined in `base.py` (AVReceiver, Display, AudioOutput). Drivers register
themselves by name so the bridge can compose a system purely from config:

    HARMONIA_RECEIVER=denon        # or "none" for TV-direct setups
    HARMONIA_DISPLAY=vizio         # or "cec", or "none"
    HARMONIA_AUDIO=hdmi            # PipeWire/HDMI channel management

The registry (`registry.py`) maps those names to driver classes. New hardware
support is added by dropping a module in this package that subclasses the right
base and calls the register_* decorator — no bridge changes required.
"""
from __future__ import annotations

# Importing the concrete driver modules registers them (via decorators).
# Kept in one place so `import drivers` wires up the whole catalog.
from . import base  # noqa: F401
from . import registry  # noqa: F401

# Concrete drivers auto-load: any module in this package ending in "_driver" or
# named for a device group registers itself on import. This makes plugins truly
# drop-in — scaffold a new src/drivers/<name>_driver.py (see
# setup/harmonia_driver.py) and it's picked up with no edits here. Each import is
# wrapped so one broken/optional driver can't take down the whole catalog.
import logging as _logging
import os as _os
import pkgutil as _pkgutil

_log = _logging.getLogger("drivers")

# Always-load core modules first (stable order), then auto-discover the rest.
_CORE = ("denon_driver", "vizio_driver", "cec_display", "null_devices")
_seen = set()


def _load(mod: str):
    if mod in _seen:
        return
    _seen.add(mod)
    try:
        __import__(f"{__name__}.{mod}", fromlist=["*"])
    except Exception as _e:  # pragma: no cover - defensive
        _log.warning("driver module %s failed to load: %s", mod, _e)


for _m in _CORE:
    _load(_m)

# Auto-discover any other *_driver.py (and *_devices.py) modules dropped in.
for _finder, _name, _ispkg in _pkgutil.iter_modules([_os.path.dirname(__file__)]):
    if _name in ("base", "registry", "__init__"):
        continue
    if _name.endswith("_driver") or _name.endswith("_devices") \
            or _name.endswith("_display"):
        _load(_name)
