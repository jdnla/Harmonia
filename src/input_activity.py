"""Reliable 'last user input' tracker by watching kernel input devices directly.

Why this exists: idle-off relies on swayidle (the Wayland idle protocol) to know
when the user is active. The long-lived swayidle can go DEAF — it keeps running
but silently stops delivering events — after which idle-off counts down and
powers off even while the user is actively pressing the remote or keyboard.

This module watches /dev/input/event* directly (the service user is in the
`input` group), so it sees EVERY real input — keyboard, mouse, AND the remote
(whose presses arrive as the ydotoold virtual device's key events) — completely
independent of swayidle/Wayland. idle-off consults `seconds_since_last()` as an
authoritative second opinion: if there was real input within the idle window, a
swayidle 'IDLE' is bogus and we defer instead of powering off.

Stdlib only (no evdev dependency): we open each EV_KEY-capable event device
non-blocking and select() on them; any readable data = an input event = activity.
We only need the *timing* of input, not decode it, so reading raw bytes and
bumping a monotonic timestamp is enough. Never raises out to callers.
"""
from __future__ import annotations

import glob
import os
import re
import select
import threading
import time
import logging

log = logging.getLogger("input_activity")

# EV_KEY bit in the device's EV capability bitmask (linux/input-event-codes.h).
_EV_KEY = 0x02


class InputActivity:
    """Background watcher; exposes seconds_since_last()."""

    def __init__(self):
        self._last = time.monotonic()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fds: dict[int, str] = {}

    # ---- device discovery ----
    @staticmethod
    def _key_event_devices() -> list[str]:
        """Return /dev/input/eventN paths for devices that emit key events
        (keyboards, mice, remotes/virtual). Parses /proc/bus/input/devices."""
        out: list[str] = []
        try:
            blocks = open("/proc/bus/input/devices").read().split("\n\n")
        except OSError:
            # Fallback: try every event node.
            return sorted(glob.glob("/dev/input/event*"))
        for b in blocks:
            if not b.strip():
                continue
            ev = re.search(r"B: EV=([0-9a-fA-F]+)", b)
            h = re.search(r"H: Handlers=(.*)", b)
            if not (ev and h):
                continue
            if not (int(ev.group(1), 16) & _EV_KEY):
                continue
            m = re.search(r"event\d+", h.group(1))
            if m:
                out.append("/dev/input/" + m.group(0))
        return out

    def _open_all(self):
        for path in self._key_event_devices():
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                self._fds[fd] = path
            except OSError as e:
                log.debug("cannot open %s: %s", path, e)
        if not self._fds:
            log.warning("input-activity: no readable input devices; relying on "
                        "swayidle only")

    # ---- lifecycle ----
    def start(self):
        self._open_all()
        if not self._fds:
            return
        log.info("input-activity watching %d device(s): %s",
                 len(self._fds), ", ".join(sorted(self._fds.values())))
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        for fd in list(self._fds):
            try:
                os.close(fd)
            except OSError:
                pass

    def _run(self):
        fds = list(self._fds)
        while not self._stop.is_set():
            try:
                r, _, _ = select.select(fds, [], [], 1.0)
            except (OSError, ValueError):
                break
            if not r:
                continue
            for fd in r:
                # SECURITY: we intentionally NEVER inspect, decode, store, or log
                # the bytes. We read only to drain the device's buffer (so select
                # doesn't spin) and treat "there was something to read" as "input
                # happened". The bytes are discarded immediately — this is a
                # presence detector, not a keylogger. Do not add any handling of
                # `chunk` beyond discarding it, or this becomes keystroke logging.
                try:
                    while True:
                        chunk = os.read(fd, 4096)
                        n = len(chunk)
                        del chunk            # discard keystroke content at once
                        if n < 4096:
                            break
                except (BlockingIOError, InterruptedError):
                    pass
                except OSError:
                    continue
            with self._lock:
                self._last = time.monotonic()

    def seconds_since_last(self) -> float:
        with self._lock:
            return max(0.0, time.monotonic() - self._last)

    @property
    def active(self) -> bool:
        """True if the watcher is running with at least one device open."""
        return bool(self._fds) and self._thread is not None
