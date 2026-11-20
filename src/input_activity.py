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

IMPORTANT correctness notes (2026-10-23):
  * We only watch REAL user-input devices. ACPI / display / CEC pseudo-devices
    ("Power Button", "Video Bus", a display connector like "DP-1") are ALSO
    EV_KEY-capable but emit events the compositor correctly ignores — e.g. a
    DPMS/CEC event fires right as the session goes idle. Counting those as user
    input pinned seconds_since_last() to ~0 and made idle-off veto EVERY real
    idle as a "deaf swayidle", so it never powered off. We exclude them by name.
  * We bump 'last input' ONLY when we actually read event bytes (n>0). A
    disconnected device hits EOF: select() then reports its fd readable forever
    and os.read() returns 0 bytes. The old code stamped "input now" on that empty
    read, so a single dead display/CEC fd (which EOFs exactly when the TV turns
    off) kept 'last input' permanently fresh. We now drop EOF'd fds instead.
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

# Device NAME patterns to EXCLUDE: ACPI / display / CEC pseudo-devices that are
# EV_KEY-capable but are NOT real user input (power/sleep/lid buttons, the ACPI
# "Video Bus", and display-connector / CEC inputs like "DP-1" / "HDMI-A-1").
# Real keyboards / mice / remotes (incl. the ydotoold virtual device that carries
# injected remote keys) never match these.
_NOISE_NAME = re.compile(
    r"(?i)(power button|sleep button|lid switch|video bus|"
    r"^(?:dp|hdmi|edp|dvi|vga)-?[0-9])")


class InputActivity:
    """Background watcher; exposes seconds_since_last()."""

    def __init__(self):
        self._last = time.monotonic()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fds: dict[int, str] = {}
        self._started = False

    # ---- device discovery ----
    @staticmethod
    def _key_event_devices() -> list[str]:
        """Return /dev/input/eventN paths for REAL user-input devices that emit
        key events (keyboards, mice, remotes/virtual), excluding ACPI/display/CEC
        pseudo-devices (see _NOISE_NAME). Parses /proc/bus/input/devices."""
        out: list[str] = []
        seen: set[str] = set()
        try:
            blocks = open("/proc/bus/input/devices").read().split("\n\n")
        except OSError:
            # Fallback: try every event node (can't name-filter here).
            return sorted(set(glob.glob("/dev/input/event*")))
        for b in blocks:
            if not b.strip():
                continue
            ev = re.search(r"B: EV=([0-9a-fA-F]+)", b)
            h = re.search(r"H: Handlers=(.*)", b)
            nm = re.search(r'N: Name="(.*)"', b)
            if not (ev and h):
                continue
            if not (int(ev.group(1), 16) & _EV_KEY):
                continue
            name = nm.group(1) if nm else ""
            if _NOISE_NAME.search(name):
                log.debug("input-activity: skipping non-user device %r", name)
                continue
            m = re.search(r"event\d+", h.group(1))
            if m:
                path = "/dev/input/" + m.group(0)
                if path not in seen:          # de-dup defensively
                    seen.add(path)
                    out.append(path)
        return out

    def _open_all(self):
        for path in self._key_event_devices():
            if path in self._fds.values():    # already watching it
                continue
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                self._fds[fd] = path
            except OSError as e:
                log.debug("cannot open %s: %s", path, e)
        if not self._fds:
            log.warning("input-activity: no readable input devices; relying on "
                        "swayidle only")

    def _drop_fd(self, fd: int):
        """Stop watching a dead/errored fd (EOF on disconnect) and close it."""
        self._fds.pop(fd, None)
        try:
            os.close(fd)
        except OSError:
            pass

    # ---- lifecycle ----
    def start(self):
        if self._started:                     # idempotent: never re-open/leak
            return
        self._started = True
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
            self._drop_fd(fd)

    def _run(self):
        while not self._stop.is_set():
            fds = list(self._fds)
            if not fds:
                # Every device disconnected (e.g. all unplugged). Wait and retry;
                # seconds_since_last keeps growing, which is the correct "no input".
                if self._stop.wait(5):
                    break
                continue
            try:
                r, _, _ = select.select(fds, [], [], 1.0)
            except (OSError, ValueError):
                # A dead fd slipped into the set — prune anything invalid.
                for fd in list(self._fds):
                    try:
                        os.fstat(fd)
                    except OSError:
                        self._drop_fd(fd)
                continue
            if not r:
                continue
            got_real = False
            for fd in r:
                read_any = False
                try:
                    while True:
                        # SECURITY: we NEVER inspect, decode, store, or log the
                        # bytes. We read only to drain the device buffer and treat
                        # "there were bytes to read" as "input happened". The bytes
                        # are discarded at once — a presence detector, not a
                        # keylogger. Do not add any handling of `chunk` beyond
                        # discarding it, or this becomes keystroke logging.
                        chunk = os.read(fd, 4096)
                        n = len(chunk)
                        del chunk            # discard content immediately
                        if n == 0:
                            # EOF: device disconnected. Stop watching it so select
                            # doesn't spin-return it forever (which used to pin
                            # 'last input' to now and veto every real idle).
                            self._drop_fd(fd)
                            break
                        read_any = True
                        if n < 4096:
                            break
                except (BlockingIOError, InterruptedError):
                    # Spurious readable with no data right now — NOT real input.
                    pass
                except OSError:
                    self._drop_fd(fd)
                    continue
                if read_any:
                    got_real = True
            # Only count as activity if a device yielded real event bytes.
            if got_real:
                with self._lock:
                    self._last = time.monotonic()

    def seconds_since_last(self) -> float:
        with self._lock:
            return max(0.0, time.monotonic() - self._last)

    @property
    def active(self) -> bool:
        """True if the watcher is running with at least one device open."""
        return bool(self._fds) and self._thread is not None
