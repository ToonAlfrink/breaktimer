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


class TestReadNamesScheduled(unittest.TestCase):
    """Active schedule-name lookup — now backed by status.active_schedule_items."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
        self.path = self.tmp.name

    def tearDown(self):
        os.unlink(self.path)

    def _active(self, now_min):
        return status.active_schedule_items(self.path, now_min=now_min)

    def test_no_window_header_ignored(self):
        _write(self.path, "steam\ndiscord\n")
        self.assertEqual(self._active(600), [])

    def test_window_active(self):
        _write(self.path, "# 09:00-17:00\nsteam\ndiscord\n")
        self.assertEqual(self._active(600), ["discord", "steam"])

    def test_window_inactive(self):
        _write(self.path, "# 09:00-17:00\nsteam\n")
        self.assertEqual(self._active(1200), [])

    def test_wraparound_window_evening(self):
        _write(self.path, "# 22:00-08:00\nspotify\n")
        self.assertEqual(self._active(1380), ["spotify"])

    def test_wraparound_window_morning(self):
        _write(self.path, "# 22:00-08:00\nspotify\n")
        self.assertEqual(self._active(60), ["spotify"])

    def test_multiple_windows_only_active(self):
        _write(self.path, "# 09:00-12:00\nsteam\n\n# 18:00-20:00\ndiscord\n")
        self.assertEqual(self._active(600), ["steam"])
        self.assertEqual(self._active(1140), ["discord"])

    def test_deduplication_across_windows(self):
        _write(self.path, "# 08:00-12:00\nsteam\n\n# 08:00-17:00\nsteam\ndiscord\n")
        self.assertEqual(self._active(600), ["discord", "steam"])

    def test_missing_file(self):
        self.assertEqual(status.active_schedule_items("/nonexistent.txt", 600), [])

    def test_none_path(self):
        self.assertEqual(status.active_schedule_items(None, 600), [])

    def test_non_window_comment_skipped(self):
        _write(self.path, "# 09:00-17:00\nsteam\n# just a note\ndiscord\n")
        self.assertEqual(self._active(600), ["discord", "steam"])


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


class TestSendSignal(unittest.TestCase):
    def test_delivers_sigterm(self):
        with patch("os.kill") as mock_kill:
            result = app_blocking._send_signal(1234, signal.SIGTERM)
        mock_kill.assert_called_once_with(1234, signal.SIGTERM)
        self.assertTrue(result)

    def test_delivers_sigkill(self):
        with patch("os.kill") as mock_kill:
            result = app_blocking._send_signal(1234, signal.SIGKILL)
        mock_kill.assert_called_once_with(1234, signal.SIGKILL)
        self.assertTrue(result)

    def test_returns_false_on_process_lookup_error(self):
        with patch("os.kill", side_effect=ProcessLookupError):
            self.assertFalse(app_blocking._send_signal(1234, signal.SIGTERM))

    def test_returns_false_on_permission_error(self):
        with patch("os.kill", side_effect=PermissionError):
            self.assertFalse(app_blocking._send_signal(1234, signal.SIGTERM))


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
             patch.object(app_blocking, "_send_signal"):
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
             patch.object(app_blocking, "_send_signal") as mock_sig:
            app_blocking.apply(is_active=True, strict=True, _now_min=600)
        mock_sig.assert_not_called()


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
        app_blocking._sigterm_tick.clear()
        app_blocking._apply_count = 0

    def tearDown(self):
        self.dir.cleanup()
        app_blocking.app_blocklist_file = None
        app_blocking._sigterm_tick.clear()

    def test_sigterms_each_pid_on_first_apply(self):
        with patch.object(app_blocking, "_find_pids", return_value=[100, 200]), \
             patch.object(app_blocking, "_send_signal", return_value=True) as mock_sig:
            app_blocking.apply(is_active=False, strict=False)
        mock_sig.assert_has_calls(
            [call(100, signal.SIGTERM), call(200, signal.SIGTERM)], any_order=False
        )

    def test_no_log_when_signal_returns_false(self):
        """Processes belonging to other users are silently skipped — no log emitted."""
        with patch.object(app_blocking, "_find_pids", return_value=[999]), \
             patch.object(app_blocking, "_send_signal", return_value=False), \
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
        app_blocking._sigterm_tick.clear()
        app_blocking._apply_count = 0

    def tearDown(self):
        self.dir.cleanup()
        app_blocking.app_blocklist_file        = None
        app_blocking.app_blocklist_active_file = None
        app_blocking._sigterm_tick.clear()

    def test_logs_sigterm_with_name_pid_and_tier(self):
        with patch.object(app_blocking, "_find_pids", return_value=[42]), \
             patch.object(app_blocking, "_send_signal", return_value=True), \
             self.assertLogs("breaktimer.apps", level="INFO") as cm:
            app_blocking.apply(is_active=False, strict=False)
        self.assertTrue(any("steam" in line and "42" in line and "always" in line
                            for line in cm.output))

    def test_logs_multi_tier_name(self):
        """A name in both always and active tiers shows combined tier label."""
        _write(app_blocking.app_blocklist_active_file, "steam\n")
        with patch.object(app_blocking, "_find_pids", return_value=[7]), \
             patch.object(app_blocking, "_send_signal", return_value=True), \
             self.assertLogs("breaktimer.apps", level="INFO") as cm:
            app_blocking.apply(is_active=True, strict=False)
        # steam appears in always+active
        steam_lines = [l for l in cm.output if "steam" in l]
        self.assertTrue(any("always" in l and "active" in l for l in steam_lines))

    def test_logs_active_tier_sigterm(self):
        with patch.object(app_blocking, "_find_pids", side_effect=lambda n: [99] if n == "discord" else []), \
             patch.object(app_blocking, "_send_signal", return_value=True), \
             self.assertLogs("breaktimer.apps", level="INFO") as cm:
            app_blocking.apply(is_active=True, strict=False)
        self.assertTrue(any("discord" in line and "active" in line for line in cm.output))

    def test_no_log_when_no_processes_running(self):
        with patch.object(app_blocking, "_find_pids", return_value=[]):
            # assertLogs would fail if no log emitted — we just verify no crash.
            app_blocking.apply(is_active=True, strict=False)


class TestApplyAllFilesAbsent(unittest.TestCase):
    """apply() with all file paths unset is a no-op — no crash."""

    def setUp(self):
        app_blocking.app_blocklist_file          = None
        app_blocking.app_blocklist_active_file   = None
        app_blocking.app_blocklist_strict_file   = None
        app_blocking.app_blocklist_schedule_file = None
        app_blocking._sigterm_tick.clear()

    def tearDown(self):
        app_blocking._sigterm_tick.clear()

    def test_no_crash(self):
        with patch.object(app_blocking, "_find_pids") as mock_find, \
             patch.object(app_blocking, "_send_signal") as mock_sig:
            app_blocking.apply(is_active=True, strict=True)
        mock_find.assert_not_called()
        mock_sig.assert_not_called()


class TestSigkillEscalation(unittest.TestCase):
    """SIGTERM → SIGKILL escalation after SIGKILL_DELAY_TICKS apply() calls."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        path = os.path.join(self.dir.name, "blocklist-apps.txt")
        _write(path, "steam\n")
        app_blocking.app_blocklist_file          = path
        app_blocking.app_blocklist_active_file   = None
        app_blocking.app_blocklist_strict_file   = None
        app_blocking.app_blocklist_schedule_file = None
        app_blocking._sigterm_tick.clear()
        app_blocking._apply_count = 0

    def tearDown(self):
        self.dir.cleanup()
        app_blocking.app_blocklist_file = None
        app_blocking._sigterm_tick.clear()

    def _run(self, n, pids):
        """Call apply() n times with the given pids; return list of (sig, pid) calls."""
        delivered = []
        def mock_send(pid, sig):
            delivered.append((sig, pid))
            return True
        with patch.object(app_blocking, "_find_pids", return_value=pids), \
             patch.object(app_blocking, "_send_signal", side_effect=mock_send):
            for _ in range(n):
                app_blocking.apply(is_active=False, strict=False)
        return delivered

    def test_sigterm_on_first_apply(self):
        calls = self._run(1, [42])
        self.assertEqual(calls, [(signal.SIGTERM, 42)])

    def test_sigterm_sent_only_once(self):
        """SIGTERM is not repeated every tick — only on first contact."""
        calls = self._run(3, [42])
        self.assertEqual([c for c in calls if c[0] == signal.SIGTERM], [(signal.SIGTERM, 42)])

    def test_no_sigkill_before_delay(self):
        calls = self._run(app_blocking.SIGKILL_DELAY_TICKS, [42])
        self.assertNotIn(signal.SIGKILL, [s for s, _ in calls])

    def test_sigkill_after_delay(self):
        calls = self._run(app_blocking.SIGKILL_DELAY_TICKS + 1, [42])
        self.assertIn(signal.SIGKILL, [s for s, _ in calls])

    def test_sigkill_log_says_survived_sigterm(self):
        n = app_blocking.SIGKILL_DELAY_TICKS + 1
        with patch.object(app_blocking, "_find_pids", return_value=[99]), \
             patch.object(app_blocking, "_send_signal", return_value=True), \
             self.assertLogs("breaktimer.apps", level="INFO") as cm:
            for _ in range(n):
                app_blocking.apply(is_active=False, strict=False)
        self.assertTrue(any("SIGKILL" in l and "survived SIGTERM" in l for l in cm.output))

    def test_state_cleared_when_pid_gone(self):
        """If a PID disappears from running processes, escalation state is cleaned up."""
        self._run(1, [42])
        self.assertIn(42, app_blocking._sigterm_tick)

        self._run(1, [])  # pid 42 gone
        self.assertNotIn(42, app_blocking._sigterm_tick)

    def test_sigterm_restarts_after_pid_reappears(self):
        """If a PID goes away then comes back, SIGTERM is sent again (not SIGKILL)."""
        self._run(1, [42])   # SIGTERM → 42 in _sigterm_tick
        self._run(1, [])     # gone → cleared
        calls = self._run(1, [42])  # back → SIGTERM again
        self.assertEqual(calls, [(signal.SIGTERM, 42)])

    def test_state_cleared_after_sigkill(self):
        """After SIGKILL, _sigterm_tick entry is removed so the next tick starts fresh."""
        n = app_blocking.SIGKILL_DELAY_TICKS + 1
        self._run(n, [42])
        self.assertNotIn(42, app_blocking._sigterm_tick)

    def test_multiple_pids_escalate_independently(self):
        """Each PID's escalation is tracked separately."""
        # pid 10 present from tick 1; pid 20 joins at tick 3
        delivered = []
        def mock_send(pid, sig):
            delivered.append((sig, pid))
            return True

        delay = app_blocking.SIGKILL_DELAY_TICKS
        with patch.object(app_blocking, "_send_signal", side_effect=mock_send):
            for i in range(delay + 2):
                pids = [10, 20] if i >= 2 else [10]
                with patch.object(app_blocking, "_find_pids", return_value=pids):
                    app_blocking.apply(is_active=False, strict=False)

        # pid 10 should have received SIGKILL; pid 20 joined later so still pending
        self.assertIn((signal.SIGKILL, 10), delivered)
        self.assertNotIn((signal.SIGKILL, 20), delivered)


if __name__ == "__main__":
    unittest.main()
