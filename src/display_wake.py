"""Wake the display (DPMS on) on KDE Plasma Wayland.

On Wayland the old X11 `xset dpms force on` does not work; the supported way on
KDE is `kscreen-doctor --dpms on`. Must run inside the graphical session (needs
WAYLAND_DISPLAY / DBUS_SESSION_BUS_ADDRESS), so the launcher service — which
already runs in-session — owns this.
"""
from __future__ import annotations

import logging
import subprocess

from session_env import graphical_env

log = logging.getLogger("display_wake")


def wake_display() -> bool:
    """Force the display DPMS on. Returns True on success."""
    env = graphical_env()
    try:
        r = subprocess.run(["kscreen-doctor", "--dpms", "on"],
                           capture_output=True, text=True, env=env, timeout=10)
        if r.returncode == 0:
            log.info("Display woken (dpms on)")
            return True
        log.warning("kscreen-doctor --dpms on failed rc=%s: %s",
                    r.returncode, r.stderr.strip())
        return False
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("wake_display failed: %s", e)
        return False


def dpms_state() -> str | None:
    """Return the current DPMS mode ('on'/'off') for the first screen, or None."""
    env = graphical_env()
    try:
        r = subprocess.run(["kscreen-doctor", "--dpms", "show"],
                           capture_output=True, text=True, env=env, timeout=10)
        # Output like: "dpms mode for screen DP-1: off"
        for line in r.stdout.splitlines():
            if "dpms mode" in line and ":" in line:
                return line.rsplit(":", 1)[1].strip()
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("dpms_state failed: %s", e)
    return None
