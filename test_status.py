"""Tests for the live status bridge (status.py)."""
import json
import os
import tempfile
import time
import unittest
from unittest import mock

import status


class InTempRuntimeDir(unittest.TestCase):
    """Point XDG_RUNTIME_DIR at a fresh temp dir for each test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self._tmp.name})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()


class TestStatusFile(InTempRuntimeDir):
    def test_round_trip(self):
        status.write_status({"remaining_seconds": 42.0, "is_active": True})
        self.assertEqual(
            status.read_status(),
            {"remaining_seconds": 42.0, "is_active": True},
        )

    def test_write_is_atomic_leaves_no_temp_file(self):
        status.write_status({"x": 1})
        self.assertEqual(os.listdir(self._tmp.name), ["breaktimer-status.json"])

    def test_missing_returns_none(self):
        self.assertIsNone(status.read_status())

    def test_stale_returns_none(self):
        status.write_status({"x": 1})
        old = time.time() - 60
        os.utime(status.status_path(), (old, old))
        self.assertIsNone(status.read_status(max_age_seconds=5))

    def test_corrupt_returns_none(self):
        with open(status.status_path(), "w") as f:
            f.write("{truncated")
        self.assertIsNone(status.read_status())


class TestSingletonLock(InTempRuntimeDir):
    def test_second_acquire_fails_until_released(self):
        first = status.acquire_singleton_lock("test")
        self.assertIsNotNone(first)
        self.assertIsNone(status.acquire_singleton_lock("test"))
        first.close()
        second = status.acquire_singleton_lock("test")
        self.assertIsNotNone(second)
        second.close()

    def test_different_names_do_not_conflict(self):
        a = status.acquire_singleton_lock("core")
        b = status.acquire_singleton_lock("ambient")
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        a.close()
        b.close()


class TestColorForFraction(unittest.TestCase):
    def test_full_is_blue(self):
        self.assertEqual(status.color_for_fraction(1.0), (0, 0, 255))

    def test_half_is_yellow(self):
        self.assertEqual(status.color_for_fraction(0.5), (255, 255, 0))

    def test_empty_is_black(self):
        self.assertEqual(status.color_for_fraction(0.0), (0, 0, 0))

    def test_out_of_range_clamped(self):
        self.assertEqual(status.color_for_fraction(1.5), (0, 0, 255))
        self.assertEqual(status.color_for_fraction(-0.5), (0, 0, 0))


if __name__ == "__main__":
    unittest.main()
