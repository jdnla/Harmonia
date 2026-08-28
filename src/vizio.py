"""Vizio SmartCast TV control client.

Uses the SmartCast REST API over HTTPS (port 7345). Requires a one-time
paired AUTH token (see setup/pair_vizio.py). Tested against a Vizio P65Q9-H1
(P-series, firmware 5.1.3.x).

The API uses self-signed certs, so TLS verification is disabled (LAN-local).
Writes to settings require the AUTH header and a HASHVAL read immediately
before the write (SmartCast uses optimistic-concurrency hashes).
"""
from __future__ import annotations

import logging
from typing import Optional

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
log = logging.getLogger("vizio")

# Volume is 0-100 natively on SmartCast.
POWER_ENDPOINT = "/state/device/power_mode"
CURRENT_INPUT = "/menu_native/dynamic/tv_settings/devices/current_input"
INPUT_LIST = "/menu_native/dynamic/tv_settings/devices/name_input"
AUDIO_ROOT = "/menu_native/dynamic/tv_settings/audio"


class VizioClient:
    def __init__(self, host: str, token: str, port: int = 7345, timeout: float = 4.0):
        self.base = f"https://{host}:{port}"
        self.token = token
        self.timeout = timeout
        self._s = requests.Session()
        self._s.verify = False
        self._s.headers.update({"Content-Type": "application/json", "AUTH": token})

    def _get(self, path: str) -> Optional[dict]:
        try:
            r = self._s.get(self.base + path, timeout=self.timeout)
            return r.json()
        except (requests.RequestException, ValueError) as e:
            log.warning("Vizio GET %s failed: %s", path, e)
            return None

    def _put(self, path: str, payload: dict) -> bool:
        try:
            r = self._s.put(self.base + path, json=payload, timeout=self.timeout)
            j = r.json()
            ok = j.get("STATUS", {}).get("RESULT") == "SUCCESS"
            if not ok:
                log.warning("Vizio PUT %s -> %s", path, j.get("STATUS"))
            return ok
        except (requests.RequestException, ValueError) as e:
            log.warning("Vizio PUT %s failed: %s", path, e)
            return False

    # ---- Status ----
    def get_state(self) -> dict:
        state: dict = {"available": False}
        pm = self._get(POWER_ENDPOINT)
        if pm is None:
            return state
        state["available"] = True
        try:
            state["power"] = "ON" if pm["ITEMS"][0]["VALUE"] == 1 else "OFF"
        except (KeyError, IndexError):
            pass
        ci = self._get(CURRENT_INPUT)
        try:
            state["input"] = ci["ITEMS"][0]["VALUE"]
        except (KeyError, IndexError, TypeError):
            pass
        vol = self._get_audio_value("volume")
        if vol is not None:
            state["volume"] = int(vol)
        mute = self._get_audio_value("mute")
        if mute is not None:
            state["mute"] = str(mute).upper() == "ON"
        return state

    def _get_audio_value(self, cname: str):
        data = self._get(AUDIO_ROOT)
        if not data:
            return None
        for item in data.get("ITEMS", []):
            if item.get("CNAME") == cname:
                return item.get("VALUE")
        return None

    def _get_item(self, path: str):
        """Return (hashval, value) for a single-item endpoint."""
        data = self._get(path)
        try:
            item = data["ITEMS"][0]
            return item.get("HASHVAL"), item.get("VALUE")
        except (KeyError, IndexError, TypeError):
            return None, None

    # ---- Commands ----
    def power(self, on: bool):
        # Key-command approach is most reliable across firmware: POWER_ON / POWER_OFF
        code = 1 if on else 0
        # Codeset 11 = power; 0 off / 1 on. Falls back to toggle (2) if needed.
        return self._key_command(11, code)

    def _key_command(self, codeset: int, code: int) -> bool:
        payload = {
            "KEYLIST": [{"CODESET": codeset, "CODE": code, "ACTION": "KEYPRESS"}]
        }
        return self._put("/key_command/", payload)

    def set_input(self, name: str) -> bool:
        # Read current hashval, then write the new input name
        h, _ = self._get_item(CURRENT_INPUT)
        if h is None:
            return False
        return self._put(CURRENT_INPUT, {"REQUEST": "MODIFY", "VALUE": name, "HASHVAL": h})

    def set_volume(self, pct: int) -> bool:
        h, cur = self._get_item(f"{AUDIO_ROOT}/volume")
        if h is None:
            # volume lives in the audio group; use modify on the volume item
            return self._modify_audio("volume", max(0, min(100, int(pct))))
        return self._put(f"{AUDIO_ROOT}/volume",
                         {"REQUEST": "MODIFY", "VALUE": max(0, min(100, int(pct))), "HASHVAL": h})

    def _modify_audio(self, cname: str, value) -> bool:
        data = self._get(AUDIO_ROOT)
        if not data:
            return False
        for item in data.get("ITEMS", []):
            if item.get("CNAME") == cname:
                h = item.get("HASHVAL")
                uri = f"{AUDIO_ROOT}/{cname}"
                return self._put(uri, {"REQUEST": "MODIFY", "VALUE": value, "HASHVAL": h})
        return False

    def volume_up(self):
        return self._key_command(5, 1)   # codeset 5 = volume, 1 = up

    def volume_down(self):
        return self._key_command(5, 0)   # 0 = down

    def mute(self, on: bool):
        return self._key_command(5, 4 if on else 3)  # 4 = mute on, 3 = mute off
