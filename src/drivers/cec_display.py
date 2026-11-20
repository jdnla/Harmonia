"""Generic CEC display driver (stub).

For TVs without a native network API: control power + (optionally) volume/mute
over HDMI-CEC using cec-client (libcec) or the same navkey/CEC path the rest of
the project uses. This is a scaffold — the CEC command plumbing is marked as placeholders
where hardware-specific testing is needed. It registers as HARMONIA_DISPLAY=cec.

Enough is implemented to be usable for power on/off and to serve as the template
for LG/Samsung/Sony native drivers later.
"""
from __future__ import annotations

import logging
import subprocess

from .base import Display, CAP_POWER, CAP_INPUT, CAP_VOLUME, CAP_MUTE
from .registry import register_display

log = logging.getLogger("drivers.cec")


@register_display("cec")
class CECDisplay(Display):
    role_id = "display"

    def __init__(self, cec_adapter: str, osd_name: str,
                 capabilities: tuple[str, ...], inputs: list[str],
                 name: str, label: str, cec_bin: str = "cec-client"):
        self.cec_adapter = cec_adapter
        self.osd_name = osd_name
        self.capabilities = capabilities
        self.inputs = inputs
        self.name = name
        self.label = label
        self.model = "CEC"
        self.manufacturer = "Generic (CEC)"
        self._cec_bin = cec_bin

    @classmethod
    def from_env(cls, env):
        caps = tuple(c.strip() for c in env("CEC_CAPS", "power").split(",")
                     if c.strip())
        inputs = [s for s in (i.strip()
                  for i in env("CEC_INPUTS", "").split(",")) if s]
        return cls(
            cec_adapter=env("CEC_ADAPTER", "RPI"),  # or /dev/cec0, etc.
            osd_name=env("CEC_OSD_NAME", "Harmonia"),
            capabilities=caps,
            inputs=inputs,
            name=env("CEC_DISPLAY_NAME", "TV"),
            label=env("CEC_LABEL", "TV"),
            cec_bin=env("CEC_CLIENT_BIN", "cec-client"),
        )

    def _cec(self, command: str) -> bool:
        """Send a raw cec-client command (e.g. 'on 0', 'standby 0')."""
        try:
            subprocess.run(
                [self._cec_bin, "-s", "-d", "1", self.cec_adapter],
                input=command + "\n", text=True, capture_output=True, timeout=8)
            return True
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("cec command %r failed: %s", command, e)
            return False

    # ---- state ----
    def get_state(self) -> dict:
        # CEC power-status polling ('pow 0') is slow/flaky on many adapters.
        # Report available but power unknown unless a poll is explicitly enabled.
        state = {"available": True}
        # Placeholder: optional 'pow 0' parse when CEC_POLL_POWER=1.
        return state

    # ---- commands ----
    def power(self, on: bool) -> None:
        self._cec("on 0" if on else "standby 0")

    def set_input(self, src: str) -> None:
        # Input switching over CEC is device-specific (active-source / vendor
        # commands). Left as a placeholder for per-TV tuning.
        log.info("CEC set_input(%s) not implemented for this display", src)

    # Volume over CEC (only if declared): CEC volume-up/down/mute user-control.
    def volume_up(self) -> None:
        self._cec("volup")

    def volume_down(self) -> None:
        self._cec("voldown")

    def mute(self, on: bool) -> None:
        self._cec("mute")  # CEC mute is a toggle; state tracking is best-effort
