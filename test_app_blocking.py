"""Tests for app_blocking.py — process/app blocking tier system."""
import os
import signal
import tempfile
import unittest
from unittest.mock import call, patch

import app_blocking
import status


def _write(path, content):
    with open(path, "w") as f:
        f.write(content)


class TestInWindow(unittest.TestCase):
    def test_same_day_inside(self):
        self.assertTrue(status.in_window(540, 1020, 600))   # 09:00–17:00, now 10:00

    def test_same_day_at_start(self):
        self.assertTrue(status.in_window(540, 1020, 540))   # exactly at start

    def test_same_day_at_end_exclusive(self):
        self.assertFalse(status.in_window(540, 1020, 1020)) # end is exclusive

    def test_same_day_outside(self):
        self.assertFalse(status.in_window(540, 1020, 1200)) # 20:00 not in 09:00–17:00

    def test_wraparound_inside_evening(self):
        self.assertTrue(status.in_window(1320, 480, 1400))  # 22:00–08:00, now 23:20

    def test_wraparound_inside_morning(self):
        self.assertTrue(status.in_window(1320, 480, 60))    # 22:00–08:00, now 01:00

    def test_wraparound_outside(self):
        self.assertFalse(status.in_window(1320, 480, 600))  # 22:00–08:00, now 10:00

    def test_zero_length_never_active(self):
        self.assertFalse(status.in_window(600, 600, 600))


