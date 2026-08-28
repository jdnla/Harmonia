"""Driver registry: map config names -> driver classes.

Concrete drivers register themselves at import time:

    from .registry import register_receiver
    from .base import AVReceiver

    @register_receiver("denon")
    class DenonReceiver(AVReceiver):
        @classmethod
        def from_env(cls, env): ...

Each driver class must provide a `from_env(env)` classmethod that builds an
instance from a callable `env(key, default=None, required=False)` (the bridge's
env helper), so construction details stay inside the driver.

The special name "none" is reserved by the bridge for "no device in this role"
and is not registered here.
"""
from __future__ import annotations

import logging

log = logging.getLogger("drivers.registry")

_RECEIVERS: dict[str, type] = {}
_DISPLAYS: dict[str, type] = {}
_AUDIO: dict[str, type] = {}


def _register(table: dict, name: str):
    def deco(cls):
        key = name.lower()
        if key in table:
            log.warning("driver %r already registered (%s) — overriding with %s",
                        key, table[key].__name__, cls.__name__)
        table[key] = cls
        cls.driver_name = key
        return cls
    return deco


def register_receiver(name: str):
    return _register(_RECEIVERS, name)


def register_display(name: str):
    return _register(_DISPLAYS, name)


def register_audio(name: str):
    return _register(_AUDIO, name)


def _get(table: dict, kind: str, name: str):
    key = (name or "").lower()
    if key in ("", "none", "off", "disabled"):
        return None
    cls = table.get(key)
    if cls is None:
        raise KeyError(
            f"unknown {kind} driver {name!r}; available: "
            f"{', '.join(sorted(table)) or '(none)'}")
    return cls


def get_receiver(name: str):
    return _get(_RECEIVERS, "receiver", name)


def get_display(name: str):
    return _get(_DISPLAYS, "display", name)


def get_audio(name: str):
    return _get(_AUDIO, "audio", name)


def available() -> dict:
    return {
        "receivers": sorted(_RECEIVERS),
        "displays": sorted(_DISPLAYS),
        "audio": sorted(_AUDIO),
    }
