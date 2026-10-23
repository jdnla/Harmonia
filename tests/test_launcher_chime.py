"""Tests for the launcher service's attention-chime decision logic.

Covers the "is the amp cold or warm, and is it on the PC input?" path in
LauncherService._play_chime, which is what makes the chime audible when the
user powers the system on themselves (amp already ON, but on another source,
and the retained MQTT power state still saying OFF).

paho-mqtt isn't needed: it's stubbed before the import, and the service object
is built with __new__ so no real MQTT client / KDE session is touched.

    python3 -m unittest discover -s tests -v      (or: make test)
"""
from __future__ import annotations

import logging
import os
import sys
import time
import types
import unittest

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


def _stub_paho():
    """launcher_service imports paho at module level; only the client object is
    referenced here, never constructed, so an empty stub is enough."""
    if "paho.mqtt.client" in sys.modules:
        return
    paho = types.ModuleType("paho")
    mqtt = types.ModuleType("paho.mqtt")
    client = types.ModuleType("paho.mqtt.client")
    client.Client = object
    client.MQTTMessage = object
    mqtt.client = client
    paho.mqtt = mqtt
    sys.modules["paho"] = paho
    sys.modules["paho.mqtt"] = mqtt
    sys.modules["paho.mqtt.client"] = client


_stub_paho()
import launcher_service as ls   # noqa: E402

POWER_TOPIC = "avr-tv-bridge/denon/power/set"
INPUT_TOPIC = "avr-tv-bridge/denon/input/set"


def _msg(topic, payload):
    data = payload if isinstance(payload, bytes) else str(payload).encode()
    return types.SimpleNamespace(topic=topic, payload=data)


class FakeChime:
    def __init__(self):
        self.calls = []
        self.exc = None

    def play(self, extra=0):
        if self.exc is not None:
            raise self.exc
        self.calls.append(extra)


class FakeIdle:
    def __init__(self):
        self.defers = []
        self.power_ons = 0

    def defer(self, secs):
        self.defers.append(secs)

    def on_power_on(self):
        self.power_ons += 1


class FakeClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload=None, retain=False):
        self.published.append((topic, payload, retain))

    def topics(self):
        return [t for t, _, _ in self.published]


class FakeDenon:
    """Minimal stand-in for denon.DenonClient.

    drop_sets models an AVR ignoring SI while it boots; turn_on_after models the
    amp reporting power=ON a couple of polls after PWON."""

    def __init__(self, power="ON", input_="MPLAY", drop_sets=0,
                 turn_on_after=None, unreachable=False):
        self.power = power
        self.input = input_
        self.drop_sets = drop_sets
        self.turn_on_after = turn_on_after
        self.unreachable = unreachable
        self.get_calls = 0
        self.set_calls = []

    def get_state(self):
        self.get_calls += 1
        if self.unreachable:
            raise OSError("no route to host")
        if (self.turn_on_after is not None
                and self.get_calls >= self.turn_on_after):
            self.power = "ON"
        return {"power": self.power, "input": self.input}

    def set_input(self, want):
        self.set_calls.append(want)
        if self.drop_sets > 0:
            self.drop_sets -= 1      # command swallowed (still booting)
            return
        self.input = want


