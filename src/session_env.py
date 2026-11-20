"""Discover the graphical (KDE Wayland) session environment.

Launching or focusing GUI apps requires the user session's environment —
WAYLAND_DISPLAY, DBUS_SESSION_BUS_ADDRESS, XDG_RUNTIME_DIR, DISPLAY. Depending
on how the launcher is invoked (irexec, a user MQTT service, or the CLI) these
may or may not already be present, so we detect them from a running session
process (plasmashell) and merge them into the environment.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("session_env")

# The env vars a GUI app needs to reach the compositor / session bus.
# The KDE_* / XDG_CURRENT_DESKTOP vars are needed by kdotool v0.3.0, which
# refuses to run ("Unsupported KDE version") unless it can see KDE_SESSION_VERSION
# — so window find/focus/fullscreen/close all silently fail without it.
_WANTED = (
    "WAYLAND_DISPLAY",
    "DISPLAY",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
    "XDG_SESSION_TYPE",
    "XAUTHORITY",
    "KDE_SESSION_VERSION",
    "KDE_FULL_SESSION",
    "XDG_CURRENT_DESKTOP",
)

# Processes that reliably carry a full graphical-session environment.
_ANCHOR_PROCS = ("plasmashell", "kwin_wayland", "ksmserver")


def _pids_of(name: str) -> list[int]:
    pids = []
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/comm") as f:
                    if f.read().strip() == name:
                        pids.append(int(entry))
            except OSError:
                continue
    except OSError:
        pass
    return pids


def _read_environ(pid: int) -> dict[str, str]:
    env: dict[str, str] = {}
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            raw = f.read()
        for chunk in raw.split(b"\0"):
            if b"=" in chunk:
                k, v = chunk.split(b"=", 1)
                env[k.decode("utf-8", "ignore")] = v.decode("utf-8", "ignore")
    except OSError:
        pass
    return env


def graphical_env() -> dict[str, str]:
    """Return os.environ merged with the discovered session vars.

    If the wanted vars are already all present (e.g. invoked from inside the
    session), returns os.environ unchanged. Otherwise pulls them from a
    plasmashell/kwin process owned by the current user.
    """
    env = dict(os.environ)
    # KDE_SESSION_VERSION is in the required set because kdotool v0.3.0 needs it;
    # without it window ops silently fail even when WAYLAND_DISPLAY etc. are set.
    if all(k in env for k in ("WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS",
                              "XDG_RUNTIME_DIR", "KDE_SESSION_VERSION")):
        return env

    my_uid = os.getuid()
    for proc in _ANCHOR_PROCS:
        for pid in _pids_of(proc):
            try:
                if os.stat(f"/proc/{pid}").st_uid != my_uid:
                    continue
            except OSError:
                continue
            src = _read_environ(pid)
            if "WAYLAND_DISPLAY" in src or "DISPLAY" in src:
                # Fill any missing wanted var from the session process (don't
                # clobber values we already have).
                for k in _WANTED:
                    if k in src and k not in env:
                        env[k] = src[k]
                log.debug("Merged session env from %s (pid %s)", proc, pid)
                return env
    log.warning("Could not find a graphical session process to borrow env from")
    return env
