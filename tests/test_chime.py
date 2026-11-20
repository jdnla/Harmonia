"""Tests for the attention chime player (src/chime.py).

Stdlib only (unittest + a fake player script), so it runs anywhere without the
runtime deps installed:

    python3 -m unittest discover -s tests -v      (or: make test)

The fake player records every invocation to a JSONL file, can fail its first N
notes (simulating PipeWire not having the HDMI sink back yet after a cold AVR
wake) and reports SIGTERM, so the "keep trying after a failure" and "re-press
restarts the playing note" behaviours are covered without any audio hardware.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
import unittest

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from chime import ChimePlayer   # noqa: E402

# Stand-in for paplay/pw-play: logs start/end/fail/term, optionally fails, and
# reports SIGTERM so we can prove a re-press cut the playing note off.
FAKE_PLAYER = '''\
#!{python}
import json, os, signal, sys, time

LOG = os.environ["FAKE_PLAYER_LOG"]


def rec(kind):
    with open(LOG, "a") as f:
        f.write(json.dumps({{"kind": kind, "pid": os.getpid(),
                            "argv": sys.argv[1:], "t": time.time()}}) + "\\n")


def on_term(signum, frame):
    rec("term")
    raise SystemExit(143)


signal.signal(signal.SIGTERM, on_term)
rec("start")
time.sleep(float(os.environ.get("FAKE_PLAYER_DUR", "0.05")))
fail_first = int(os.environ.get("FAKE_PLAYER_FAIL_FIRST", "0"))
if fail_first:
    with open(LOG) as f:
        starts = sum(1 for ln in f
                     if ln.strip() and json.loads(ln)["kind"] == "start")
    if starts <= fail_first:
        sys.stderr.write("Connection failure: Connection refused\\n")
        rec("fail")
        raise SystemExit(1)
rec("end")
'''


class LogSink(logging.Handler):
    """Collects chime log records (the player logs from a worker thread)."""

    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())

    def text(self):
        return "\n".join(self.messages)


class ChimeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        self.log_path = os.path.join(d, "player.jsonl")
        open(self.log_path, "w").close()
        self.sound = os.path.join(d, "ding.oga")
        with open(self.sound, "wb") as f:
            f.write(b"OggS")
        self.bin = os.path.join(d, "bin")
        os.makedirs(self.bin)
        # Isolate PATH so a real paplay/pw-play on the host can't be picked up.
        self._path = os.environ.get("PATH", "")
        os.environ["PATH"] = self.bin
        os.environ["FAKE_PLAYER_LOG"] = self.log_path
        os.environ.pop("FAKE_PLAYER_FAIL_FIRST", None)
        os.environ.pop("FAKE_PLAYER_DUR", None)
        self.addCleanup(self._restore_env)
        self.players = []
        # Capture chime log output for the duration of each test (and keep it
        # out of the test output by not propagating to the root handler).
        self.logs = LogSink()
        logger = logging.getLogger("chime")
        logger.addHandler(self.logs)
        self.addCleanup(logger.removeHandler, self.logs)
        propagate = logger.propagate
        logger.propagate = False
        self.addCleanup(setattr, logger, "propagate", propagate)

    def _restore_env(self):
        os.environ["PATH"] = self._path
        for key in ("FAKE_PLAYER_LOG", "FAKE_PLAYER_FAIL_FIRST",
                    "FAKE_PLAYER_DUR"):
            os.environ.pop(key, None)
        # Don't leave a fake player (or its sequence) running past the test.
        for chime in self.players:
            with chime._lock:
                chime._gen += 1
                chime._stop_locked()

    # --- helpers ---------------------------------------------------------
    def make_player(self, name="paplay"):
        path = os.path.join(self.bin, name)
        with open(path, "w") as f:
            f.write(FAKE_PLAYER.format(python=sys.executable))
        os.chmod(path, 0o755)
        return path

    def chime(self, **kw):
        kw.setdefault("sound", self.sound)
        kw.setdefault("player", "paplay")
        kw.setdefault("gap_ms", 0)
        ch = ChimePlayer(**kw)
        self.players.append(ch)
        return ch

    def events(self):
        out = []
        with open(self.log_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def kinds(self):
        return [e["kind"] for e in self.events()]

    def wait_for(self, pred, timeout=5.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            ev = self.events()
            if pred(ev):
                return ev
            time.sleep(0.02)
        self.fail(f"timed out; events={self.events()}")

    def wait_kinds(self, kind, count, timeout=5.0):
        return self.wait_for(
            lambda ev: sum(1 for e in ev if e["kind"] == kind) >= count, timeout)

    def wait_log(self, needle, timeout=5.0):
        """Wait for a chime log line — the player logs its summary *after* the
        fake has written its final event, so waiting on the log avoids a race."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if needle in self.logs.text():
                return
            time.sleep(0.02)
        self.fail(f"{needle!r} not logged; logs={self.logs.text()!r}")

    # --- tests -----------------------------------------------------------
    def test_plays_repeat_times_with_paplay_flags(self):
        self.make_player("paplay")
        self.chime(repeat=3, sink="hdmi-stereo", volume=1234).play()
        ev = self.wait_kinds("end", 3)
        starts = [e for e in ev if e["kind"] == "start"]
        self.assertEqual(len(starts), 3)
        for s in starts:
            self.assertEqual(
                s["argv"], ["-d", "hdmi-stereo", "--volume", "1234", self.sound])

    def test_keeps_going_after_a_failed_note(self):
        """The first notes of a cold wake fail (no HDMI sink yet) — the rest of
        the sequence must still be attempted, and the win logged as played."""
        self.make_player("paplay")
        os.environ["FAKE_PLAYER_FAIL_FIRST"] = "2"
        self.chime(repeat=3).play()
        ev = self.wait_kinds("end", 1)
        kinds = [e["kind"] for e in ev]
        self.assertEqual(kinds.count("start"), 3)
        self.assertEqual(kinds.count("fail"), 2)
        self.wait_log("chime played (1/3x")

    def test_all_notes_failing_is_reported_as_not_audible(self):
        self.make_player("paplay")
        os.environ["FAKE_PLAYER_FAIL_FIRST"] = "99"
        self.chime(repeat=2).play()
        ev = self.wait_kinds("fail", 2)
        self.assertEqual([e["kind"] for e in ev],
                         ["start", "fail", "start", "fail"])
        self.wait_log("chime NOT audible")
        self.assertIn("Connection failure", self.logs.text())

    def test_repress_terminates_the_playing_note(self):
        """A re-press restarts: the note still playing is killed, not stacked."""
        self.make_player("paplay")
        os.environ["FAKE_PLAYER_DUR"] = "2"
        ch = self.chime(repeat=1)
        ch.play()
        ev = self.wait_kinds("start", 1)
        first_pid = ev[0]["pid"]
        ch.play()
        ev = self.wait_for(
            lambda ev: len([e for e in ev if e["kind"] == "start"]) == 2
            and any(e["kind"] == "term" and e["pid"] == first_pid for e in ev))
        by_pid = {}
        for e in ev:
            by_pid.setdefault(e["pid"], []).append(e["kind"])
        # First player was terminated mid-note (never finished on its own).
        self.assertIn("term", by_pid[first_pid])
        self.assertNotIn("end", by_pid[first_pid])
        self.assertEqual(self.kinds().count("start"), 2)

    def test_repress_during_the_gap_restarts_the_sequence(self):
        self.make_player("paplay")
        os.environ["FAKE_PLAYER_DUR"] = "0.02"
        ch = self.chime(repeat=3, gap_ms=300)
        ch.play()
        self.wait_kinds("end", 1)
        ch.play()   # mid-gap: the old sequence must abandon its remaining notes
        self.wait_kinds("end", 4)
        # 1 note from the first press + 3 from the second (old one abandoned).
        self.assertEqual(self.kinds().count("start"), 4)

    def test_extra_repeats_are_appended(self):
        self.make_player("paplay")
        self.chime(repeat=1).play(extra=2)
        self.wait_kinds("end", 3)
        self.assertEqual(self.kinds().count("start"), 3)

    def test_falls_back_to_pw_play_without_paplay_flags(self):
        self.make_player("pw-play")
        self.chime(repeat=1, sink="hdmi-stereo", volume=1234).play()
        ev = self.wait_kinds("end", 1)
        self.assertEqual(ev[0]["argv"], [self.sound])

    def test_pw_play_configured_does_not_get_paplay_flags(self):
        self.make_player("pw-play")
        self.chime(repeat=1, player="pw-play", sink="hdmi-stereo",
                   volume=1234).play()
        ev = self.wait_kinds("end", 1)
        self.assertEqual(ev[0]["argv"], [self.sound])

    def test_missing_sound_plays_nothing(self):
        self.make_player("paplay")
        self.chime(sound=os.path.join(self.tmp.name, "nope.oga")).play()
        time.sleep(0.2)
        self.assertEqual(self.events(), [])
        self.assertIn("chime sound not found", self.logs.text())

    def test_missing_player_does_not_raise(self):
        self.chime(repeat=1, player="paplay").play()
        time.sleep(0.2)
        self.assertEqual(self.events(), [])
        self.assertIn("no player found", self.logs.text())


if __name__ == "__main__":
    unittest.main()
