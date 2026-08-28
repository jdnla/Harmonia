#!/usr/bin/env python3
"""Inject a navigation keystroke into the focused window via ydotool.

Called by irexec on IR remote D-pad presses. Maps a logical button name to a
Linux input keycode and sends it through the ydotoold daemon, so it lands in
whatever app currently has focus (Plex HTPC, Netflix, etc.).

Usage: navkey.py <button>   e.g. navkey.py up
Buttons: up down left right ok back menu home playpause

Requires ydotoold running (see systemd/ydotoold.service) and its socket at
/tmp/.ydotool_socket (this build of ydotoold ignores --socket-path).
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys

from session_env import graphical_env

log = logging.getLogger("navkey")

def _default_socket() -> str:
    xrd = os.environ.get("XDG_RUNTIME_DIR")
    if xrd:
        return f"{xrd}/.ydotool_socket"
    return "/tmp/.ydotool_socket"


YDOTOOL_SOCKET = os.environ.get("YDOTOOL_SOCKET") or _default_socket()

# Logical button -> Linux input event keycode (see /usr/include/linux/input-event-codes.h)
KEYCODES: dict[str, int] = {
    "up": 103,       # KEY_UP
    "down": 108,     # KEY_DOWN
    "left": 105,     # KEY_LEFT
    "right": 106,    # KEY_RIGHT
    "ok": 28,        # KEY_ENTER
    "select": 28,    # alias
    "back": 1,       # KEY_ESC
    "exit": 1,       # alias
    "menu": 139,     # KEY_MENU
    "home": 102,     # KEY_HOME
    "playpause": 164,  # KEY_PLAYPAUSE
    "play": 164,
    "pause": 164,
}


def send_key(button: str) -> bool:
    button = button.lower()
    code = KEYCODES.get(button)
    if code is None:
        log.error("Unknown nav button: %s", button)
        return False
    env = graphical_env()
    env["YDOTOOL_SOCKET"] = YDOTOOL_SOCKET
    # Hold the key briefly between press and release. A zero-gap press/release
    # can be dropped or mis-registered by Qt apps (e.g. Plex HTPC) on Wayland;
    # --key-delay inserts a hold so it looks like a real keypress. Milliseconds.
    hold_ms = os.environ.get("NAVKEY_HOLD_MS", "40")
    try:
        # ydotool key syntax: <code>:1 (press) <code>:0 (release)
        r = subprocess.run(
            ["ydotool", "key", "--key-delay", str(hold_ms),
             f"{code}:1", f"{code}:0"],
            capture_output=True, text=True, env=env, timeout=5)
        if r.returncode == 0:
            log.info("Sent %s (keycode %d)", button, code)
            return True
        log.warning("ydotool failed for %s rc=%s: %s",
                    button, r.returncode, r.stderr.strip())
        return False
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("send_key %s failed: %s", button, e)
        return False


def main(argv: list[str]) -> int:
    logging.basicConfig(level="INFO", format="%(levelname)s %(name)s: %(message)s")
    if len(argv) != 2:
        print(f"Usage: {argv[0]} <button>  ({', '.join(KEYCODES)})")
        return 2
    return 0 if send_key(argv[1]) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
