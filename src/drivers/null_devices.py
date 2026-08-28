"""Null / no-op device drivers.

The bridge treats the config value "none" as "no device in this role" and skips
it entirely (registry returns None). These null classes exist for two narrower
uses:

  * testing the bridge composition without hardware, and
  * a documented, explicit placeholder if a future setup wants a role present in
    HA but inert.

They are registered under "null" (not "none", which the registry reserves).
"""
from __future__ import annotations

from .base import AVReceiver, Display


class _NullMixin:
    def get_state(self) -> dict:
        return {"available": False}


class NullReceiver(_NullMixin, AVReceiver):
    capabilities = ()

    @classmethod
    def from_env(cls, env):
        return cls()

    def power(self, on: bool) -> None: ...
    def set_input(self, src: str) -> None: ...
    def set_volume(self, vol: float) -> None: ...
    def mute(self, on: bool) -> None: ...


class NullDisplay(_NullMixin, Display):
    capabilities = ()

    @classmethod
    def from_env(cls, env):
        return cls()

    def power(self, on: bool) -> None: ...
    def set_input(self, src: str) -> None: ...


# Register under "null" for explicit opt-in; "none" stays a bridge-level skip.
from .registry import register_receiver, register_display  # noqa: E402

register_receiver("null")(NullReceiver)
register_display("null")(NullDisplay)
