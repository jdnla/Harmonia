"""Denon/Marantz AVR control client.

Uses the AVR's telnet control protocol (port 23) for commands and status,
with the HTTP status XML (port 8080) as a supplementary source for device info.

Tested against a Denon AVR-X1800H. The protocol is shared across most modern
Denon/Marantz receivers, so this should work broadly.
"""
from __future__ import annotations

import socket
import time
import logging
from typing import Optional

log = logging.getLogger("denon")

# Master volume on Denon is expressed as a number where "435" means 43.5 dB-ish
# on the display (half-dB precision uses a trailing 3rd digit). MVMAX reports the
# ceiling. We normalise to a 0-100 percentage for Home Assistant.
DEFAULT_MAX_VOL = 98.0


class DenonClient:
    def __init__(self, host: str, port: int = 23, timeout: float = 2.0,
                 vol_offset: float = 0.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._max_vol = DEFAULT_MAX_VOL
        # Cosmetic offset between the Denon control-protocol volume value and the
        # number shown on the receiver's on-screen display. Added on read and
        # subtracted on write so Home Assistant matches the on-screen number.
        # FIXME(2026-07-25): set empirically to +2 from a single data point
        # (set 45 -> screen showed 47). Not yet verified constant across the
        # range; characterize at 40/50/60 and confirm it's a fixed offset (or
        # map the real relationship) when the TV is free.
        self.vol_offset = vol_offset

    def _txn(self, commands: list[str], read_time: float = 0.5) -> list[str]:
        """Open a short-lived connection, send commands, collect replies.

        The Denon protocol is asynchronous: after sending e.g. 'PW?\\r' the AVR
        emits one or more lines. We open a fresh socket per transaction because
        the AVR only allows a limited number of persistent control connections
        and a per-transaction approach is far more robust for a polling bridge.
        """
        lines: list[str] = []
        try:
            with socket.create_connection((self.host, self.port), self.timeout) as s:
                s.settimeout(self.timeout)
                for cmd in commands:
                    s.sendall((cmd + "\r").encode("ascii"))
                    time.sleep(0.05)
                # Drain replies for a short window
                deadline = time.time() + read_time
                buf = b""
                while time.time() < deadline:
                    try:
                        s.settimeout(max(0.05, deadline - time.time()))
                        chunk = s.recv(256)
                        if not chunk:
                            break
                        buf += chunk
                    except socket.timeout:
                        break
                lines = [l.strip() for l in buf.decode("ascii", "ignore").split("\r") if l.strip()]
        except (OSError, socket.timeout) as e:
            log.warning("Denon %s transaction failed: %s", self.host, e)
        return lines

    # ---- Status ----
    def get_state(self) -> dict:
        """Return a dict with power, input, volume (0-100), mute, available."""
        lines = self._txn(["PW?", "SI?", "MV?", "MU?"], read_time=0.8)
        state: dict = {"available": bool(lines)}
        for l in lines:
            if l.startswith("PW"):
                state["power"] = "ON" if l == "PWON" else "OFF"
            elif l.startswith("SI"):
                state["input"] = l[2:]
            elif l.startswith("MVMAX"):
                # e.g. "MVMAX 98"
                try:
                    self._max_vol = float(l.split()[1]) or DEFAULT_MAX_VOL
                except (IndexError, ValueError):
                    pass
            elif l.startswith("MV") and l[2:].isdigit():
                # Expose the Denon's native scale, plus the display offset so the
                # value matches the receiver's on-screen number. "445" -> 44.5.
                state["volume"] = self._raw_to_display(l[2:]) + self.vol_offset
            elif l.startswith("MU"):
                state["mute"] = (l == "MUON")
        return state

    @property
    def max_volume(self) -> float:
        return self._max_vol

    @staticmethod
    def _raw_to_display(raw: str) -> float:
        # "43" -> 43.0, "435" -> 43.5 (matches the receiver's displayed number)
        if len(raw) == 3:
            return int(raw[:2]) + (int(raw[2]) / 10.0)
        return float(int(raw))

    @staticmethod
    def _display_to_raw(vol: float) -> str:
        """Convert a display value (e.g. 44 or 44.5) to a Denon raw code.

        Denon accepts whole steps as 2 digits ("44") and half steps as 3 digits
        ("445"). Only .0 and .5 are valid; anything else snaps to the nearest.
        """
        whole = int(vol)
        frac = vol - whole
        half = 5 if 0.25 <= frac < 0.75 else 0
        if frac >= 0.75:
            whole += 1
            half = 0
        return f"{whole:02d}" if half == 0 else f"{whole:02d}{half}"

    # ---- Commands ----
    def power(self, on: bool):
        self._txn(["PWON" if on else "PWSTANDBY"])

    def set_input(self, src: str):
        self._txn([f"SI{src}"])

    def set_volume(self, vol: float):
        """Set absolute volume. Input is on the display scale (HA-facing), so we
        subtract the offset to get the control-protocol value before sending."""
        raw_vol = float(vol) - self.vol_offset
        raw_vol = max(0.0, min(self._max_vol, raw_vol))
        self._txn([f"MV{self._display_to_raw(raw_vol)}"])

    def volume_up(self):
        self._txn(["MVUP"])

    def volume_down(self):
        self._txn(["MVDOWN"])

    def mute(self, on: bool):
        self._txn(["MUON" if on else "MUOFF"])
