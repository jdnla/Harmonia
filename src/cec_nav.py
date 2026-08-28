#!/usr/bin/env python3
"""CEC D-pad navigation service.

The TV forwards remote D-pad/OK/Back presses to us (Playback Device 2) as CEC
USER_CONTROL_PRESSED messages. This service reads them via `cec-follower -m`
(which works as a normal user in the `video` group — no root, unlike monitor
mode) and injects the corresponding keystroke into the focused app via ydotool.

We use CEC rather than the USB-UIRT IR path for navigation because CEC gives
clean, unambiguous press events, whereas this remote's NEC IR frames decode
unreliably (repeat frames misread as new presses). NOTE: the TV sends BOTH CEC
and IR for each press, so the IR D-pad mappings must be disabled to avoid double
input — the installer handles that.

ui-cmd -> Linux input keycode mapping (from observed cec-follower output).
"""
from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time

import nav_profiles

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("cec_nav")

CEC_DEVICE = os.environ.get("CEC_DEVICE", "/dev/cec0")
YDOTOOL = os.environ.get("YDOTOOL_BIN", "/usr/local/bin/ydotool")
XDG_RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
YDOTOOL_SOCKET = os.environ.get("YDOTOOL_SOCKET", f"{XDG_RUNTIME_DIR}/.ydotool_socket")

# CEC ui-cmd name -> Linux input event keycode
KEYMAP: dict[str, int] = {
    "up": 103,       # KEY_UP
    "down": 108,     # KEY_DOWN
    "left": 105,     # KEY_LEFT
    "right": 106,    # KEY_RIGHT
    "select": 28,    # KEY_ENTER  (OK)
    "back": 1,       # KEY_ESC
    "exit": 1,
    "root menu": 139,   # KEY_MENU
    "contents menu": 139,
    "play": 164,     # KEY_PLAYPAUSE
    "pause": 164,
}

# Debounce: the TV can emit the same ui-cmd a couple times in quick succession
# for a single press. Drop repeats of the same key within this window. Kept
# short (CEC is far cleaner than IR, so this mostly catches genuine dupes).
DEBOUNCE_MS = int(os.environ.get("CEC_NAV_DEBOUNCE_MS", "120"))

# Per-app nav profiles (nav_profiles.py) remap the D-pad for apps that don't do
# arrow-key spatial navigation (e.g. IPTVnator -> Tab/Shift+Tab). Set to 0 to
# disable and always use the default KEYMAP. The focused-window class is resolved
# via kdotool and cached briefly so we don't shell out on every press.
NAV_PROFILES_ENABLED = os.environ.get("NAV_PROFILES", "1") not in ("0", "false", "False")
KDOTOOL = os.environ.get("KDOTOOL_BIN", "kdotool")
# How long (ms) to trust a cached focused-window class before re-querying. A
# short TTL keeps profile switching snappy when the user changes apps while
# avoiding a kdotool call on every single D-pad press during fast scrolling.
FOCUS_CACHE_MS = int(os.environ.get("NAV_FOCUS_CACHE_MS", "400"))

_UICMD_RE = re.compile(r"ui-cmd:\s*([a-z][a-z ]*?)\s*\(0x")


