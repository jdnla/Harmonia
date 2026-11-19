#!/usr/bin/env python3
"""Interactive one-time pairing with a Vizio SmartCast display.

Prefer the generic command (driver and host come from config.env):

    harmonia pair --driver vizio [host] [--save]

This standalone entry point remains for installer/script use.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")
if not os.path.isdir(SRC):
    SRC = ROOT
sys.path.insert(0, SRC)

from vizio import pair  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    host = sys.argv[1]
    token_file = sys.argv[2] if len(sys.argv) > 2 else None
    try:
        pair(host, token_file)
    except Exception as e:
        print(f"Pairing failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
