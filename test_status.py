"""Tests for the live status bridge (status.py)."""
import json
import os
import subprocess
import sys
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

    def test_lock_released_when_holder_exits(self):
        # Watchdog correctness: after a crash, the OS releases the lock so a
        # fresh process can restart. Verify this by acquiring in a subprocess,
        # letting it exit, then confirming we can acquire in this process.
        script = (
            "import os, sys, status\n"
            f"os.environ['XDG_RUNTIME_DIR'] = {self._tmp.name!r}\n"
            "lock = status.acquire_singleton_lock('restart-test')\n"
            "sys.exit(0 if lock else 1)\n"
        )
        p = subprocess.run([sys.executable, "-c", script], check=True)
        lock = status.acquire_singleton_lock("restart-test")
        self.assertIsNotNone(lock, "lock must be free after the holder process exits")
        lock.close()


class TestFormatTime(unittest.TestCase):
    def test_minutes_and_seconds(self):
        self.assertEqual(status.format_time(65), "1:05")

    def test_zero(self):
        self.assertEqual(status.format_time(0), "0:00")

    def test_negative_clamped(self):
        self.assertEqual(status.format_time(-3), "0:00")


class TestColorForFraction(unittest.TestCase):
    def test_endpoints_match_palette(self):
        lo = status.COLOR_STOPS[0][1:]
        hi = status.COLOR_STOPS[-1][1:]
        self.assertEqual(status.color_for_fraction(0.0), lo)
        self.assertEqual(status.color_for_fraction(1.0), hi)

    def test_midpoint_interpolates(self):
        r, g, b = status.color_for_fraction(0.5)
        self.assertIsInstance(r, int)
        self.assertIsInstance(g, int)
        self.assertIsInstance(b, int)

    def test_out_of_range_clamped(self):
        lo = status.COLOR_STOPS[0][1:]
        hi = status.COLOR_STOPS[-1][1:]
        self.assertEqual(status.color_for_fraction(1.5), hi)
        self.assertEqual(status.color_for_fraction(-0.5), lo)


class TestFormatHistoryLine(unittest.TestCase):
    def _line(self, totals):
        return status.format_history_line(totals)

    def test_empty_history_shows_today_only(self):
        self.assertIn("today", self._line({}))
        self.assertNotIn("avg", self._line({}))

    def test_today_only_no_avg_no_spark(self):
        line = self._line({status.today_str(): 3600})
        self.assertIn("today", line)
        self.assertNotIn("avg", line)

    def test_past_days_include_avg(self):
        line = self._line({"2026-01-01": 7200, status.today_str(): 3600})
        self.assertIn("avg", line)

    def test_flat_spark_uses_mid_character(self):
        past = {f"2026-01-{d:02d}": 3600 for d in range(1, 10)}
        self.assertIn("▅", self._line(past))

    def test_below_avg_shows_negative_delta(self):
        past = {f"2026-01-{d:02d}": 14400 for d in range(1, 8)}
        self.assertIn("-", self._line({**past, status.today_str(): 1800}))

    def test_above_avg_shows_plus_delta(self):
        past = {f"2026-01-{d:02d}": 3600 for d in range(1, 8)}
        self.assertIn("+", self._line({**past, status.today_str(): 14400}))

    def test_monthly_sparkline_uses_month_totals(self):
        totals = {"2026-01-01": 36 * 3600, "2026-02-01": 72 * 3600, status.today_str(): 0}
        line = self._line(totals)
        spark_chars = set("▁▂▃▄▅▆▇█")
        found = [c for c in line if c in spark_chars]
        self.assertEqual(len(found), 2)
        self.assertNotEqual(found[0], found[1])

    def test_current_month_excluded_from_sparkline(self):
        today = status.today_str()
        day1 = f"{today[:7]}-01"
        line = self._line({day1: 7200, today: 3600})
        self.assertFalse(any(c in line for c in "▁▂▃▄▅▆▇█"))


class TestNoCommandChannel(unittest.TestCase):
    """Pin the invariant: status.py has no IPC surface for time extension.

    The command channel (command_path / write_command / read_command) was
    deleted in commit 70fe335 along with the rest of the extend pathway.
    These tests catch any attempt to re-introduce it.
    """

    def test_no_command_path(self):
        self.assertFalse(hasattr(status, "command_path"),
                         "command channel was removed — command_path must not exist")

    def test_no_write_command(self):
        self.assertFalse(hasattr(status, "write_command"),
                         "command channel was removed — write_command must not exist")

    def test_no_read_command(self):
        self.assertFalse(hasattr(status, "read_command"),
                         "command channel was removed — read_command must not exist")


if __name__ == "__main__":
    unittest.main()
