"""Vizio SmartCast pairing protocol."""
from __future__ import annotations

import os

import requests


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
