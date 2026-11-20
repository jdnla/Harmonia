"""Soft 'attention' chime played on the host's audio output (the AVR).

Triggered from Home Assistant (a dashboard button) over MQTT to gently get
someone's attention in the media room. Plays a gentle notification sound a few
times over ~4s, then stops on its own. Re-triggerable: a new press RESTARTS the
sequence rather than stacking overlapping copies.

This module is just the player — the "turn the amp on if it's off" logic lives
in launcher_service (which owns the MQTT client), so the chime is heard even
from a cold/quiet room. Stdlib only; plays via `paplay` to the default sink
(which on this box is the HDMI output feeding the Denon). Never raises to
callers.

Two things matter for it to actually be *heard*:

* A cold AVR wake brings the HDMI sink back into PipeWire several seconds
  after the amp reports power=ON. A note played before that exits non-zero
  ("Connection failure"/"No such entity"). That must not abort the sequence —
  we keep trying the remaining repeats so a later note lands once the sink is
  back.
* A re-press must RESTART, so the note the previous press is still playing is
  terminated, not left running underneath the new one.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time

log = logging.getLogger("chime")

# If the configured player binary isn't installed, try the other common one.
# Both play the same files. The sink/volume flags are paplay-only (pw-play's
# --volume is 0..1 and its sink flag is --target), so they're not translated.
_FALLBACK_PLAYERS = {
    "paplay": ("pw-play",),
    "pw-play": ("paplay",),
}


class ChimePlayer:
    def __init__(self, sound: str, repeat: int = 3, gap_ms: int = 400,
                 sink: str = "", player: str = "paplay", volume: int = 0):
        self.sound = sound
        self.repeat = max(1, repeat)
        self.gap = max(0.0, gap_ms / 1000.0)
        self.sink = sink
        self.player = player
        # paplay stream volume, 0..65536 (65536 = 100%). 0 => don't pass --volume
        # (play at full/native level). Lower it to make the chime softer. This is
        # the playback stream's own volume — it does NOT change the AVR/system
        # volume, so the chime is quieter relative to everything else.
        self.volume = max(0, min(65536, int(volume)))
        self._gen = 0                       # bumped each play() to supersede
        self._lock = threading.Lock()
        self._proc = None                   # the note currently playing
        self._resolved = None               # [player, flags..., sound], cached
        self._warned = False                # log a missing player only once

    def play(self, extra: int = 0):
        """(Re)start the chime. A new call supersedes any in-flight sequence so
        repeated button presses restart it instead of overlapping. `extra` adds
        repeats (used on a cold amp-wake: the amp's HDMI audio can take a few
        seconds to actually pass sound after it reports power=ON, so extra
        repeats make sure some of the chime is heard once it's ready)."""
        if not os.path.exists(self.sound):
            log.warning("chime sound not found: %s", self.sound)
            return
        reps = max(1, self.repeat + max(0, int(extra)))
        with self._lock:
            self._gen += 1
            gen = self._gen
            # Cut off the note the previous press is still playing, so a
            # re-press really restarts instead of stacking two copies.
            self._stop_locked()
        threading.Thread(target=self._run, args=(gen, reps), daemon=True).start()

    def _stop_locked(self):
        """Terminate the in-flight note. Caller must hold self._lock."""
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    def _command(self):
        """Resolve the argv for one note, or None if no player is installed."""
        if self._resolved is not None:
            return self._resolved
        wanted = os.path.basename(self.player)
        names = (self.player, *_FALLBACK_PLAYERS.get(wanted, ()))
        for name in names:
            path = shutil.which(name)
            if not path:
                continue
            if os.path.basename(name) != wanted:
                log.warning("chime player %r not found; using %r",
                            self.player, name)
            cmd = [path]
            # Sink/volume are paplay's own flags; pw-play's --volume is 0..1 and
            # its sink flag is --target, so they aren't translated.
            if os.path.basename(name) == "paplay":
                if self.sink:
                    cmd += ["-d", self.sink]
                if self.volume:
                    cmd += ["--volume", str(self.volume)]
            cmd.append(self.sound)
            self._resolved = cmd
            return cmd
        if not self._warned:
            self._warned = True
            log.error("chime: no player found for %r — install pulseaudio-utils "
                      "(paplay) or pipewire-bin (pw-play)", self.player)
        return None

    @staticmethod
    def _wait(proc):
        """Wait for one note. Returns (returncode, stderr text); kills a player
        that hangs so a wedged process can't block the whole sequence."""
        try:
            _, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            return -1, "timed out"
        return proc.returncode, (err or b"").decode("utf-8", "ignore")

    def _gap(self, gen: int) -> bool:
        """Interruptible gap. False => a newer press took over."""
        end = time.monotonic() + self.gap
        while time.monotonic() < end:
            if gen != self._gen:
                return False
            time.sleep(0.05)
        return True

    def _run(self, gen: int, reps: int):
        """Thread body: never let a note-level surprise kill the thread with a
        traceback (the chime is cosmetic and must not spam the journal)."""
        try:
            self._sequence(gen, reps)
        except Exception as e:
            log.exception("chime sequence failed: %s", e)

    def _sequence(self, gen: int, reps: int):
        cmd = self._command()
        if cmd is None:
            return
        ok = 0
        failed_once = False
        for i in range(reps):
            with self._lock:
                if gen != self._gen:        # a newer press took over
                    return
                try:
                    proc = subprocess.Popen(
                        cmd, stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE)
                except OSError as e:
                    log.warning("chime play failed (%s): %s", cmd[0], e)
                    return
                self._proc = proc
            rc, err = self._wait(proc)
            with self._lock:
                if self._proc is proc:
                    self._proc = None
                superseded = gen != self._gen
            if superseded:
                return
            if rc == 0:
                ok += 1
            elif not failed_once:
                failed_once = True
                # Very common on a cold wake: PipeWire hasn't got the HDMI sink
                # back yet, so the first note fails. Keep going — a later note
                # lands once the sink is available (this used to abort here,
                # which made the whole chime silent).
                log.warning("chime play failed (%s rc=%s): %s", cmd[0], rc,
                            (err.strip().splitlines()[0] if err.strip()
                             else "no stderr"))
            if i < reps - 1 and not self._gap(gen):
                return
        if ok:
            log.info("chime played (%d/%dx %s)", ok, reps,
                     os.path.basename(self.sound))
        else:
            log.warning("chime NOT audible: all %d attempts to play %s failed",
                        reps, os.path.basename(self.sound))
