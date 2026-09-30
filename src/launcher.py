#!/usr/bin/env python3
"""App launcher with launch-or-focus for KDE Plasma Wayland.

Given an app key (e.g. "plex", "netflix"), either focus its existing window
if it's already running, or launch it. Focus uses kdotool driving KWin's
scripting API — the reliable way to raise windows on Wayland.

Can be invoked three ways, all sharing this logic:
  - CLI:   launcher.py plex          (used by irexec for physical remote buttons)
  - import: Launcher().activate("plex")  (used by the MQTT/HA service)

App registry entries:
  match  — list of (attribute, regex) to identify the app's window
           attribute is one of: class, name, role
  launch — argv list to start the app when not running
"""
from __future__ import annotations

import fcntl
import logging
import os
import re
import subprocess
import sys
import time

from session_env import graphical_env
import source_sync

log = logging.getLogger("launcher")

# --- App registry ---------------------------------------------------------
# match: window is considered "this app" if ANY (attr, pattern) matches.
# launch: command to start it (via os.setpgrp so it detaches from us).
# fullscreen: force the window fullscreen after launch/focus (default True).
# launch_wait: max seconds to wait for the window to appear after launching,
#              so we can fullscreen it.
# Default AV source for launched apps: the host is on the AVR's MPLAY input
# and whichever TV input the bridge has AUTO-DETECTED (see _default_source).
# When an app is launched via a remote NUMBER button, the launcher nudges the
# AVR/TV back to this source IF a device is powered on but on the wrong input
# (see source_sync.ensure_source — only acts when actually wrong, never touches
# power). Override per-app by giving that app its own "source" dict (e.g. a
# future button that wants a different AVR source):
#   "source": {"avr": "GAME", "tv": "HDMI-2"}
# Keys are optional: include only the device you want corrected. A per-app
# "source" bypasses TV auto-detection (it's an explicit choice).
_FALLBACK_TV_INPUT = os.environ.get("SCENE_TV_INPUT", "HDMI-1")


def _default_source() -> dict:
    """Build the default AV source at launch time.

    The TV input is read from the bridge's detected value (retained MQTT
    topic) so it follows the physical wiring instead of a hardcoded HDMI port;
    it falls back to SCENE_TV_INPUT if the bridge hasn't published. Resolved
    per-launch (not at import) because the irexec/CLI path doesn't load
    config.env before import, and the wiring can change between launches.
    """
    return {
        "avr": os.environ.get("SCENE_AVR_INPUT", "MPLAY"),
        "tv": source_sync.retained_tv_input(_FALLBACK_TV_INPUT),
    }

# Per-app Chrome profile dirs live here. Each Chrome --app gets its own so it's
# an independent process (see the chrome_app apps below).
_CHROME_PROFILES = os.path.expanduser(
    os.environ.get("CHROME_PROFILES_DIR", "~/.config/harmonia-chrome"))

