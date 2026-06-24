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
        status.Snapshot(remaining_seconds=42.0, is_active=True).publish()
        snap = status.Snapshot.read()
        self.assertEqual(snap.remaining_seconds, 42.0)
        self.assertTrue(snap.is_active)

    def test_write_is_atomic_leaves_no_temp_file(self):
        status.Snapshot().publish()
        self.assertEqual(os.listdir(self._tmp.name), ["breaktimer-status.json"])

    def test_missing_returns_none(self):
        self.assertIsNone(status.Snapshot.read())

    def test_stale_returns_none(self):
        status.Snapshot().publish()
        old = time.time() - 60
        os.utime(status.status_path(), (old, old))
        self.assertIsNone(status.Snapshot.read(max_age_seconds=5))

    def test_corrupt_returns_none(self):
        with open(status.status_path(), "w") as f:
            f.write("{truncated")
        self.assertIsNone(status.Snapshot.read())


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


class TestInWindow(unittest.TestCase):
    """status.in_window: canonical window-logic tests live here; blocking modules delegate to this."""

    def test_same_day_inside(self):
        self.assertTrue(status.in_window(9 * 60, 17 * 60, 12 * 60))

    def test_same_day_at_start(self):
        self.assertTrue(status.in_window(9 * 60, 17 * 60, 9 * 60))

    def test_same_day_at_end_exclusive(self):
        self.assertFalse(status.in_window(9 * 60, 17 * 60, 17 * 60))

    def test_wraparound_inside(self):
        self.assertTrue(status.in_window(22 * 60, 8 * 60, 23 * 60))

    def test_wraparound_early_morning(self):
        self.assertTrue(status.in_window(22 * 60, 8 * 60, 3 * 60))

    def test_wraparound_outside(self):
        self.assertFalse(status.in_window(22 * 60, 8 * 60, 12 * 60))

    def test_zero_length_never_active(self):
        self.assertFalse(status.in_window(9 * 60, 9 * 60, 9 * 60))


class TestFmtWindow(unittest.TestCase):
    def test_same_day(self):
        self.assertEqual(status.fmt_window(9 * 60, 17 * 60), "09:00-17:00")

    def test_wraparound(self):
        self.assertEqual(status.fmt_window(22 * 60, 8 * 60), "22:00-08:00")

    def test_midnight(self):
        self.assertEqual(status.fmt_window(0, 6 * 60), "00:00-06:00")

    def test_non_zero_minutes(self):
        self.assertEqual(status.fmt_window(9 * 60 + 30, 17 * 60 + 45), "09:30-17:45")


