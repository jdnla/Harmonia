"""Denon/Marantz AV receiver driver.

Adapter that wraps the telnet client and exposes it through the AVReceiver
interface + driver registry, selectable via HARMONIA_RECEIVER=denon.
"""
from __future__ import annotations

from .client import DenonClient
from ..base import (AVReceiver, DiscoveredDevice,
                    CAP_POWER, CAP_VOLUME, CAP_MUTE, CAP_INPUT)
from ..registry import register_receiver


@register_receiver("denon")
class DenonReceiver(AVReceiver):
    role_id = "receiver"
    capabilities = (CAP_POWER, CAP_VOLUME, CAP_MUTE, CAP_INPUT)

    # Default input list (friendly, HA-facing). Overridable via env.
    DEFAULT_INPUTS = "MPLAY,CD,TUNER,DVD,BD,GAME,AUX1,TV,SAT/CBL,NET,BT"

    def __init__(self, client: DenonClient, inputs: list[str],
                 name: str, model: str, label: str):
        self._c = client
        self.inputs = inputs
        self.name = name
        self.model = model
        self.label = label
        self.manufacturer = "Denon"

    @classmethod
    def from_env(cls, env):
        client = DenonClient(
            env("DENON_HOST", required=True),
            vol_offset=float(env("DENON_VOLUME_OFFSET", "0")))
        inputs = env("DENON_INPUTS", cls.DEFAULT_INPUTS).split(",")
        return cls(
            client=client,
            inputs=[s for s in (i.strip() for i in inputs) if s],
            name=env("DENON_NAME", "Denon AVR"),
            model=env("DENON_MODEL", "AVR"),
            label=env("DENON_LABEL", "AVR"),
        )

    # ---- discovery ----
    @classmethod
    def discover(cls, ssdp_results, timeout: float = 3.0):
        """Denon/Marantz announce over SSDP with a 'Denon-Heos' server string
        and serve an AiOS UPnP description. Match those and offer each as an
        AVReceiver candidate."""
        found = []
        seen = set()
        for ip, headers in ssdp_results:
            h = headers.lower()
            if ip in seen:
                continue
            if "denon" in h or "marantz" in h or "heos" in h:
                seen.add(ip)
                found.append(DiscoveredDevice(
                    driver_name="denon", role="receiver", host=ip,
                    label=f"Denon/Marantz receiver @ {ip}",
                    env_updates={"HARMONIA_RECEIVER": "denon", "DENON_HOST": ip},
                    followup=""))
        return found

    # ---- state ----
    def get_state(self) -> dict:
        return self._c.get_state()

    @property
    def max_volume(self) -> float:
        return self._c.max_volume

    @property
    def vol_offset(self) -> float:
        return self._c.vol_offset

    # ---- commands ----
    def power(self, on: bool) -> None:
        self._c.power(on)

    def set_input(self, src: str) -> None:
        self._c.set_input(src)

    def set_volume(self, vol: float) -> None:
        self._c.set_volume(vol)

    def volume_up(self) -> None:
        self._c.volume_up()

    def volume_down(self) -> None:
        self._c.volume_down()

    def mute(self, on: bool) -> None:
        self._c.mute(on)
