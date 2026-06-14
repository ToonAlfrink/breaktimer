"""Tests for brightness control: pause switch and circadian curve.

The pause lets the user reclaim manual screen control (a call, a movie).
The circadian curve multiplies the depletion fraction so a full bar late at
night still dims the screen rather than blasting cold-bright light.

Run: python3 -m unittest -q
"""
import logging
import math
import os
import tempfile
import time
import unittest
from unittest import mock

import brightness_control
import status

logging.getLogger("breaktimer").addHandler(logging.NullHandler())


def _noon_mock():
    """Return a mock datetime.datetime.now() anchored at 13:00 (circadian peak)."""
    dt = mock.Mock()
    dt.hour = 13
    dt.minute = 0
    return dt


class BrightnessPauseTest(unittest.TestCase):
    """Runs against a fresh XDG_RUNTIME_DIR with display writes and clock mocked out."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self._tmp.name})
        self._env.start()
        self._apply = mock.patch.object(brightness_control, "_apply_to_all_displays")
        self.apply_mock = self._apply.start()
        # Pin time to noon so circadian_fraction == 1.0 and depletion passes through.
        self._dt = mock.patch("brightness_control.datetime")
        dt_mock = self._dt.start()
        dt_mock.datetime.now.return_value = _noon_mock()

    def tearDown(self):
        self._dt.stop()
        self._apply.stop()
        self._env.stop()
        self._tmp.cleanup()

    def test_pause_parks_displays_and_blocks_fraction_updates(self):
        brightness_control.pause(7200, level=60)
        self.apply_mock.assert_called_once_with(60)

        self.apply_mock.reset_mock()
        brightness_control.set_brightness_by_fraction(0.5)
        self.apply_mock.assert_not_called()

    def test_pause_defaults_to_full_brightness(self):
        brightness_control.pause(7200)
        self.apply_mock.assert_called_once_with(100)

    def test_expired_pause_resumes_dimming(self):
        with open(status.brightness_pause_path(), "w") as f:
            f.write(str(time.time() - 1))
        brightness_control.set_brightness_by_fraction(0.5)
        self.apply_mock.assert_called_once_with(50)

    def test_unpause_resumes_dimming(self):
        brightness_control.pause(7200)
        brightness_control.unpause()
        self.assertFalse(brightness_control.is_paused())

        self.apply_mock.reset_mock()
        brightness_control.set_brightness_by_fraction(1.0)
        self.apply_mock.assert_called_once_with(100)

    def test_unpause_without_pause_is_harmless(self):
        brightness_control.unpause()
        self.assertFalse(brightness_control.is_paused())

    def test_corrupt_pause_file_means_not_paused(self):
        with open(status.brightness_pause_path(), "w") as f:
            f.write("garbage")
        self.assertFalse(brightness_control.is_paused())


class BrightnessLogTest(unittest.TestCase):
    """Every real brightness override is logged with its cause; steady state is quiet."""

    def setUp(self):
        self._apply = mock.patch.object(brightness_control, "_apply_to_all_displays")
        self._apply.start()
        self._dt = mock.patch("brightness_control.datetime")
        self._dt.start().datetime.now.return_value = _noon_mock()  # circadian == 1.0
        self._paused = mock.patch.object(brightness_control, "is_paused", return_value=False)
        self._paused.start()
        brightness_control._last_applied = None

    def tearDown(self):
        self._paused.stop()
        self._dt.stop()
        self._apply.stop()

    def test_change_logs_level_and_cause(self):
        with self.assertLogs("breaktimer.brightness", level="INFO") as cm:
            brightness_control.set_brightness_by_fraction(0.5)
        self.assertTrue(any("50%" in m and "bar 50%" in m for m in cm.output))

    def test_unchanged_level_does_not_relog(self):
        brightness_control.set_brightness_by_fraction(0.5)
        with self.assertNoLogs("breaktimer.brightness", level="INFO"):
            brightness_control.set_brightness_by_fraction(0.5)


class CircadianFractionTest(unittest.TestCase):
    """Pin the shape of the circadian curve."""

    def test_peak_at_1pm(self):
        self.assertAlmostEqual(brightness_control.circadian_fraction(13), 1.0)

    def test_trough_at_1am(self):
        self.assertAlmostEqual(brightness_control.circadian_fraction(1), brightness_control._CIRCADIAN_FLOOR)

    def test_floor_is_positive(self):
        self.assertGreater(brightness_control._CIRCADIAN_FLOOR, 0)

    def test_symmetry_around_peak(self):
        # Equal distance before and after the peak → equal brightness.
        self.assertAlmostEqual(
            brightness_control.circadian_fraction(10),
            brightness_control.circadian_fraction(16),
        )

    def test_midday_brighter_than_midnight(self):
        self.assertGreater(
            brightness_control.circadian_fraction(12),
            brightness_control.circadian_fraction(0),
        )

    def test_always_in_range(self):
        for h in range(24):
            v = brightness_control.circadian_fraction(h)
            self.assertGreaterEqual(v, brightness_control._CIRCADIAN_FLOOR - 1e-9)
            self.assertLessEqual(v, 1.0 + 1e-9)

    def test_full_bar_at_night_is_dimmed(self):
        # A full bar at 1 am must land below noon brightness at the same bar level.
        night = brightness_control.circadian_fraction(1) * 1.0
        noon = brightness_control.circadian_fraction(13) * 1.0
        self.assertLess(night, noon)


if __name__ == "__main__":
    unittest.main()
