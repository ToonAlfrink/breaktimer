"""Tests for the brightness-pause switch.

The pause exists so the user can reclaim manual control of their screens
for a while (a call, a movie). A regression here either keeps dimming a
screen the user asked us to leave alone, or never resumes — so both sides
of the contract are pinned.

Run: python3 -m unittest -q
"""
import os
import tempfile
import time
import unittest
from unittest import mock

import brightness_control
import status


class BrightnessPauseTest(unittest.TestCase):
    """Runs against a fresh XDG_RUNTIME_DIR with display writes mocked out."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self._tmp.name})
        self._env.start()
        self._apply = mock.patch.object(brightness_control, "_apply_to_all_displays")
        self.apply_mock = self._apply.start()

    def tearDown(self):
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


if __name__ == "__main__":
    unittest.main()