class TestParseScheduleFile(unittest.TestCase):
    """status.parse_schedule_file: canonical schedule-file parser used by blocklist and app_blocking."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, content):
        path = os.path.join(self._tmp.name, "schedule.txt")
        with open(path, "w") as f:
            f.write(content)
        return path

    def test_none_path_returns_empty(self):
        self.assertEqual(status.parse_schedule_file(None, now_min=600), [])

    def test_missing_file_returns_empty(self):
        self.assertEqual(status.parse_schedule_file("/nonexistent/schedule.txt", now_min=600), [])

    def test_empty_file_returns_empty(self):
        path = self._write("")
        self.assertEqual(status.parse_schedule_file(path, now_min=600), [])

    def test_active_window_returns_entry(self):
        path = self._write("# 09:00-17:00\nreddit.com\n")
        result = status.parse_schedule_file(path, now_min=12 * 60)
        self.assertEqual(len(result), 1)
        start, end, items, is_active = result[0]
        self.assertEqual(start, 9 * 60)
        self.assertEqual(end, 17 * 60)
        self.assertEqual(items, ["reddit.com"])
        self.assertTrue(is_active)

    def test_inactive_window_returns_entry_marked_inactive(self):
        path = self._write("# 09:00-17:00\nreddit.com\n")
        result = status.parse_schedule_file(path, now_min=20 * 60)
        self.assertEqual(len(result), 1)
        self.assertFalse(result[0][3])

    def test_items_lowercased(self):
        path = self._write("# 09:00-17:00\nReddit.COM\nSTEAM\n")
        result = status.parse_schedule_file(path, now_min=12 * 60)
        self.assertEqual(result[0][2], ["reddit.com", "steam"])

    def test_items_before_first_header_ignored(self):
        path = self._write("orphan\n# 09:00-17:00\nvalid\n")
        result = status.parse_schedule_file(path, now_min=12 * 60)
        self.assertEqual(len(result), 1)
        self.assertNotIn("orphan", result[0][2])

    def test_empty_window_omitted(self):
        path = self._write("# 09:00-17:00\n# 22:00-08:00\nnight\n")
        result = status.parse_schedule_file(path, now_min=12 * 60)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][1], 8 * 60)  # only 22:00-08:00

    def test_all_windows_returned_not_just_active(self):
        path = self._write("# 09:00-17:00\nwork\n\n# 22:00-08:00\nnight\n")
        result = status.parse_schedule_file(path, now_min=12 * 60)
        self.assertEqual(len(result), 2)
        self.assertTrue(result[0][3])   # 09:00-17:00 active at noon
        self.assertFalse(result[1][3])  # 22:00-08:00 inactive at noon

    def test_wraparound_window_active_at_night(self):
        path = self._write("# 22:00-08:00\nnight\n")
        result = status.parse_schedule_file(path, now_min=23 * 60)
        self.assertTrue(result[0][3])

    def test_wraparound_window_active_early_morning(self):
        path = self._write("# 22:00-08:00\nnight\n")
        result = status.parse_schedule_file(path, now_min=3 * 60)
        self.assertTrue(result[0][3])

    def test_deduplication_within_window(self):
        path = self._write("# 09:00-17:00\ndup\ndup\nDUP\n")
        result = status.parse_schedule_file(path, now_min=12 * 60)
        self.assertEqual(result[0][2], ["dup"])

    def test_non_window_comments_skipped(self):
        path = self._write("# 09:00-17:00\nreddit\n# just a note\nhn\n")
        result = status.parse_schedule_file(path, now_min=12 * 60)
        # insertion order preserved within a window (not sorted — that's active_schedule_items)
        self.assertEqual(result[0][2], ["reddit", "hn"])

    def test_blank_lines_skipped(self):
        path = self._write("# 09:00-17:00\n\nreddit\n\n")
        result = status.parse_schedule_file(path, now_min=12 * 60)
        self.assertEqual(result[0][2], ["reddit"])


class TestActiveScheduleItems(unittest.TestCase):
    """status.active_schedule_items: filters parse_schedule_file to active items."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, content):
        path = os.path.join(self._tmp.name, "schedule.txt")
        with open(path, "w") as f:
            f.write(content)
        return path

    def test_none_path_returns_empty(self):
        self.assertEqual(status.active_schedule_items(None, now_min=600), [])

    def test_active_window_returns_items(self):
        path = self._write("# 09:00-17:00\nzebra\napple\n")
        result = status.active_schedule_items(path, now_min=12 * 60)
        self.assertEqual(result, ["apple", "zebra"])  # sorted

    def test_inactive_window_returns_empty(self):
        path = self._write("# 09:00-17:00\nreddit\n")
        self.assertEqual(status.active_schedule_items(path, now_min=20 * 60), [])

    def test_deduplication_across_overlapping_active_windows(self):
        path = self._write("# 08:00-12:00\nsteam\n\n# 08:00-17:00\nsteam\ndiscord\n")
        result = status.active_schedule_items(path, now_min=10 * 60)
        self.assertEqual(result, ["discord", "steam"])

    def test_only_active_windows_contribute(self):
        path = self._write("# 09:00-17:00\nwork\n\n# 22:00-08:00\nnight\n")
        result = status.active_schedule_items(path, now_min=12 * 60)
        self.assertEqual(result, ["work"])

    def test_sorted_output(self):
        path = self._write("# 09:00-17:00\nzz\naa\nmm\n")
        result = status.active_schedule_items(path, now_min=12 * 60)
        self.assertEqual(result, ["aa", "mm", "zz"])


class TestReadItems(unittest.TestCase):
    """status.read_items() — the shared flat-file reader used by blocklist and app_blocking."""

    def _write(self, content):
        self._tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
        self._tmp.write(content)
        self._tmp.close()
        return self._tmp.name

    def tearDown(self):
        if hasattr(self, "_tmp"):
            try:
                os.unlink(self._tmp.name)
            except OSError:
                pass

    def test_none_path(self):
        self.assertEqual(status.read_items(None), [])

    def test_missing_file(self):
        self.assertEqual(status.read_items("/nonexistent/path.txt"), [])

    def test_empty_file(self):
        self.assertEqual(status.read_items(self._write("")), [])

    def test_basic_items(self):
        self.assertEqual(status.read_items(self._write("steam\ndiscord\nspotify\n")),
                         ["discord", "spotify", "steam"])

    def test_skips_comments(self):
        self.assertEqual(status.read_items(self._write("# comment\nsteam\n# another\n")),
                         ["steam"])

    def test_skips_blank_lines(self):
        self.assertEqual(status.read_items(self._write("\nsteam\n\ndiscord\n\n")),
                         ["discord", "steam"])

    def test_lowercased(self):
        self.assertEqual(status.read_items(self._write("Steam\nDISCORD\nSpotify\n")),
                         ["discord", "spotify", "steam"])

    def test_deduplication(self):
        self.assertEqual(status.read_items(self._write("steam\nsteam\nSteam\n")),
                         ["steam"])

    def test_sorted_output(self):
        self.assertEqual(status.read_items(self._write("zsh\nabc\nmiddle\n")),
                         ["abc", "middle", "zsh"])


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
