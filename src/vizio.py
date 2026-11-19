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
import os
import re
from typing import Optional

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
log = logging.getLogger("vizio")

# Volume is 0-100 natively on SmartCast.
POWER_ENDPOINT = "/state/device/power_mode"
TV_SETTINGS = "/menu_native/dynamic/tv_settings"
CURRENT_INPUT = f"{TV_SETTINGS}/devices/current_input"
CURRENT_INPUTS = f"{TV_SETTINGS}/devices/current_inputs"
INPUT_LIST = f"{TV_SETTINGS}/devices/name_input"
AUDIO_ROOT = f"{TV_SETTINGS}/audio"
ADVERTISING_ROOT = f"{TV_SETTINGS}/admin_and_privacy/advertising"
SYSTEM_ROOT = f"{TV_SETTINGS}/system"
INPUT_VISIBILITY_ROOT = f"{SYSTEM_ROOT}/hide_from_input_list"
INPUT_NAME_ROOT = f"{SYSTEM_ROOT}/input_name"
SYSTEM_INFO_ROOT = (
    f"{TV_SETTINGS}/admin_and_privacy/system_information/tv_information"
)

# SmartCast's input-name/visibility groups use lower-case CNAMEs.
INPUT_CNAME = {
    "HDMI-1": "hdmi1",
    "HDMI-2": "hdmi2",
    "HDMI-3": "hdmi3",
    "HDMI-4": "hdmi4",
    "COMP": "comp",
    "TV": "tuner",
    "TUNER": "tuner",
}

# SmartCast key codes. Digits are ASCII in codeset 0 (48 = '0', 57 = '9').
KEYMAP = {
    "MENU": (4, 8),
    "HOME": (4, 3),
    "BACK": (4, 0),
    "EXIT": (9, 0),
    "OK": (3, 2),
    "UP": (3, 8),
    "DOWN": (3, 0),
    "LEFT": (3, 1),
    "RIGHT": (3, 7),
    "VOL_UP": (5, 1),
    "VOL_DOWN": (5, 0),
    "MUTE_ON": (5, 3),
    "MUTE_OFF": (5, 2),
    "MUTE_TOGGLE": (5, 4),
    "POWER_OFF": (11, 0),
    "POWER_ON": (11, 1),
    "POWER_TOGGLE": (11, 2),
}