class ChimeDecisionTest(unittest.TestCase):
    def setUp(self):
        # launcher_service calls logging.basicConfig() on import; keep the
        # expected chime chatter out of the test output.
        self._log = logging.getLogger("launcher_service")
        level = self._log.level
        self.addCleanup(self._log.setLevel, level)
        self._log.setLevel(logging.CRITICAL)

    def make(self, denon=None, mqtt_power=None, amp_on=True, amp_input="MPLAY",
             input_sync=True, amp_wait=1.0, amp_settle=0.0, cold_extra=2,
             idle_defer=180.0, with_denon=True):
        svc = ls.LauncherService.__new__(ls.LauncherService)
        svc.chime = self.chime = FakeChime()
        svc.idle_off = self.idle = FakeIdle()
        svc.client = self.client = FakeClient()
        svc.camera_popup = None
        svc.cam_trigger_topic = "avr-tv-bridge/campopup/trigger"
        svc._denon_id = "denon"
        svc._denon_power = mqtt_power
        svc._chime_wake_until = 0.0
        svc._chime_amp_on = amp_on
        svc._chime_amp_input = amp_input
        svc._chime_input_sync = input_sync
        svc._chime_amp_wait = amp_wait
        svc._chime_amp_settle = amp_settle
        svc._chime_cold_extra = cold_extra
        svc._chime_idle_defer = idle_defer
        svc._chime_denon = denon if with_denon else None
        self.denon = denon
        return svc

    # --- cold wake -------------------------------------------------------
    def test_cold_wake_powers_on_routes_input_then_plays(self):
        denon = FakeDenon(power="OFF", input_="CD", turn_on_after=3,
                          drop_sets=1)
        svc = self.make(denon=denon, mqtt_power="OFF")
        svc._play_chime()
        # The SI sent while the amp was still in standby was dropped; the
        # post-wake verify retried it, so the PC input really is selected.
        self.assertEqual(denon.input, "MPLAY")
        self.assertEqual(self.client.topics(), [POWER_TOPIC])
        self.assertEqual(self.client.published[0][1], "ON")
        self.assertEqual(self.chime.calls, [2])     # cold extra repeats
        self.assertGreater(svc._chime_wake_until, time.monotonic())
        self.assertEqual(self.idle.defers, [180.0])

    def test_cold_wake_still_routes_input_when_sync_is_off(self):
        denon = FakeDenon(power="OFF", input_="CD", turn_on_after=2)
        svc = self.make(denon=denon, mqtt_power="OFF", input_sync=False)
        svc._play_chime()
        self.assertEqual(denon.input, "MPLAY")
        self.assertIn(POWER_TOPIC, self.client.topics())
        self.assertEqual(self.chime.calls, [2])

    # --- warm amp --------------------------------------------------------
    def test_stale_mqtt_state_does_not_force_a_cold_wake(self):
        """You powered the system on yourself; MQTT still says OFF but the amp
        is on. No re-power, no long wait — just route the input and chime."""
        denon = FakeDenon(power="ON", input_="CD")
        svc = self.make(denon=denon, mqtt_power="OFF")
        started = time.monotonic()
        svc._play_chime()
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(self.client.published, [])     # no PWON re-send
        self.assertEqual(denon.set_calls, ["MPLAY"])    # direct telnet switch
        self.assertEqual(denon.input, "MPLAY")
        self.assertEqual(self.chime.calls, [0])     # NOT the cold extra repeats
        self.assertEqual(svc._chime_wake_until, 0.0)

    def test_already_on_the_pc_input_plays_immediately(self):
        denon = FakeDenon(power="ON", input_="MPLAY")
        svc = self.make(denon=denon, mqtt_power="ON")
        started = time.monotonic()
        svc._play_chime()
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(self.client.published, [])
        self.assertEqual(denon.set_calls, [])
        self.assertEqual(self.chime.calls, [0])

    def test_input_sync_off_leaves_a_warm_amp_on_its_input(self):
        denon = FakeDenon(power="ON", input_="CD")
        svc = self.make(denon=denon, mqtt_power="ON", input_sync=False)
        svc._play_chime()
        self.assertEqual(self.client.published, [])
        self.assertEqual(denon.input, "CD")
        self.assertEqual(self.chime.calls, [0])

    def test_amp_wake_disabled_only_plays(self):
        denon = FakeDenon(power="OFF", input_="CD")
        svc = self.make(denon=denon, mqtt_power="OFF", amp_on=False)
        svc._play_chime()
        self.assertEqual(self.client.published, [])
        self.assertEqual(denon.set_calls, [])
        self.assertEqual(self.chime.calls, [0])
        self.assertEqual(svc._chime_wake_until, 0.0)

    # --- degraded paths --------------------------------------------------
    def test_without_a_direct_denon_client_uses_mqtt(self):
        svc = self.make(mqtt_power="OFF", amp_wait=0.0, with_denon=False)
        svc._play_chime()
        self.assertIn(POWER_TOPIC, self.client.topics())
        self.assertIn(INPUT_TOPIC, self.client.topics())
        self.assertEqual(self.chime.calls, [2])
        self.assertGreater(svc._chime_wake_until, time.monotonic())

    def test_unreachable_denon_falls_back_to_mqtt_and_still_plays(self):
        denon = FakeDenon(unreachable=True)
        svc = self.make(denon=denon, mqtt_power="OFF", amp_wait=0.6)
        started = time.monotonic()
        svc._play_chime()
        self.assertIn(POWER_TOPIC, self.client.topics())
        self.assertEqual(self.chime.calls, [2])
        # Bounded: the wake wait gives up, then the input retries give up.
        self.assertLess(time.monotonic() - started, 6.0)

    def test_a_playback_error_is_swallowed(self):
        denon = FakeDenon(power="ON", input_="MPLAY")
        svc = self.make(denon=denon, mqtt_power="ON")
        self.chime.exc = RuntimeError("boom")
        with self.assertLogs("launcher_service", level="ERROR"):
            svc._play_chime()       # logged, never raised into the MQTT thread

    # --- MQTT glue -------------------------------------------------------
    def test_chime_wake_marker_is_consumed_once(self):
        denon = FakeDenon(power="OFF", input_="MPLAY", turn_on_after=2)
        svc = self.make(denon=denon, mqtt_power="OFF")
        svc._play_chime()
        self.assertGreater(svc._chime_wake_until, 0.0)
        # The bridge publishes the OFF->ON caused by the chime wake...
        svc._denon_power = "OFF"
        svc._on_message(None, None,
                        _msg("avr-tv-bridge/denon/power/state", "ON"))
        self.assertEqual(self.idle.power_ons, 0)
        self.assertEqual(svc._chime_wake_until, 0.0)
        # ...and a real power-on afterwards still starts the short idle window.
        svc._denon_power = "OFF"
        svc._on_message(None, None,
                        _msg("avr-tv-bridge/denon/power/state", "ON"))
        self.assertEqual(self.idle.power_ons, 1)

    def test_real_power_on_starts_the_idle_window(self):
        svc = self.make(denon=FakeDenon(power="ON"), mqtt_power="OFF")
        svc._on_message(None, None,
                        _msg("avr-tv-bridge/denon/power/state", "ON"))
        self.assertEqual(self.idle.power_ons, 1)

    def test_chime_topic_triggers_the_chime(self):
        svc = self.make(denon=FakeDenon(power="ON"), mqtt_power="ON")
        ran = []
        svc._play_chime = lambda: ran.append(True)
        svc._on_message(None, None, _msg("avr-tv-bridge/chime/set", "PRESS"))
        for _ in range(100):
            if ran:
                break
            time.sleep(0.01)
        self.assertEqual(ran, [True])


if __name__ == "__main__":
    unittest.main()