APPS: dict[str, dict] = {
    "plex": {
        "label": "Plex",
        # Match by window CLASS only. The Bigscreen homescreen shows a
        # plasmashell-owned surface *titled* "Plex HTPC" (tile/launch feedback);
        # matching on name would treat that ghost as a running Plex and refuse to
        # launch. The real app window has class "Plex HTPC"; plasmashell's is
        # "org.kde.plasmashell", so class-only matching avoids the false positive.
        "match": [("class", r"(?i)plex\s*htpc")],
        "launch": ["snap", "run", "plex-htpc"],
        # pgrep pattern to detect the running process even before its window
        # appears (covers the several-second startup gap where a window-only
        # check would miss it and wrongly launch a second instance).
        "proc": r"plex-htpc/.*/(bin/Plex|Plex\.sh)",
        # Per-app environment overrides (None => unset the variable for this
        # launch only). In the Plasma Bigscreen session the shell exports
        # QT_QUICK_CONTROLS_STYLE=org.kde.breeze and QT_QPA_PLATFORMTHEME=KDE to
        # every child. Plex HTPC's QML UI then tries to load the Breeze QML style,
        # but the confined snap can't see the host's org.kde.breeze module and
        # dies with: 'module "org.kde.breeze" is not installed' (constant respawn
        # / "spinning"). Clearing these lets the snap use its bundled default
        # style. Harmless in a normal Plasma session (the vars aren't set there).
        "env": {"QT_QUICK_CONTROLS_STYLE": None, "QT_QPA_PLATFORMTHEME": None},
        "fullscreen": True,
        "launch_wait": 20,
    },
    "netflix": {
        "label": "Netflix",
        # Match ONLY the Chrome --app window, NOT a Firefox tab titled "Netflix".
        # Verified empirically: Chrome --app windows get a distinct window class
        # like "chrome-www.netflix.com__-Default". Firefox is "firefox_firefox",
        # so matching on the chrome-<domain> class avoids the false positive.
        "match": [("class", r"(?i)^chrome-www\.netflix\.com")],
        # --ozone-platform=wayland is required for Chrome to open on this Wayland
        # session when launched outside an existing browser context.
        # --start-fullscreen asks Chrome to open fullscreen; we also enforce it
        # via the window manager below as a belt-and-braces fallback.
        # NOTE: --enable-spatial-navigation was tried (to give the D-pad a 2-D
        # focus ring on the browse grid) but REMOVED: it parks focus on the
        # play/pause button during playback, so OK/Space fire a double toggle
        # (button activation + the site's own play/pause shortcut = pause-then-
        # play). The keyboard spacebar hit it too, and it can't be intercepted
        # (Chrome blocks --load-extension; Netflix has no alt play/pause key).
        # Not worth the regression — Netflix's own UI handles the arrow keys.
        # --password-store=basic: keep Chrome's cookie-encryption key in its own
        # profile (Local State) instead of the KDE wallet. Under our passwordless
        # SDDM autologin the wallet never unlocks (no login password is entered,
        # and pam_kwallet5 doesn't match the running kwalletd6), so Chrome
        # couldn't decrypt its saved Netflix auth cookie and logged us out every
        # boot. 'basic' removes the keyring dependency so the login persists.
        # Each Chrome --app runs in its OWN isolated profile (--user-data-dir) so
        # it's a fully independent browser process: independently launchable,
        # closable, and crash-isolated. Without this, ONE shared Chrome process
        # serves every --app window, so a wedged/closed window drags the others
        # down and you can't close one app without the other. Cost: separate
        # cookie store => one-time login per app (persists afterward).
        "launch": ["google-chrome", "--ozone-platform=wayland",
                   "--password-store=basic",
                   f"--user-data-dir={_CHROME_PROFILES}/netflix",
                   "--start-fullscreen", "--app=https://www.netflix.com"],
        # chrome_app=True => window-only running check + per-app wedged-Chrome
        # recovery (see activate()). No "proc" match: unreliable for Chrome.
        "chrome_app": True,
        "profile": "netflix",
        "fullscreen": True,
        "launch_wait": 15,
    },
    "firefox": {
        "label": "Firefox",
        # Snap Firefox uses window class "firefox_firefox". Firefox has built-in
        # single-instance remoting, so a second launch opens a new WINDOW rather
        # than a new process — matching the existing window here lets us raise it
        # instead (launch-or-focus) so tapping the tile again doesn't pile up
        # windows.
        "match": [("class", r"(?i)^firefox")],
        "launch": ["firefox"],
        # Don't force fullscreen: a browser is more useful with its chrome/tabs.
        "fullscreen": False,
        "launch_wait": 15,
    },
    "chrome": {
        "label": "Chrome",
        # Regular Chrome browser window class is "google-chrome". Anchored so it
        # does NOT match the Netflix --app window (class "chrome-www.netflix.com…"),
        # which is a separate registry entry.
        "match": [("class", r"(?i)^google-chrome$"),
                  ("class", r"(?i)^chromium$")],
        "launch": ["google-chrome-stable"],
        "fullscreen": False,
        "launch_wait": 15,
    },
    "sportsnet": {
        "label": "Sportsnet",
        # Isolated Chrome profile + stable window class (see Netflix above).
        "match": [("class", r"(?i)^chrome-watch\.sportsnet\.ca")],
        # --password-store=basic for login persistence under passwordless
        # autologin (keeps Chrome's cookie key out of the never-unlocked wallet).
        "launch": ["google-chrome", "--ozone-platform=wayland",
                   "--password-store=basic",
                   f"--user-data-dir={_CHROME_PROFILES}/sportsnet",
                   "--start-fullscreen", "--app=https://watch.sportsnet.ca/"],
        "chrome_app": True,
        "profile": "sportsnet",
        "fullscreen": True,
        "launch_wait": 15,
    },
    "iptvnator": {
        "label": "IPTVnator",
        # Snap Electron app. Window class confirmed empirically: "iptvnator".
        # Launched with its snap defaults (runs under XWayland via
        # --ozone-platform=x11, which the snap sets itself). It renders when
        # started from the graphical session (a bare SSH launch fails with
        # "Missing X server" — no DISPLAY/X-auth there, not an app problem).
        # NOTE: native Wayland (--ozone-platform=wayland) does NOT work — the
        # snap's bundled GL platform (core22 app wired to the empty/ancient
        # gnome-3-28-1804 provider) fails "MESA-LOADER: failed to open
        # radeonsi/swrast", so Electron can't make a GL context and never maps a
        # window. XWayland is the working path (input/fullscreen all fine).
        # fullscreen=True: the launcher forces it fullscreen after it maps.
        "match": [("class", r"(?i)^iptvnator$")],
        "launch": ["snap", "run", "iptvnator"],
        "proc": r"iptvnator/.*/iptvnator\.bin",
        "fullscreen": True,
        "launch_wait": 25,
    },
}