def pair(host: str, token_file: str | None = None, *,
         device_id: str = "harmonia-bridge",
         device_name: str = "Harmonia",
         prompt=input) -> str:
    """Pair with a SmartCast display and write its AUTH token.

    Returns the token-file path. Raises RuntimeError when pairing fails, so CLI
    callers can report it without exiting from inside the protocol code.
    """
    if not host:
        raise RuntimeError("a display host is required")
    default = "~/.config/avr-tv-bridge/vizio.token"
    out = os.path.expanduser((token_file or default).replace("%h", "~"))
    base = f"https://{host}:7345"
    session = requests.Session()
    session.verify = False
    session.headers.update({"Content-Type": "application/json"})

    print(f"Starting pairing with {host} ... watch the display for a PIN.")
    try:
        started = session.put(
            f"{base}/pairing/start",
            json={"DEVICE_ID": device_id, "DEVICE_NAME": device_name},
            timeout=5).json()
    except (requests.RequestException, ValueError) as e:
        raise RuntimeError(f"pairing start failed: {e}") from e
    if started.get("STATUS", {}).get("RESULT") != "SUCCESS":
        raise RuntimeError(f"pairing start rejected: {started}")
    item = started.get("ITEM") or {}
    req_token = item.get("PAIRING_REQ_TOKEN")
    challenge = item.get("CHALLENGE_TYPE")

    pin = prompt("Enter the PIN shown on the display: ").strip()
    try:
        paired = session.put(
            f"{base}/pairing/pair",
            json={"DEVICE_ID": device_id, "CHALLENGE_TYPE": challenge,
                  "RESPONSE_VALUE": pin, "PAIRING_REQ_TOKEN": req_token},
            timeout=5).json()
    except (requests.RequestException, ValueError) as e:
        raise RuntimeError(f"pairing completion failed: {e}") from e
    if paired.get("STATUS", {}).get("RESULT") != "SUCCESS":
        raise RuntimeError(f"pairing rejected: {paired}")
    token = (paired.get("ITEM") or {}).get("AUTH_TOKEN")
    if not token:
        raise RuntimeError("pairing succeeded but no AUTH_TOKEN was returned")

    parent = os.path.dirname(out)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(out, "w") as f:
        f.write(token + "\n")
    os.chmod(out, 0o600)
    print(f"Success. Token written to {out}")
    return out


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

    @staticmethod
    def _items_by_cname(data: Optional[dict]) -> dict[str, dict]:
        if not data:
            return {}
        return {item.get("CNAME"): item for item in data.get("ITEMS") or []
                if item.get("CNAME")}

    def get_item(self, path: str, cname: str | None = None) -> Optional[dict]:
        """Read a group item (or the first item when cname is omitted)."""
        items = self._items_by_cname(self._get(path))
        if cname is None:
            return next(iter(items.values()), None)
        return items.get(cname)

    def set_item(self, path: str, cname: str, value) -> bool:
        """Read-modify-write one setting and verify the readback.

        SmartCast requires the current HASHVAL. Most settings use
        ``<group>/<cname>``; a few single-item endpoints (e.g. input_name)
        are the group path itself, so use that when it is the only item.
        """
        data = self._get(path)
        items = self._items_by_cname(data)
        item = items.get(cname)
        if item is None:
            log.warning("Vizio setting %s/%s not found", path, cname)
            return False
        uri = path if len(items) == 1 else f"{path.rstrip('/')}/{cname}"
        ok = self._put(uri, {
            "REQUEST": "MODIFY",
            "VALUE": value,
            "HASHVAL": item.get("HASHVAL"),
        })
        if not ok:
            return False
        after = self._items_by_cname(self._get(path)).get(cname)
        if after is None or after.get("VALUE") != value:
            log.warning("Vizio setting %s/%s readback %r != %r",
                        path, cname, after.get("VALUE") if after else None, value)
            return False
        return True

    # ---- Settings / diagnostics ----
    def get_audio_setting(self, cname: str):
        item = self.get_item(AUDIO_ROOT, cname)
        return item.get("VALUE") if item else None

    def set_audio_setting(self, cname: str, value) -> bool:
        return self.set_item(AUDIO_ROOT, cname, value)

    def get_inputs(self) -> list[dict]:
        item = self.get_item(CURRENT_INPUTS)
        value = item.get("VALUE") if item else None
        return value if isinstance(value, list) else []

    def get_input_visibility(self) -> dict[str, str]:
        items = self._items_by_cname(self._get(INPUT_VISIBILITY_ROOT))
        return {cname: item.get("VALUE") for cname, item in items.items()}

    @staticmethod
    def input_cname(hardware_name: str) -> str:
        return INPUT_CNAME.get(hardware_name.upper(), hardware_name.lower())

    def set_input_visibility(self, hardware_name: str, visible: bool) -> bool:
        return self.set_item(INPUT_VISIBILITY_ROOT,
                             self.input_cname(hardware_name),
                             "Visible" if visible else "Hidden")

    def set_input_name(self, hardware_name: str, name: str) -> bool:
        """Set an input's friendly name.

        Note: P65Q9-H1 firmware 5.1.3.1 rejects this write (TYPE_ERROR) and
        can hang the SmartCast HTTP service. Use the TV's Input Settings menu
        instead; this is kept for other firmware that may accept it.
        """
        return self.set_item(
            f"{INPUT_NAME_ROOT}/{self.input_cname(hardware_name)}",
            "name_input", {"METADATA": "", "NAME": name})

    def get_tv_profile(self) -> dict:
        """Read the TV settings used for diagnostics and input detection."""
        info = self._items_by_cname(self._get(SYSTEM_INFO_ROOT))
        audio = self._items_by_cname(self._get(AUDIO_ROOT))
        ads = self._items_by_cname(self._get(ADVERTISING_ROOT))
        visibility = self.get_input_visibility()
        inputs = self.get_inputs()

        def value(items: dict[str, dict], cname: str):
            item = items.get(cname)
            return item.get("VALUE") if item else None

        visible_inputs = [
            i.get("CUSTOM_NAME") or i.get("NAME")
            for i in inputs if i.get("VISIBLE")
        ]
        return {
            "available": bool(info or audio or ads or visibility),
            "model": value(info, "model_name"),
            "firmware": value(info, "version"),
            "cast_version": value(info, "cast_version"),
            "tv_speakers": value(audio, "tv_speakers"),
            "earc": value(audio, "earc_mode"),
            "digital_audio_out": value(audio, "digital_audio_out"),
            "limit_ad_tracking": value(ads, "limit_ad_tracking"),
            "ad_replacement": value(ads, "ad_replacement"),
            "input_visibility": visibility,
            "visible_inputs": visible_inputs,
            "inputs": inputs,
        }

    # ---- Key commands ----
    def send_key(self, codeset: int, code: int) -> bool:
        payload = {
            "KEYLIST": [{"CODESET": codeset, "CODE": code, "ACTION": "KEYPRESS"}]
        }
        return self._put("/key_command/", payload)

    def _key_command(self, codeset: int, code: int) -> bool:
        return self.send_key(codeset, code)

    def send_key_sequence(self, spec: str) -> bool:
        """Send a comma/space-separated key sequence (names or single digits).

        Examples: ``MENU 1 9 9 9``, ``BACK``, ``HOME``.
        """
        tokens = [t for t in re.split(r"[,\s]+", spec.strip()) if t]
        if not tokens:
            return False
        for token in tokens:
            key = token.upper()
            if key in KEYMAP:
                codeset, code = KEYMAP[key]
            elif len(key) == 1 and key.isdigit():
                codeset, code = 0, 48 + int(key)
            else:
                log.warning("Unknown TV key %r in sequence %r", token, spec)
                return False
            if not self.send_key(codeset, code):
                return False
        return True


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

    def list_named_inputs(self) -> list[tuple[str, str]]:
        """Return [(hardware_name, friendly_name)] for every input.

        The SmartCast `name_input` endpoint lists each input's fixed hardware
        NAME (e.g. "HDMI-2" — the value accepted by set_input) alongside its
        VALUE.NAME, the friendly label the TV shows. When a CEC device is
        connected the TV renames that input to the device's OSD name, so e.g.
        the receiver appears as ("HDMI-2", "AVR-X1800H"). Read-only; returns []
        on any error so callers can fall back safely.
        """
        data = self._get(INPUT_LIST)
        out: list[tuple[str, str]] = []
        if not data:
            return out
        for item in data.get("ITEMS", []):
            hw = item.get("NAME")
            val = item.get("VALUE")
            friendly = val.get("NAME") if isinstance(val, dict) else val
            if hw:
                out.append((hw, friendly or ""))
        return out

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
