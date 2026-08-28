#!/usr/bin/env python3
"""Interactive one-time pairing with a Vizio SmartCast TV.

Run this once per TV. It initiates pairing (a PIN appears on the TV screen),
asks you to type the PIN, completes the handshake, and writes the AUTH token to
the token file the bridge reads.

Usage:
    python3 pair_vizio.py <tv_ip> [token_output_path]
"""
import sys
import os
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DEVICE_ID = "mediapc-bridge"
DEVICE_NAME = "LinuxPC-HA-Bridge"


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    host = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.expanduser(
        "~/.config/avr-tv-bridge/vizio.token")
    base = f"https://{host}:7345"
    s = requests.Session()
    s.verify = False
    s.headers.update({"Content-Type": "application/json"})

    print(f"Starting pairing with {host} ... watch the TV for a PIN.")
    r = s.put(f"{base}/pairing/start",
              json={"DEVICE_ID": DEVICE_ID, "DEVICE_NAME": DEVICE_NAME},
              timeout=5).json()
    if r.get("STATUS", {}).get("RESULT") != "SUCCESS":
        print("Failed to start pairing:", r)
        sys.exit(1)
    req_token = r["ITEM"]["PAIRING_REQ_TOKEN"]
    ctype = r["ITEM"]["CHALLENGE_TYPE"]

    pin = input("Enter the PIN shown on the TV: ").strip()

    r = s.put(f"{base}/pairing/pair",
              json={"DEVICE_ID": DEVICE_ID, "CHALLENGE_TYPE": ctype,
                    "RESPONSE_VALUE": pin, "PAIRING_REQ_TOKEN": req_token},
              timeout=5).json()
    if r.get("STATUS", {}).get("RESULT") != "SUCCESS":
        print("Pairing failed:", r)
        sys.exit(1)
    token = r["ITEM"]["AUTH_TOKEN"]

    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        f.write(token + "\n")
    os.chmod(out, 0o600)
    print(f"Success. Token written to {out}")


if __name__ == "__main__":
    main()
