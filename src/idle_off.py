"""Auto power-off the AVR + TV after a period of inactivity.

Inactivity = ALL of: no mouse/keyboard/remote input (via the Wayland idle
protocol, watched by `swayidle`) AND nothing playing (playerctl/MPRIS). The
remote's D-pad counts as input automatically: cec_nav injects real uinput key
events via ydotool, which reset the compositor's idle timer.

When the idle timeout fires we re-check media; if something is playing we defer
(re-arm and wait again). Otherwise we show an on-screen warning TOAST (the same
layer-shell notification style as HA notifications) with a countdown; if input
resumes during the countdown we cancel, otherwise we publish the bridge's "Media
Center Off" scene (TV off, then AVR off). The PC itself stays on so services keep
running and it wakes instantly.

Runs inside the launcher service (it has the graphical-session env needed for
swayidle to reach the compositor). Enable/tune via env (config.env.example:
IDLE_OFF_*). Exposes an HA on/off toggle.
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time

from session_env import graphical_env
import media_control

log = logging.getLogger("idle_off")


class IdleOff:
    def __init__(self,
                 publish_off,                 # callable() -> fire off scene
                 timeout_secs: int = 1800,    # 30 min
                 warn_secs: int = 60,         # cancel window
                 enabled: bool = True,
                 notifier=None):              # NotifyPopup for the warning toast
        self.publish_off = publish_off
        self.timeout_secs = timeout_secs
        self.warn_secs = warn_secs
        self._enabled = enabled
        self._notifier = notifier
        self._env = graphical_env()
        self._stop = threading.Event()
        self._sway: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        # Watchdog: swayidle can go STALE — the process stays alive but its
        # Wayland idle-notification listener silently stops delivering events
        # (observed after display mode changes / DPMS sleep). Then _run's
        # blocking stdout read waits forever and idle-off never fires again. We
        # stamp the last swayidle event (spawn/IDLE/RESUME) and a watchdog thread
        # respawns swayidle if it's been silent far longer than it should be.
        self._last_sway_event = 0.0  # monotonic
        self._wd_thread: threading.Thread | None = None
        # Single-fire guard: a warning countdown is either pending or not. Prevents
        # the double-popup that happened when swayidle emitted IDLE more than once
        # (or respawned) while a warning was already in flight.
        self._warn_lock = threading.Lock()
        self._warn_active = False
        self._warn_timer: threading.Timer | None = None
        # Resume-watcher: the long-lived swayidle can go deaf after ~30 min (it
        # fires IDLE but never RESUME, until the watchdog respawns it much later).
        # A deaf swayidle means the warning popup never sees input and won't
        # cancel. So during the warning window we spawn a FRESH, short-lived
        # swayidle purely to catch "input resumed" (fresh instances reliably
        # deliver resume). It cancels the countdown the instant any input arrives.
        self._resume_sway: subprocess.Popen | None = None
        self._resume_thread: threading.Thread | None = None
        # Handle to the currently-shown warning toast, so we can dismiss it the
        # moment input cancels the countdown (otherwise the toast lingers on
        # screen for its full timeout even though the power-off was aborted —
        # which looks exactly like "the timeout didn't clear").
        self._warn_toast = None

    # ---- HA toggle ----
    def set_enabled(self, on: bool):
        self._enabled = on
        log.info("idle auto-off %s", "enabled" if on else "disabled")
        if not on:
            self._cancel_warning()

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ---- HA-settable timeout ----
    def set_timeout(self, secs: int):
        """Change the inactivity timeout at runtime. swayidle bakes the timeout
        into its launch args, so we update the value and respawn swayidle (the
        _run loop rebuilds its command each spawn); a pending warning is
        canceled so the new timeout applies cleanly from now."""
        secs = int(secs)
        if secs < 1:
            log.warning("ignoring invalid idle timeout: %s", secs)
            return
        if secs == self.timeout_secs:
            return
        log.info("idle auto-off timeout %ds -> %ds", self.timeout_secs, secs)
        self.timeout_secs = secs
        self._cancel_warning()
        # Respawn swayidle so it re-arms with the new timeout. Terminating it
        # makes the _run loop's read exit and reissue Popen with the new value.
        if self._sway is not None and self._sway.poll() is None:
            self._sway.terminate()

    @property
    def timeout(self) -> int:
        return self.timeout_secs

    # ---- lifecycle ----
    def start(self):
        self._last_sway_event = time.monotonic()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        # Watchdog respawns swayidle if it goes stale (see _watchdog).
        self._wd_thread = threading.Thread(target=self._watchdog, daemon=True)
        self._wd_thread.start()

    def stop(self):
        self._stop.set()
        if self._sway and self._sway.poll() is None:
            self._sway.terminate()
        self._cancel_warning()
        self._stop_resume_watcher()

    # ---- warning countdown state ----
    def _cancel_warning(self):
        with self._warn_lock:
            self._warn_active = False
            if self._warn_timer is not None:
                self._warn_timer.cancel()
                self._warn_timer = None
            toast = self._warn_toast
            self._warn_toast = None
        self._stop_resume_watcher()
        # Dismiss the on-screen warning toast right away.
        if toast is not None and self._notifier is not None:
            try:
                self._notifier.dismiss(toast)
            except Exception as e:
                log.debug("toast dismiss failed: %s", e)

    # ---- resume watcher (fresh swayidle during the warning window) ----
    def _start_resume_watcher(self):
        """Spawn a fresh, short-lived swayidle whose only job is to fire when
        input resumes during the warning countdown, and cancel the power-off.

        Fresh swayidle instances reliably deliver resume (unlike the long-lived
        one, which can go deaf). We use a tiny idle timeout so it arms almost
        immediately; the very next input then produces a resume line we act on.
        """
        self._stop_resume_watcher()  # ensure only one
        try:
            self._resume_sway = subprocess.Popen(
                ["swayidle", "-w", "timeout", "1", "echo RIDLE",
                 "resume", "echo RRESUME"],
                env=self._env, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1)
        except OSError as e:
            log.warning("resume-watcher swayidle failed to start: %s", e)
            self._resume_sway = None
            return
        self._resume_thread = threading.Thread(
            target=self._resume_loop, daemon=True)
        self._resume_thread.start()

    def _resume_loop(self):
        sw = self._resume_sway
        if sw is None or sw.stdout is None:
            return
        armed = False
        for line in sw.stdout:
            line = line.strip()
            if line == "RIDLE":
                # It has armed (seen the idle state). The NEXT resume is real
                # input from the user, so from here a RRESUME cancels.
                armed = True
            elif line == "RRESUME" and armed:
                # Only act if a warning is still up (avoid races with elapsed).
                if self._warn_active:
                    log.info("input resumed (resume-watcher) — canceling "
                             "idle power-off")
                    self._cancel_warning()
                return

    def _stop_resume_watcher(self):
        sw = self._resume_sway
        self._resume_sway = None
        if sw is not None and sw.poll() is None:
            try:
                sw.terminate()
            except Exception:
                pass

    # ---- core ----
    def _media_playing(self) -> bool:
        try:
            return media_control.status() == "playing"
        except Exception as e:
            log.debug("media status check failed: %s", e)
            return False

    def _run(self):
        """Drive swayidle: it calls back (prints a line) when the timeout of no
        input elapses, and again on resume. We read its stdout as the signal."""
        while not self._stop.is_set():
            # Rebuild the command each spawn so a respawn (e.g. after
            # set_timeout terminates swayidle) picks up the CURRENT timeout.
            cmd = [
                "swayidle", "-w",
                "timeout", str(self.timeout_secs),
                "echo IDLE",
                "resume", "echo RESUME",
            ]
            try:
                self._sway = subprocess.Popen(
                    cmd, env=self._env, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, text=True, bufsize=1)
            except OSError as e:
                log.error("failed to start swayidle: %s — idle-off disabled: %s",
                          e, e)
                return
            self._last_sway_event = time.monotonic()  # fresh spawn = alive now
            log.info("swayidle started (timeout %ds)", self.timeout_secs)
            assert self._sway.stdout is not None
            for line in self._sway.stdout:
                if self._stop.is_set():
                    break
                self._last_sway_event = time.monotonic()  # heartbeat
                line = line.strip()
                if line == "IDLE":
                    self._on_input_idle()
                elif line == "RESUME":
                    self._on_resume()
            # swayidle exited (died, session change, or watchdog killed a stale
            # one) — brief pause then respawn.
            if not self._stop.wait(2):
                continue

    def _watchdog(self):
        """Respawn swayidle if it goes STALE (alive but no events for too long).

        swayidle should emit IDLE within `timeout_secs` of the last input, and
        RESUME on the next input, so on any real machine we see an event at least
        every `timeout_secs` (+ the warn window). If we've seen NOTHING for
        comfortably longer than that, its listener has gone dead — terminate it so
        _run's read loop unblocks and respawns a fresh one. Checked periodically.
        """
        while not self._stop.wait(self._wd_interval()):
            if not self._enabled:
                # Disabled: keep the stamp fresh so we don't insta-kill on re-enable.
                self._last_sway_event = time.monotonic()
                continue
            silent = time.monotonic() - self._last_sway_event
            # Allow one full idle period + the warn window + generous margin.
            limit = self.timeout_secs + self.warn_secs + 300
            if silent > limit and self._sway is not None and self._sway.poll() is None:
                log.warning("swayidle silent %.0fs (> %ds) — presumed stale, "
                            "respawning", silent, limit)
                try:
                    self._sway.terminate()  # unblocks _run -> respawn
                except Exception as e:
                    log.debug("watchdog terminate failed: %s", e)

    def _wd_interval(self) -> float:
        # Check a few times per idle period, but not too chatty; min 60s.
        return max(60.0, self.timeout_secs / 4.0)

    def _on_resume(self):
        # Any input returned — cancel a pending warning if one is up.
        if self._warn_active:
            log.info("input resumed — canceling idle power-off")
        self._cancel_warning()

    def _on_input_idle(self):
        if not self._enabled:
            return
        # Input has been idle for `timeout_secs`. If media is playing, this is
        # NOT inactivity — defer (swayidle stays armed and will fire again on the
        # next idle period after playback stops / input resumes).
        if self._media_playing():
            log.info("input idle %ds but media is playing — not powering off",
                     self.timeout_secs)
            return
        # Single-fire guard: if a warning countdown is already running, ignore
        # duplicate IDLE events (this is what caused two popups).
        with self._warn_lock:
            if self._warn_active:
                return
            self._warn_active = True
        log.info("inactive %ds (no input, nothing playing) — warning before "
                 "power-off", self.timeout_secs)
        self._warn_then_off()

    # ---- warning + off ----
    def _warn_then_off(self):
        # Show a warning TOAST (same layer-shell style as HA notifications), then
        # start a warn_secs countdown. If input resumes (_on_resume) the countdown
        # is canceled; otherwise we power off. Non-blocking: the toast is
        # fire-and-forget and the countdown is a Timer.
        mins = self.timeout_secs // 60
        title = "Media Center"
        body = (f"No activity for {mins} min. Powering off the TV and receiver "
                f"in {self.warn_secs}s — press any key to stay on.")
        if self._notifier is not None:
            try:
                # amber accent; toast lifetime = the warning window so it stays up
                # for the whole countdown. Keep the handle so we can dismiss it
                # early if input cancels the countdown.
                self._warn_toast = self._notifier.show(
                    title, body, "", timeout=float(self.warn_secs),
                    opts={"accent": "#f39c12"})
            except Exception as e:
                log.warning("warning toast failed: %s", e)
        else:
            log.info("(no notifier) idle warning: %s", body)

        with self._warn_lock:
            if not self._warn_active:
                return  # canceled between the guard and here
            self._warn_timer = threading.Timer(self.warn_secs, self._warn_elapsed)
            self._warn_timer.daemon = True
            self._warn_timer.start()
        # Arm a fresh swayidle to catch input during the countdown (the main one
        # may be deaf). This is what makes the popup actually cancel on a keypress.
        self._start_resume_watcher()

    def _warn_elapsed(self):
        # Countdown finished with no input resume — power off (unless canceled).
        with self._warn_lock:
            if not self._warn_active:
                return
            self._warn_active = False
            self._warn_timer = None
        self._stop_resume_watcher()
        log.info("warning elapsed with no input — powering off")
        self._do_off()

    def _do_off(self):
        # Final guard: don't power off if playback resumed during the warning.
        if self._media_playing():
            log.info("media resumed during warning — aborting power-off")
            return
        try:
            self.publish_off()
            log.info("idle auto-off: fired Media Center Off scene")
        except Exception as e:
            log.exception("idle auto-off: failed to fire off scene: %s", e)