class TestReadNames(unittest.TestCase):
    def test_none_path(self):
        self.assertEqual(app_blocking._read_names(None), [])

    def test_missing_file(self):
        self.assertEqual(app_blocking._read_names("/nonexistent/path.txt"), [])

    def test_empty_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("")
            path = f.name
        try:
            self.assertEqual(app_blocking._read_names(path), [])
        finally:
            os.unlink(path)

    def test_basic_names(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("steam\ndiscord\nspotify\n")
            path = f.name
        try:
            self.assertEqual(app_blocking._read_names(path), ["discord", "spotify", "steam"])
        finally:
            os.unlink(path)

    def test_skips_comments(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("# this is a comment\nsteam\n# another comment\n")
            path = f.name
        try:
            self.assertEqual(app_blocking._read_names(path), ["steam"])
        finally:
            os.unlink(path)

    def test_skips_blank_lines(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("\nsteam\n\ndiscord\n\n")
            path = f.name
        try:
            self.assertEqual(app_blocking._read_names(path), ["discord", "steam"])
        finally:
            os.unlink(path)

    def test_case_insensitive_lowercased(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("Steam\nDISCORD\nSpotify\n")
            path = f.name
        try:
            self.assertEqual(app_blocking._read_names(path), ["discord", "spotify", "steam"])
        finally:
            os.unlink(path)

    def test_deduplication(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("steam\nsteam\nSteam\n")
            path = f.name
        try:
            self.assertEqual(app_blocking._read_names(path), ["steam"])
        finally:
            os.unlink(path)

    def test_sorted_output(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("zsh\nabc\nmiddle\n")
            path = f.name
        try:
            self.assertEqual(app_blocking._read_names(path), ["abc", "middle", "zsh"])
        finally:
            os.unlink(path)


class TestReadNamesScheduled(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
        self.path = self.tmp.name

    def tearDown(self):
        os.unlink(self.path)

    def test_no_window_header_ignored(self):
        _write(self.path, "steam\ndiscord\n")
        # Names before first header are ignored.
        self.assertEqual(app_blocking._read_names_scheduled(self.path, now_min=600), [])

    def test_window_active(self):
        _write(self.path, "# 09:00-17:00\nsteam\ndiscord\n")
        result = app_blocking._read_names_scheduled(self.path, now_min=600)  # 10:00
        self.assertEqual(result, ["discord", "steam"])

    def test_window_inactive(self):
        _write(self.path, "# 09:00-17:00\nsteam\n")
        result = app_blocking._read_names_scheduled(self.path, now_min=1200)  # 20:00
        self.assertEqual(result, [])

    def test_wraparound_window_evening(self):
        _write(self.path, "# 22:00-08:00\nspotify\n")
        result = app_blocking._read_names_scheduled(self.path, now_min=1380)  # 23:00
        self.assertEqual(result, ["spotify"])

    def test_wraparound_window_morning(self):
        _write(self.path, "# 22:00-08:00\nspotify\n")
        result = app_blocking._read_names_scheduled(self.path, now_min=60)   # 01:00
        self.assertEqual(result, ["spotify"])

    def test_multiple_windows_only_active(self):
        _write(self.path, "# 09:00-12:00\nsteam\n\n# 18:00-20:00\ndiscord\n")
        # 10:00 — first window active, second not
        result = app_blocking._read_names_scheduled(self.path, now_min=600)
        self.assertEqual(result, ["steam"])
        # 19:00 — second window active, first not
        result = app_blocking._read_names_scheduled(self.path, now_min=1140)
        self.assertEqual(result, ["discord"])

    def test_deduplication_across_windows(self):
        _write(self.path, "# 08:00-12:00\nsteam\n\n# 08:00-17:00\nsteam\ndiscord\n")
        result = app_blocking._read_names_scheduled(self.path, now_min=600)  # 10:00
        self.assertEqual(result, ["discord", "steam"])  # steam deduplicated

    def test_missing_file(self):
        self.assertEqual(app_blocking._read_names_scheduled("/nonexistent.txt", 600), [])

    def test_none_path(self):
        self.assertEqual(app_blocking._read_names_scheduled(None, 600), [])

    def test_non_window_comment_skipped(self):
        _write(self.path, "# 09:00-17:00\nsteam\n# just a note\ndiscord\n")
        result = app_blocking._read_names_scheduled(self.path, now_min=600)
        self.assertEqual(result, ["discord", "steam"])


class TestFindPids(unittest.TestCase):
    def test_returns_pids_on_match(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "1234\n5678\n"
            pids = app_blocking._find_pids("steam")
        self.assertEqual(pids, [1234, 5678])
        mock_run.assert_called_once_with(
            ["pgrep", "-ix", "steam"], capture_output=True, text=True
        )

    def test_returns_empty_when_no_match(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            self.assertEqual(app_blocking._find_pids("steam"), [])

    def test_returns_empty_when_pgrep_missing(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            self.assertEqual(app_blocking._find_pids("steam"), [])

    def test_strips_whitespace_from_pids(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "  42  \n  99  \n"
            pids = app_blocking._find_pids("discord")
        self.assertEqual(pids, [42, 99])


class TestKill(unittest.TestCase):
    def test_delivers_sigterm(self):
        with patch("os.kill") as mock_kill:
            result = app_blocking._kill(1234)
        mock_kill.assert_called_once_with(1234, signal.SIGTERM)
        self.assertTrue(result)

    def test_returns_false_on_process_lookup_error(self):
        with patch("os.kill", side_effect=ProcessLookupError):
            self.assertFalse(app_blocking._kill(1234))

    def test_returns_false_on_permission_error(self):
        with patch("os.kill", side_effect=PermissionError):
            self.assertFalse(app_blocking._kill(1234))


class TestApplyTiers(unittest.TestCase):
    """apply() tier activation — which names get included under which conditions."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = self.dir.name

        def write(name, content):
            path = os.path.join(d, name)
            _write(path, content)
            return path

        app_blocking.app_blocklist_file          = write("blocklist-apps.txt",          "steam\n")
        app_blocking.app_blocklist_active_file   = write("blocklist-apps-active.txt",   "discord\n")
        app_blocking.app_blocklist_strict_file   = write("blocklist-apps-strict.txt",   "spotify\n")
        app_blocking.app_blocklist_schedule_file = write("blocklist-apps-schedule.txt",
                                                          "# 09:00-17:00\nvlc\n")

    def tearDown(self):
        self.dir.cleanup()
        app_blocking.app_blocklist_file          = None
        app_blocking.app_blocklist_active_file   = None
        app_blocking.app_blocklist_strict_file   = None
        app_blocking.app_blocklist_schedule_file = None

    def _apply(self, is_active, strict, now_min=600):
        with patch.object(app_blocking, "_find_pids", return_value=[]) as mock_find, \
             patch.object(app_blocking, "_kill") as mock_kill:
            app_blocking.apply(is_active=is_active, strict=strict, _now_min=now_min)
            return mock_find.call_args_list

    def test_always_tier_always_included(self):
        calls = self._apply(is_active=False, strict=False)
        names = [c.args[0] for c in calls]
        self.assertIn("steam", names)

    def test_active_tier_excluded_when_not_active(self):
        calls = self._apply(is_active=False, strict=False)
        names = [c.args[0] for c in calls]
        self.assertNotIn("discord", names)

    def test_active_tier_included_when_active(self):
        calls = self._apply(is_active=True, strict=False)
        names = [c.args[0] for c in calls]
        self.assertIn("discord", names)

    def test_strict_tier_excluded_when_not_strict(self):
        calls = self._apply(is_active=False, strict=False)
        names = [c.args[0] for c in calls]
        self.assertNotIn("spotify", names)

    def test_strict_tier_included_when_strict(self):
        calls = self._apply(is_active=False, strict=True)
        names = [c.args[0] for c in calls]
        self.assertIn("spotify", names)

    def test_schedule_tier_included_when_window_active(self):
        calls = self._apply(is_active=False, strict=False, now_min=600)  # 10:00 in 09:00–17:00
        names = [c.args[0] for c in calls]
        self.assertIn("vlc", names)

    def test_schedule_tier_excluded_outside_window(self):
        calls = self._apply(is_active=False, strict=False, now_min=1200)  # 20:00 not in window
        names = [c.args[0] for c in calls]
        self.assertNotIn("vlc", names)

    def test_all_tiers_active(self):
        calls = self._apply(is_active=True, strict=True, now_min=600)
        names = [c.args[0] for c in calls]
        self.assertIn("steam", names)
        self.assertIn("discord", names)
        self.assertIn("spotify", names)
        self.assertIn("vlc", names)

    def test_no_kill_when_no_pids(self):
        with patch.object(app_blocking, "_find_pids", return_value=[]) as _, \
             patch.object(app_blocking, "_kill") as mock_kill:
            app_blocking.apply(is_active=True, strict=True, _now_min=600)
        mock_kill.assert_not_called()


class TestApplyKills(unittest.TestCase):
    """apply() actually kills processes when pids are found."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = self.dir.name
        path = os.path.join(d, "blocklist-apps.txt")
        _write(path, "steam\n")
        app_blocking.app_blocklist_file          = path
        app_blocking.app_blocklist_active_file   = None
        app_blocking.app_blocklist_strict_file   = None
        app_blocking.app_blocklist_schedule_file = None

    def tearDown(self):
        self.dir.cleanup()
        app_blocking.app_blocklist_file = None

    def test_kills_each_pid(self):
        with patch.object(app_blocking, "_find_pids", return_value=[100, 200]), \
             patch.object(app_blocking, "_kill", return_value=True) as mock_kill:
            app_blocking.apply(is_active=False, strict=False)
        mock_kill.assert_has_calls([call(100), call(200)], any_order=False)

    def test_no_log_when_kill_returns_false(self):
        """Processes belonging to other users are silently skipped — no log emitted."""
        with patch.object(app_blocking, "_find_pids", return_value=[999]), \
             patch.object(app_blocking, "_kill", return_value=False), \
             patch.object(app_blocking.log, "info") as mock_info:
            app_blocking.apply(is_active=False, strict=False)
        mock_info.assert_not_called()


class TestApplyLogTrail(unittest.TestCase):
    """apply() logs kills with the why-it-acted format."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = self.dir.name

        def write(name, content):
            path = os.path.join(d, name)
            _write(path, content)
            return path

        app_blocking.app_blocklist_file          = write("blocklist-apps.txt",        "steam\n")
        app_blocking.app_blocklist_active_file   = write("blocklist-apps-active.txt", "discord\n")
        app_blocking.app_blocklist_strict_file   = None
        app_blocking.app_blocklist_schedule_file = None

    def tearDown(self):
        self.dir.cleanup()
        app_blocking.app_blocklist_file        = None
        app_blocking.app_blocklist_active_file = None

    def test_logs_kill_with_name_pid_and_tier(self):
        with patch.object(app_blocking, "_find_pids", return_value=[42]), \
             patch.object(app_blocking, "_kill", return_value=True), \
             self.assertLogs("breaktimer.apps", level="INFO") as cm:
            app_blocking.apply(is_active=False, strict=False)
        self.assertTrue(any("steam" in line and "42" in line and "always" in line
                            for line in cm.output))

    def test_logs_multi_tier_name(self):
        """A name in both always and active tiers shows combined tier label."""
        _write(app_blocking.app_blocklist_active_file, "steam\n")
        with patch.object(app_blocking, "_find_pids", return_value=[7]), \
             patch.object(app_blocking, "_kill", return_value=True), \
             self.assertLogs("breaktimer.apps", level="INFO") as cm:
            app_blocking.apply(is_active=True, strict=False)
        # steam appears in always+active
        steam_lines = [l for l in cm.output if "steam" in l]
        self.assertTrue(any("always" in l and "active" in l for l in steam_lines))

    def test_logs_active_tier_kill(self):
        with patch.object(app_blocking, "_find_pids", side_effect=lambda n: [99] if n == "discord" else []), \
             patch.object(app_blocking, "_kill", return_value=True), \
             self.assertLogs("breaktimer.apps", level="INFO") as cm:
            app_blocking.apply(is_active=True, strict=False)
        self.assertTrue(any("discord" in line and "active" in line for line in cm.output))

    def test_no_log_when_no_processes_running(self):
        with patch.object(app_blocking, "_find_pids", return_value=[]):
            # assertLogs would fail if no log emitted — we just verify no crash.
            app_blocking.apply(is_active=True, strict=False)


class TestReadNamePublicAPIs(unittest.TestCase):
    """Public read_names*() helpers delegate to the private implementations."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = self.dir.name

        def write(name, content):
            path = os.path.join(d, name)
            _write(path, content)
            return path

        app_blocking.app_blocklist_file          = write("apps.txt",      "steam\n")
        app_blocking.app_blocklist_active_file   = write("active.txt",    "discord\n")
        app_blocking.app_blocklist_strict_file   = write("strict.txt",    "spotify\n")
        app_blocking.app_blocklist_schedule_file = write("schedule.txt",  "# 08:00-20:00\nvlc\n")

    def tearDown(self):
        self.dir.cleanup()
        app_blocking.app_blocklist_file          = None
        app_blocking.app_blocklist_active_file   = None
        app_blocking.app_blocklist_strict_file   = None
        app_blocking.app_blocklist_schedule_file = None

    def test_read_names(self):
        self.assertEqual(app_blocking.read_names(), ["steam"])

    def test_read_names_active(self):
        self.assertEqual(app_blocking.read_names_active(), ["discord"])

    def test_read_names_strict(self):
        self.assertEqual(app_blocking.read_names_strict(), ["spotify"])

    def test_read_names_schedule_in_window(self):
        self.assertEqual(app_blocking.read_names_schedule(now_min=600), ["vlc"])

    def test_read_names_schedule_outside_window(self):
        self.assertEqual(app_blocking.read_names_schedule(now_min=1380), [])


class TestApplyAllFilesAbsent(unittest.TestCase):
    """apply() with all file paths unset is a no-op — no crash."""

    def setUp(self):
        app_blocking.app_blocklist_file          = None
        app_blocking.app_blocklist_active_file   = None
        app_blocking.app_blocklist_strict_file   = None
        app_blocking.app_blocklist_schedule_file = None

    def test_no_crash(self):
        with patch.object(app_blocking, "_find_pids") as mock_find, \
             patch.object(app_blocking, "_kill") as mock_kill:
            app_blocking.apply(is_active=True, strict=True)
        mock_find.assert_not_called()
        mock_kill.assert_not_called()


# ---------------------------------------------------------------------------
# read_schedule_windows
# ---------------------------------------------------------------------------

class TestReadScheduleWindowsApps(unittest.TestCase):
    """read_schedule_windows returns all windows with their names and active state."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        app_blocking.app_blocklist_schedule_file = None

    def tearDown(self):
        self._tmp.cleanup()

    def _sched(self, content):
        path = os.path.join(self._tmp.name, "blocklist-apps-schedule.txt")
        with open(path, "w") as f:
            f.write(content)
        app_blocking.app_blocklist_schedule_file = path

    def test_none_path_returns_empty(self):
        self.assertEqual(app_blocking.read_schedule_windows(now_min=600), [])

    def test_returns_active_window(self):
        self._sched("# 09:00-17:00\nsteam\n")
        windows = app_blocking.read_schedule_windows(now_min=12 * 60)
        self.assertEqual(len(windows), 1)
        start, end, names, is_active = windows[0]
        self.assertEqual(start, 9 * 60)
        self.assertEqual(end, 17 * 60)
        self.assertEqual(names, ["steam"])
        self.assertTrue(is_active)

    def test_returns_inactive_window(self):
        self._sched("# 09:00-17:00\nsteam\n")
        windows = app_blocking.read_schedule_windows(now_min=20 * 60)
        self.assertEqual(len(windows), 1)
        _, _, names, is_active = windows[0]
        self.assertEqual(names, ["steam"])
        self.assertFalse(is_active)

    def test_returns_all_windows_not_just_active(self):
        self._sched("# 09:00-17:00\nsteam\n\n# 22:00-08:00\ndiscord\n")
        windows = app_blocking.read_schedule_windows(now_min=12 * 60)
        self.assertEqual(len(windows), 2)
        _, _, names0, active0 = windows[0]
        _, _, names1, active1 = windows[1]
        self.assertEqual(names0, ["steam"])
        self.assertTrue(active0)
        self.assertEqual(names1, ["discord"])
        self.assertFalse(active1)

    def test_empty_windows_omitted(self):
        self._sched("# 09:00-17:00\n# 22:00-08:00\ndiscord\n")
        windows = app_blocking.read_schedule_windows(now_min=12 * 60)
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0][1], 8 * 60)  # only the 22:00-08:00 window


if __name__ == "__main__":
    unittest.main()