class Launcher:
    def __init__(self):
        self.env = graphical_env()
        self._source_thread = None

    # ---- kdotool helpers (reliable window enumeration) ----
    def _kdo(self, *args: str) -> str:
        try:
            r = subprocess.run(["kdotool", *args], capture_output=True,
                               text=True, env=self.env, timeout=10)
            return r.stdout.strip()
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("kdotool %s failed: %s", args, e)
            return ""

    def _all_windows(self) -> list[str]:
        out = self._kdo("search", "")
        return [w for w in out.splitlines() if w.strip()]

    def _window_attr(self, wid: str, attr: str) -> str:
        verb = {
            "class": "getwindowclassname",
            "name": "getwindowname",
            "role": "getwindowrole",
        }.get(attr, "getwindowname")
        return self._kdo(verb, wid)

    # Session-shell processes that may own decoy windows (e.g. a Plasma
    # Bigscreen homescreen tile titled after the app). A real app window is
    # owned by the app itself, never by the shell — so skip these owners.
    _SHELL_OWNERS = ("plasmashell", "kwin_wayland", "kwin_x11", "ksmserver",
                     "plasma-bigscreen")

    def _window_owner_comm(self, wid: str) -> str:
        pid = self._kdo("getwindowpid", wid)
        if not pid.isdigit():
            return ""
        try:
            with open(f"/proc/{pid}/comm") as f:
                return f.read().strip()
        except OSError:
            return ""

    def find_window(self, app_key: str) -> str | None:
        """Return the window id of the app if running, else None.

        Enumerates windows and matches against the registry, rather than using
        `kdotool search <pattern>` which is unreliable on this setup. Windows
        owned by the session shell (plasmashell/kwin) are skipped so a Bigscreen
        homescreen tile titled after the app can't be mistaken for the app.
        """
        spec = APPS[app_key]["match"]
        # Cache attributes per window to limit kdotool calls.
        for wid in self._all_windows():
            if self._window_owner_comm(wid) in self._SHELL_OWNERS:
                continue
            for attr, pattern in spec:
                val = self._window_attr(wid, attr)
                if val and re.search(pattern, val):
                    log.info("Found %s window %s (%s=%r)", app_key, wid, attr, val)
                    return wid
        return None

    def _proc_running(self, app_key: str) -> bool:
        """True if the app's process is already running (via pgrep pattern).

        Covers the startup gap: an app can be launched but not yet have a
        matchable window for several seconds, during which a window-only check
        would wrongly conclude it's not running and launch a duplicate.
        """
        pat = APPS[app_key].get("proc")
        if not pat:
            return False
        try:
            r = subprocess.run(["pgrep", "-f", pat], capture_output=True,
                               text=True, timeout=5)
            return r.returncode == 0 and bool(r.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            return False

    def _fullscreen(self, wid: str):
        """Force a window fullscreen via the window manager (app-agnostic)."""
        self._kdo("windowstate", "--add", "FULLSCREEN", wid)

    def _wait_for_window(self, app_key: str, timeout: float) -> str | None:
        """Poll for the app's window to appear (after launching)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            wid = self.find_window(app_key)
            if wid:
                return wid
            time.sleep(0.5)
        return None

    # ---- public API ----
    def activate(self, app_key: str, sync_source: bool = True) -> str:
        """Focus the app if running, else launch it. Returns an action string.

        Serialized per app with a file lock so two near-simultaneous invocations
        (e.g. an IR remote double-sending the button) can't both pass the
        "not running" check and launch duplicate instances.

        sync_source: if True (default), also nudge the AVR/TV to this app's
        expected input when a device is powered on but on the wrong source
        (see source_sync). Runs in the background so it never slows the launch.
        """
        if app_key not in APPS:
            log.error("Unknown app: %s", app_key)
            return "unknown"

        spec = APPS[app_key]
        want_fs = spec.get("fullscreen", True)

        # Correct the AV source in the background (only acts if a device is on
        # the wrong input; never touches power). Fires for both launch and
        # focus so switching back to an app also fixes the source. A per-app
        # "source" overrides the auto-detected default.
        if sync_source:
            src = spec.get("source") or _default_source()
            try:
                self._source_thread = source_sync.ensure_source_async(src)
            except Exception as e:
                log.debug("source sync dispatch failed: %s", e)

        # The launch DECISION (check-if-running + launch) is serialized under a
        # per-app lock so concurrent invocations (IR double-send) can't both
        # decide to launch. The lock is released before the long window-wait.
        launched = self._decide_and_launch(app_key)

        if launched == "error":
            return "error"

        # Focus + fullscreen the window (whether we launched or it existed).
        wid = self.find_window(app_key)
        if not wid:
            wid = self._wait_for_window(app_key, spec.get("launch_wait", 15))
        # Wedged-Chrome recovery: if a Chrome --app launched but no window ever
        # appeared, its isolated profile's Chrome is wedged. Kill ONLY that app's
        # process (safe now that each app has its own profile — never touches the
        # others) and relaunch once.
        if not wid and spec.get("chrome_app") and launched == "launched":
            log.warning("%s launched but no window — assuming wedged Chrome; "
                        "killing this app's profile and retrying once", app_key)
            self._kill_chrome_profile(app_key)
            time.sleep(1)
            self._decide_and_launch(app_key)   # relaunch (window-only check)
            wid = self._wait_for_window(app_key, spec.get("launch_wait", 15))
        if wid:
            self._kdo("windowactivate", wid)
            if want_fs:
                self._fullscreen(wid)
        elif want_fs:
            log.warning("%s window did not appear in time", app_key)
        return launched

    # ---- close / kill ----
    def _kill_chrome_profile(self, app_key: str, sig: str = "-TERM") -> bool:
        """Kill the Chrome process for one app's isolated profile only. Matches
        on the unique --user-data-dir so it never affects other apps' Chrome."""
        profile = APPS.get(app_key, {}).get("profile")
        if not profile:
            return False
        # Match the unique profile PATH (not "--user-data-dir=..."): a pattern
        # starting with "--" makes pkill/pgrep treat it as options. The path is
        # unique to this app's Chrome cmdline, so it never hits another app.
        pat = f"{_CHROME_PROFILES}/{profile}"
        try:
            subprocess.run(["pkill", sig, "-f", pat], timeout=5)
            return True
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("kill %s chrome failed: %s", app_key, e)
            return False

    def close(self, app_key: str) -> str:
        """Close one app without affecting others. For Chrome --app apps this
        kills only that app's isolated profile process; for others it closes the
        matched window via the window manager. Returns 'closed' or 'not-running'.
        """
        if app_key not in APPS:
            return "unknown"
        spec = APPS[app_key]
        if spec.get("chrome_app"):
            if not self.find_window(app_key) and not self._chrome_profile_running(app_key):
                return "not-running"
            # graceful, then force any straggler
            self._kill_chrome_profile(app_key, "-TERM")
            time.sleep(1.5)
            self._kill_chrome_profile(app_key, "-KILL")
            log.info("%s closed (isolated profile killed)", app_key)
            return "closed"
        wid = self.find_window(app_key)
        if not wid:
            return "not-running"
        self._kdo("windowclose", wid)
        log.info("%s window closed", app_key)
        return "closed"

    def _chrome_profile_running(self, app_key: str) -> bool:
        profile = APPS.get(app_key, {}).get("profile")
        if not profile:
            return False
        pat = f"{_CHROME_PROFILES}/{profile}"   # path, not --user-data-dir=
        try:
            r = subprocess.run(["pgrep", "-f", pat], capture_output=True, timeout=5)
            return r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    @staticmethod
    def _runtime_dir() -> str:
        return os.environ.get("XDG_RUNTIME_DIR", "/tmp")

    # Seconds after a launch during which we will NOT launch the same app again.
    # Covers the startup gap: between Popen and the app's process/window becoming
    # detectable (snap/Chrome take 1-3s), a second button press would otherwise
    # pass the "not running" check and spawn a duplicate. A stamp file written
    # under the lock closes that gap. Configurable via LAUNCH_COOLDOWN_SECS.
    _LAUNCH_COOLDOWN = float(os.environ.get("LAUNCH_COOLDOWN_SECS", "12"))

    def _recent_launch(self, app_key: str) -> bool:
        """True if this app was launched within the cooldown window (its startup
        may still be in progress and not yet detectable as running)."""
        stamp = f"{self._runtime_dir()}/avr-launch-{app_key}.stamp"
        try:
            age = time.time() - os.path.getmtime(stamp)
        except OSError:
            return False
        return age < self._LAUNCH_COOLDOWN

    def _mark_launched(self, app_key: str):
        stamp = f"{self._runtime_dir()}/avr-launch-{app_key}.stamp"
        try:
            with open(stamp, "w") as f:
                f.write(str(time.time()))
        except OSError:
            pass

    def _decide_and_launch(self, app_key: str) -> str:
        """Under a per-app lock: if the app is already running (window OR
        process) or was just launched (still starting up), do nothing; otherwise
        launch it. Returns 'focused' (already running / still starting),
        'launched', or 'error'. Held only for the fast decision, not the
        subsequent window-wait."""
        lock_path = f"{self._runtime_dir()}/avr-launch-{app_key}.lock"
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            # Chrome --app apps: decide by WINDOW only. Their proc match is
            # unreliable (one shared browser process, stale cmdline), which is
            # what made the launcher "focus" a window that no longer existed and
            # spin. Non-Chrome apps still use the proc fallback for the startup
            # gap.
            chrome_app = APPS[app_key].get("chrome_app")
            running = self.find_window(app_key)
            if not chrome_app and not running:
                running = self._proc_running(app_key)
            if running:
                log.info("%s already running — focusing, not relaunching", app_key)
                return "focused"
            # Startup-gap guard: a very recent launch may not be detectable as
            # running yet. Don't launch a second copy; the earlier launch is
            # still coming up.
            if self._recent_launch(app_key):
                log.info("%s launched moments ago — still starting, not "
                         "relaunching", app_key)
                return "focused"
            cmd = APPS[app_key]["launch"]
            log.info("Launching %s: %s", app_key, " ".join(cmd))
            # Apply any per-app environment overrides (a None value unsets the
            # variable for this launch only — used to strip session-wide Qt
            # style/theme vars the app's confined snap can't satisfy).
            launch_env = dict(self.env)
            for k, v in APPS[app_key].get("env", {}).items():
                if v is None:
                    launch_env.pop(k, None)
                else:
                    launch_env[k] = v
            try:
                subprocess.Popen(cmd, start_new_session=True, env=launch_env,
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            except OSError as e:
                log.error("Failed to launch %s: %s", app_key, e)
                return "error"
            # Record the launch time so a press arriving during the startup gap
            # focuses instead of launching a duplicate.
            self._mark_launched(app_key)
            return "launched"
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)


def main(argv: list[str]) -> int:
    logging.basicConfig(level="INFO", format="%(levelname)s %(name)s: %(message)s")
    # Usage: launcher.py <app>            -> launch/focus
    #        launcher.py close <app>      -> close just that app (not the others)
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(f"Usage: {argv[0]} <app> | close <app>   (apps: {', '.join(APPS)})")
        return 2
    launcher = Launcher()
    if argv[1] == "close":
        if len(argv) != 3:
            print(f"Usage: {argv[0]} close <app>")
            return 2
        result = launcher.close(argv[2])
        print(result)
        return 0 if result in ("closed", "not-running") else 1
    result = launcher.activate(argv[1])
    print(result)
    # The number-button/CLI process exits right after this; wait for the source
    # correction (a daemon thread) to finish so it isn't killed mid-flight.
    t = getattr(launcher, "_source_thread", None)
    if t is not None:
        t.join(timeout=8)
    return 0 if result in ("focused", "launched") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
