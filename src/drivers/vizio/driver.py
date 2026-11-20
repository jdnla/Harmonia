"""Vizio SmartCast TV driver.

Adapter wrapping the SmartCast client behind the Display interface. Declares
capabilities from config: by default a TV whose audio is routed to a receiver
exposes only power+input, but a TV-direct setup can add volume+mute via
VIZIO_CAPS.
"""
from __future__ import annotations

from .client import VizioClient
from .pair import pair as vizio_pair
from ..base import (Display, DiscoveredDevice,
                    CAP_POWER, CAP_VOLUME, CAP_MUTE, CAP_INPUT)
from ..registry import register_display


@register_display("vizio")
class VizioDisplay(Display):
    role_id = "display"

    DEFAULT_INPUTS = "HDMI-1,HDMI-2,HDMI-3,HDMI-4,COMP,TUNER,CAST,SMARTCAST"
    input_name_writes_safe = False
    input_name_hint = (
        "Vizio SmartCast firmware may reject or hang on input-name writes; "
        "rename the input in the TV UI, then set SCENE_TV_INPUT_MATCH.")

    def __init__(self, client: VizioClient, inputs: list[str],
                 capabilities: tuple[str, ...], name: str, model: str, label: str):
        self._c = client
        self.inputs = inputs
        self.capabilities = capabilities
        self.name = name
        self.model = model
        self.label = label
        self.manufacturer = "Vizio"

    @classmethod
    def pair(cls, env, host=None, token_file=None):
        """One-time SmartCast PIN pairing; returns config.env updates."""
        host = host or env("VIZIO_HOST", required=True)
        token_file = token_file or env(
            "VIZIO_TOKEN_FILE", "~/.config/avr-tv-bridge/vizio.token")
        path = vizio_pair(host, token_file)
        return {"VIZIO_HOST": host, "VIZIO_TOKEN_FILE": path}

    @classmethod
    def from_env(cls, env):
        token = cls._read_token(env("VIZIO_TOKEN_FILE", required=True))
        client = VizioClient(env("VIZIO_HOST", required=True), token)
        inputs = env("VIZIO_INPUTS", cls.DEFAULT_INPUTS).split(",")
        caps = tuple(c.strip() for c in env("VIZIO_CAPS", "power,input").split(",")
                     if c.strip())
        return cls(
            client=client,
            inputs=[s for s in (i.strip() for i in inputs) if s],
            capabilities=caps,
            name=env("VIZIO_NAME", "Vizio TV"),
            model=env("VIZIO_MODEL", "SmartCast"),
            label=env("VIZIO_LABEL", "TV"),
        )

    @staticmethod
    def _read_token(path: str) -> str:
        with open(path) as f:
            return f.read().strip()

    # ---- discovery ----
    @classmethod
    def discover(cls, ssdp_results, timeout: float = 3.0):
        """Vizio SmartCast TVs answer SSDP on the Chromecast/DIAL port 8008 with
        a device-desc.xml. Distinguishing a Vizio from a Chromecast dongle needs
        a probe of the SmartCast API port 7345, so we confirm that before
        offering. Each match needs a follow-up pairing step."""
        import socket
        found = []
        seen = set()
        for ip, headers in ssdp_results:
            if ip in seen:
                continue
            h = headers.lower()
            # SmartCast TVs surface via the 8008 desc; confirm 7345 is open.
            if ":8008/" in h or "smartcast" in h or "vizio" in h:
                if cls._port_open(ip, 7345, timeout=1.0):
                    seen.add(ip)
                    found.append(DiscoveredDevice(
                        driver_name="vizio", role="display", host=ip,
                        label=f"Vizio SmartCast TV @ {ip}",
                        env_updates={"HARMONIA_DISPLAY": "vizio",
                                     "VIZIO_HOST": ip},
                        followup="pair"))
        return found

    @staticmethod
    def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
        import socket
        try:
            with socket.create_connection((host, port), timeout):
                return True
        except OSError:
            return False

    # ---- state ----
    def get_state(self) -> dict:
        return self._c.get_state()

    # ---- commands ----
    def power(self, on: bool) -> None:
        self._c.power(on)

    def set_input(self, src: str) -> None:
        self._c.set_input(src)

    def list_named_inputs(self) -> list[tuple[str, str]]:
        """[(hardware_name, friendly_name)] for each input (read-only).

        Lets the bridge auto-detect which physical input the media-center
        chain is on by matching the receiver's CEC name, instead of trusting
        a hardcoded HDMI port. Returns [] if the TV is unreachable.
        """
        return self._c.list_named_inputs()

    def get_profile(self) -> dict:
        """Read-only TV diagnostics (firmware, audio, ads, input visibility)."""
        return self._c.get_tv_profile()

    def get_tv_profile(self) -> dict:
        """Backward-compatible alias used by the bridge poller."""
        return self.get_profile()

    def get_input_visibility(self) -> dict[str, str]:
        return self._c.get_input_visibility()

    @staticmethod
    def input_cname(hardware_name: str) -> str:
        return VizioClient.input_cname(hardware_name)

    def get_inputs(self) -> list[dict]:
        return self._c.get_inputs()

    def set_input_name(self, hardware_name: str, name: str) -> bool:
        return self._c.set_input_name(hardware_name, name)

    def set_input_visibility(self, hardware_name: str, visible: bool) -> bool:
        return self._c.set_input_visibility(hardware_name, visible)

    def send_key_sequence(self, spec: str) -> bool:
        return self._c.send_key_sequence(spec)

    def set_volume(self, vol: float) -> None:
        self._c.set_volume(int(vol))

    def volume_up(self) -> None:
        self._c.volume_up()

    def volume_down(self) -> None:
        self._c.volume_down()

    def mute(self, on: bool) -> None:
        self._c.mute(on)
