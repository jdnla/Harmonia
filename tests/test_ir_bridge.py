"""Unit tests for ir_bridge pure logic (no hardware / MQTT needed)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import ir_bridge as ir  # noqa: E402


class TestParseTarget(unittest.TestCase):
    def test_json(self):
        self.assertEqual(
            ir.IrBridge._parse_target('{"remote":"Vizio-TV","key":"KEY_OK","repeats":3}'),
            ("Vizio-TV", "KEY_OK", 3))

    def test_json_default_repeats(self):
        self.assertEqual(
            ir.IrBridge._parse_target('{"remote":"Denon","key":"KEY_POWER"}'),
            ("Denon", "KEY_POWER", 1))

    def test_slash(self):
        self.assertEqual(ir.IrBridge._parse_target("Vizio-TV/KEY_OK"),
                         ("Vizio-TV", "KEY_OK", 1))

    def test_slash_with_repeat(self):
        self.assertEqual(ir.IrBridge._parse_target("Vizio-TV/KEY_UP x4"),
                         ("Vizio-TV", "KEY_UP", 4))

    def test_space_separator(self):
        self.assertEqual(ir.IrBridge._parse_target("Denon KEY_POWER"),
                         ("Denon", "KEY_POWER", 1))

    def test_empty(self):
        self.assertEqual(ir.IrBridge._parse_target(""), ("", "", 1))

    def test_repeats_min_one(self):
        self.assertEqual(ir.IrBridge._parse_target('{"remote":"r","key":"k","repeats":0}'),
                         ("r", "k", 1))


class TestKeylineParse(unittest.TestCase):
    def test_irsend_list_line(self):
        m = ir._KEYLINE.match("000000000000a25d KEY_UP")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "KEY_UP")

    def test_header_line_ignored(self):
        self.assertIsNone(ir._KEYLINE.match("irsend: Vizio-TV"))


class TestMode2Fold(unittest.TestCase):
    def _edges(self, text):
        out = []
        for ln in text.strip().splitlines():
            e = ir.parse_mode2_line(ln)
            if e:
                out.append(e)
        return out

    def test_nec_burst(self):
        # Leading noise space, then a clean NEC-ish burst, then the end gap.
        text = """
        space 500000
        pulse 9000
        space 4500
        pulse 560
        space 560
        pulse 560
        space 1690
        pulse 560
        space 1690
        pulse 560
        space 99000
        """
        durs = ir.fold_mode2(self._edges(text), gap_end=12000, min_edges=8)
        # Starts on the first pulse, drops the trailing 99000 end-gap, ends on a pulse.
        self.assertEqual(durs[0], 9000)
        self.assertEqual(durs[-1], 560)
        self.assertEqual(len(durs) % 2, 1)
        self.assertNotIn(99000, durs)

    def test_too_short_returns_empty(self):
        text = "pulse 9000\nspace 4500\npulse 560\nspace 99000"
        self.assertEqual(ir.fold_mode2(self._edges(text), min_edges=8), [])

    def test_ignores_until_first_pulse(self):
        text = "space 123\nspace 456\npulse 9000\nspace 4500\npulse 560\n" \
               "space 560\npulse 560\nspace 560\npulse 560\nspace 560\npulse 560\nspace 50000"
        durs = ir.fold_mode2(self._edges(text), gap_end=12000, min_edges=8)
        self.assertEqual(durs[0], 9000)


class TestSanitize(unittest.TestCase):
    def test_remote(self):
        self.assertEqual(ir.sanitize_remote("Denon AVR!"), "Denon-AVR")
        self.assertEqual(ir.sanitize_remote("  "), "learned")

    def test_key(self):
        self.assertEqual(ir.sanitize_key("power on"), "POWER_ON")
        self.assertEqual(ir.sanitize_key("KEY_power"), "KEY_POWER")


class TestLearnedStore(unittest.TestCase):
    def test_add_and_conf_regen(self):
        with tempfile.TemporaryDirectory() as data, tempfile.TemporaryDirectory() as conf:
            store = ir.LearnedStore(data, conf)
            rname, kname = store.add("Denon AVR", "power on", [9000, 4500, 560, 560,
                                                               560, 1690, 560, 560, 560])
            self.assertEqual((rname, kname), ("Denon-AVR", "POWER_ON"))
            # JSON source of truth exists.
            self.assertTrue(os.path.exists(os.path.join(data, "Denon-AVR.json")))
            # Regenerated conf is well-formed.
            text = open(os.path.join(conf, "learned-Denon-AVR.lircd.conf")).read()
            self.assertIn("name  Denon-AVR", text)
            self.assertIn("flags RAW_CODES", text)
            self.assertIn("name POWER_ON", text)
            self.assertIn("9000", text)

    def test_add_second_key_keeps_first(self):
        with tempfile.TemporaryDirectory() as data, tempfile.TemporaryDirectory() as conf:
            store = ir.LearnedStore(data, conf)
            durs = [9000, 4500, 560, 560, 560, 1690, 560, 560, 560]
            store.add("TV", "KEY_1", durs)
            store.add("TV", "KEY_2", durs)
            text = open(os.path.join(conf, "learned-TV.lircd.conf")).read()
            self.assertIn("name KEY_1", text)
            self.assertIn("name KEY_2", text)


if __name__ == "__main__":
    unittest.main()