class CecNav:
    def __init__(self):
        self._last: dict[str, float] = {}
        self._proc: subprocess.Popen | None = None
        self._stop = False
        self._focus_class = ""
        self._focus_at = 0.0  # monotonic ms of last focus query

    def _focused_class(self) -> str:
        """Return the last-known focused-window class WITHOUT blocking.

        CRITICAL: the CEC read loop is single-threaded — shelling out to kdotool
        HERE (as the original nav-profiles code did) stalls the reader on every
        press, so CEC frames arriving during the subprocess round-trip queue up
        or get missed (this was the D-pad lag/miss regression). Instead a
        background thread (_focus_poller) refreshes this cache off the hot path,
        and _inject just reads the cached string. Empty string -> default keymap."""
        return self._focus_class

    def _focus_poller(self):
        """Background: periodically refresh the focused-window class via kdotool,
        so _inject never has to. Only runs if profiles are enabled (otherwise the
        class is never consulted). Cheap and off the CEC read path."""
        interval = FOCUS_CACHE_MS / 1000.0
        while not self._stop:
            try:
                r = subprocess.run(
                    [KDOTOOL, "getactivewindow", "getwindowclassname"],
                    capture_output=True, text=True, timeout=1)
                self._focus_class = r.stdout.strip() if r.returncode == 0 else ""
            except (OSError, subprocess.SubprocessError) as e:
                log.debug("focus query failed: %s", e)
                self._focus_class = ""
            # Sleep in small slices so stop() is responsive.
            slept = 0.0
            while slept < interval and not self._stop:
                time.sleep(min(0.1, interval - slept))
                slept += 0.1

    def _inject(self, key: str):
        # Consult the per-app nav profile first: some apps (e.g. IPTVnator) don't
        # do arrow-key spatial nav and need the D-pad remapped (-> Tab/Shift+Tab).
        # Uses the CACHED focused class (refreshed by _focus_poller) so we never
        # block the CEC reader on a kdotool call. A profile may return a chord
        # list to inject instead, or [] to swallow.
        if NAV_PROFILES_ENABLED:
            chords = nav_profiles.lookup(self._focused_class(), key)
            if chords is not None:
                self._inject_chords(key, chords)
                return

        code = KEYMAP.get(key)
        if code is None:
            log.debug("Unmapped ui-cmd: %s", key)
            return
        now = time.monotonic() * 1000
        last = self._last.get(key, 0)
        if now - last < DEBOUNCE_MS:
            return  # duplicate within debounce window
        self._last[key] = now
        self._ydotool([f"{code}:1", f"{code}:0"])
        log.info("nav %s -> keycode %d", key, code)

    def _inject_chords(self, key: str, chords: list):
        """Inject a profile-remapped button (list of key chords). Debounced by
        the same window as the default path."""
        now = time.monotonic() * 1000
        last = self._last.get(key, 0)
        if now - last < DEBOUNCE_MS:
            return
        self._last[key] = now
        if not chords:  # explicitly swallowed by the profile
            log.debug("nav %s swallowed by profile (%s)", key, self._focus_class)
            return
        args = nav_profiles.chords_to_ydotool_args(chords)
        self._ydotool(args)
        log.info("nav %s -> profile[%s] %s", key, self._focus_class, args)

    def _ydotool(self, key_args: list[str]):
        env = dict(os.environ)
        env["YDOTOOL_SOCKET"] = YDOTOOL_SOCKET
        try:
            subprocess.run([YDOTOOL, "key", *key_args],
                           env=env, timeout=3,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("inject %s failed: %s", key_args, e)

    def _ensure_adapter_configured(self) -> bool:
        """Ensure the CEC adapter has claimed a logical address.

        cec-follower (v4l-utils >= ~1.32) refuses to run and exits immediately
        with "missing logical address(es)" if the adapter is unconfigured. The
        adapter can lose its claim on any display/connector re-init even if
        cec-playback.service ran at boot, so we (re)claim the playback role here
        before starting the follower. Idempotent and cheap.
        """
        try:
            r = subprocess.run(
                ["cec-ctl", "-d", CEC_DEVICE, "--playback",
                 "--osd-name", os.environ.get("CEC_OSD_NAME", "LinuxPC")],
                capture_output=True, text=True, timeout=10)
            ok = r.returncode == 0
            if ok:
                log.info("CEC adapter configured (playback role claimed)")
            else:
                log.warning("cec-ctl --playback failed rc=%s: %s",
                            r.returncode, r.stderr.strip())
            return ok
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("could not configure CEC adapter: %s", e)
            return False

    def _run_follower_once(self) -> bool:
        """Configure the adapter, run cec-follower, and pump its output until it
        exits. Returns True if it ran and later died (retry worthwhile), False if
        we were asked to stop."""
        # cec-follower needs a configured adapter or it exits instantly; claim
        # the playback role first (idempotent; also self-heals a lost claim).
        self._ensure_adapter_configured()
        cmd = ["cec-follower", "-d", CEC_DEVICE, "-m"]
        log.info("Starting CEC nav follower: %s", " ".join(cmd))
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1)
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            if self._stop:
                return False
            m = _UICMD_RE.search(line)
            if m:
                self._inject(m.group(1).strip())
        return not self._stop

    def run(self):
        # Start the focus poller (off-hot-path kdotool cache) only if per-app nav
        # profiles are enabled — otherwise the focused class is never consulted,
        # so there's no reason to run kdotool at all.
        if NAV_PROFILES_ENABLED:
            t = threading.Thread(target=self._focus_poller, daemon=True)
            t.start()
        # Retry IN-PROCESS rather than exiting for a systemd restart. On
        # power-on / display-wake the CEC adapter loses its logical-address
        # claim during display re-init, and cec-follower exits immediately until
        # it's ready again. Retrying here every RETRY_SECS (with a short backoff
        # cap) reattaches within ~1-2s of the adapter becoming ready — far
        # faster than the old exit + RestartSec + full-process-restart cycle,
        # which stacked multi-second waits into a 15-30s cold-start lag.
        retry = float(os.environ.get("CEC_NAV_RETRY_SECS", "1.0"))
        retry_max = float(os.environ.get("CEC_NAV_RETRY_MAX_SECS", "3.0"))
        delay = retry
        while not self._stop:
            started = time.monotonic()
            worthwhile = self._run_follower_once()
            if self._stop or not worthwhile:
                break
            # If the follower survived a while it was genuinely working; reset
            # the backoff so a later drop retries quickly again.
            if time.monotonic() - started > 10:
                delay = retry
            else:
                delay = min(delay * 1.5, retry_max)
            log.info("cec-follower exited (adapter not ready?); retrying in "
                     "%.1fs", delay)
            if self._stop_wait(delay):
                break

    def _stop_wait(self, secs: float) -> bool:
        """Sleep up to `secs`, returning True early if asked to stop."""
        end = time.monotonic() + secs
        while time.monotonic() < end:
            if self._stop:
                return True
            time.sleep(0.1)
        return self._stop

    def stop(self, *_):
        log.info("Stopping CEC nav")
        self._stop = True
        if self._proc:
            self._proc.terminate()


def main():
    nav = CecNav()
    signal.signal(signal.SIGTERM, nav.stop)
    signal.signal(signal.SIGINT, nav.stop)
    try:
        nav.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
