"""Tests for pointer-speed control.

The daemon silently rewrites COSMIC's input config as the bar depletes; this
pins that each real change leaves a why-it-acted trail (and that steady state
stays quiet).

Run: python3 -m unittest -q
"""
import logging
import unittest

import mouse_sensitivity_control as mouse

logging.getLogger("breaktimer").addHandler(logging.NullHandler())


class PointerSpeedLogTest(unittest.TestCase):
    def setUp(self):
        mouse._last_value = None

    def test_change_is_logged(self):
        with self.assertLogs("breaktimer.mouse", level="INFO") as cm:
            mouse.set_sensitivity_by_fraction(0.5)  # -> speed 0.0
        self.assertTrue(any("0.0" in m for m in cm.output))

    def test_unchanged_value_does_not_relog(self):
        mouse.set_sensitivity_by_fraction(0.5)
        with self.assertNoLogs("breaktimer.mouse", level="INFO"):
            mouse.set_sensitivity_by_fraction(0.5)

    def test_clamps_to_range(self):
        mouse.set_sensitivity(5.0)
        self.assertEqual(mouse._last_value, 1.0)


if __name__ == "__main__":
    unittest.main()
