"""Smoke tests for the shutdown-power core.

This app is trusted to power off the machine, so the load-bearing arithmetic —
state persistence, depletion/replenishment, and the shutdown grace window —
is pinned here against regression.

Run: python3 -m unittest -q
"""
import argparse
import json
import os
import tempfile
import time
import unittest
from unittest import mock

import main
import status
from main import (
    TimerLoop,
    TimerState,
    compute_offline_duration_seconds,
    initialize_state,
    load_state_from_file,
    save_state_to_file,
)


class StubMonitor:
    """Stands in for ActivityMonitor without spawning libinput."""

    def __init__(self):
        self._t = time.time()

    def get_last_activity_time(self):
        return self._t

    def set_last_activity_time(self, t):
        self._t = t


def make_loop(remaining, max_seconds=3600, replenish_seconds=1200):
    state = TimerState(remaining_time=remaining)
    return TimerLoop(state, 0, StubMonitor(), max_seconds, replenish_seconds)


class InTempDir(unittest.TestCase):
    """Each test runs in a fresh temp cwd so STATE_FILE never touches real state."""

    def setUp(self):
        self._old_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()


class TestStatePersistence(InTempDir):
    def test_round_trip(self):
        state = TimerState(remaining_time=1234.5, daily_work_totals={"2026-06-09": 7200.0})
        save_state_to_file(state)
        loaded = load_state_from_file()
        self.assertAlmostEqual(loaded.remaining_time, 1234.5)
        self.assertEqual(loaded.daily_work_totals, {"2026-06-09": 7200.0})
        self.assertIsNotNone(loaded.last_saved_time)

    def test_only_durable_fields_persisted(self):
        save_state_to_file(TimerState(remaining_time=10))
        with open(main.STATE_FILE) as f:
            keys = set(json.load(f))
        self.assertEqual(keys, {"remaining_time", "daily_work_totals", "last_saved_time"})

    def test_save_is_atomic_leaves_no_temp_file(self):
        save_state_to_file(TimerState(remaining_time=10))
        self.assertEqual(os.listdir("."), [main.STATE_FILE])

    def test_missing_file_returns_none(self):
        self.assertIsNone(load_state_from_file())

    def test_corrupt_file_returns_none(self):
        with open(main.STATE_FILE, "w") as f:
            f.write("{truncated")
        with mock.patch("sys.stderr"):
            self.assertIsNone(load_state_from_file())

    def test_offline_duration_from_last_saved_time(self):
        state = TimerState(remaining_time=10, last_saved_time=time.time() - 100)
        self.assertAlmostEqual(compute_offline_duration_seconds(state), 100, delta=5)

    def test_offline_duration_zero_without_history(self):
        self.assertEqual(compute_offline_duration_seconds(TimerState(remaining_time=10)), 0.0)


class TestTimerArithmetic(unittest.TestCase):
    def test_active_depletes_and_tallies_work(self):
        loop = make_loop(600)
        loop.state.is_active = True
        loop._adjust_timer(10)
        self.assertEqual(loop.state.remaining_time, 590)
        self.assertEqual(loop.state.daily_work_totals[main.today_str()], 10)

    def test_idle_replenishes_at_deplete_to_replenish_ratio(self):
        # 3600s cap / 1200s replenish = 3x refill speed
        loop = make_loop(600, max_seconds=3600, replenish_seconds=1200)
        loop.state.is_active = False
        loop._adjust_timer(10)
        self.assertEqual(loop.state.remaining_time, 630)
        self.assertNotIn(main.today_str(), loop.state.daily_work_totals)

    def test_replenish_clamps_at_max(self):
        loop = make_loop(3590, max_seconds=3600, replenish_seconds=1200)
        loop.state.is_active = False
        loop._adjust_timer(1000)
        self.assertEqual(loop.state.remaining_time, 3600)


class TestShutdownGrace(InTempDir):
    def test_zero_starts_grace_without_shutting_down(self):
        loop = make_loop(0)
        with mock.patch.object(main, "execute_shutdown") as shutdown:
            self.assertFalse(loop._check_shutdown())
        shutdown.assert_not_called()
        self.assertIsNotNone(loop.grace_start)

    def test_shutdown_fires_after_grace_elapses_and_saves_state(self):
        loop = make_loop(0)
        loop.grace_start = time.time() - TimerLoop.GRACE_SECONDS - 1
        with mock.patch.object(main, "execute_shutdown") as shutdown:
            self.assertTrue(loop._check_shutdown())
        shutdown.assert_called_once()
        self.assertIsNotNone(load_state_from_file())

    def test_refill_cancels_grace_window(self):
        loop = make_loop(120)
        loop.grace_start = time.time()
        with mock.patch.object(main, "execute_shutdown") as shutdown:
            self.assertFalse(loop._check_shutdown())
        shutdown.assert_not_called()
        self.assertIsNone(loop.grace_start)

    def test_negative_remaining_clamped_to_zero(self):
        loop = make_loop(-5)
        with mock.patch.object(main, "execute_shutdown"):
            loop._check_shutdown()
        self.assertEqual(loop.state.remaining_time, 0.0)


class TestActivityStatus(unittest.TestCase):
    def test_recent_activity_marks_active(self):
        loop = make_loop(600)
        now = time.time()
        loop.activity_monitor.set_last_activity_time(now - 5)
        loop._update_activity_status(now, 1)
        self.assertTrue(loop.state.is_active)

    def test_stale_activity_marks_idle(self):
        loop = make_loop(600)
        now = time.time()
        loop.activity_monitor.set_last_activity_time(now - 300)
        loop._update_activity_status(now, 1)
        self.assertFalse(loop.state.is_active)

    def test_long_loop_gap_counts_as_idle(self):
        # suspend/resume: a large gap between loop iterations is downtime, not work
        loop = make_loop(600)
        now = time.time()
        loop.activity_monitor.set_last_activity_time(now)
        loop._update_activity_status(now, 500)
        self.assertFalse(loop.state.is_active)


class TestInitializeState(InTempDir):
    @staticmethod
    def _args(start_minutes=None):
        return argparse.Namespace(start_minutes=start_minutes)

    def test_fresh_state_starts_full(self):
        state = initialize_state(self._args(), 3600)
        self.assertEqual(state.remaining_time, 3600)

    def test_saved_time_clamped_to_cap(self):
        save_state_to_file(TimerState(remaining_time=7200))
        state = initialize_state(self._args(), 3600)
        self.assertEqual(state.remaining_time, 3600)

    def test_start_minutes_override_clamped_to_cap(self):
        state = initialize_state(self._args(start_minutes=120), 3600)
        self.assertEqual(state.remaining_time, 3600)


class TestGraceRemaining(unittest.TestCase):
    def test_returns_none_when_not_in_grace(self):
        self.assertIsNone(make_loop(100)._grace_remaining())

    def test_returns_countdown_during_grace(self):
        loop = make_loop(0)
        loop.grace_start = time.time() - 10
        self.assertAlmostEqual(loop._grace_remaining(), TimerLoop.GRACE_SECONDS - 10, delta=1)

    def test_clamps_at_zero_when_elapsed(self):
        loop = make_loop(0)
        loop.grace_start = time.time() - TimerLoop.GRACE_SECONDS - 10
        self.assertEqual(loop._grace_remaining(), 0.0)


class TestExtendCommand(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self._tmp.name})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_extend_adds_seconds(self):
        status.write_command({"type": "extend", "seconds": 600})
        loop = make_loop(300)
        loop._check_commands()
        self.assertEqual(loop.state.remaining_time, 900)

    def test_extend_clamped_at_max(self):
        status.write_command({"type": "extend", "seconds": 600})
        loop = make_loop(3500)
        loop._check_commands()
        self.assertEqual(loop.state.remaining_time, 3600)

    def test_extend_cancels_grace(self):
        status.write_command({"type": "extend", "seconds": 600})
        loop = make_loop(0)
        loop.grace_start = time.time()
        loop._check_commands()
        self.assertIsNone(loop.grace_start)

    def test_no_command_is_noop(self):
        loop = make_loop(300)
        loop._check_commands()
        self.assertEqual(loop.state.remaining_time, 300)

    def test_unknown_command_type_ignored(self):
        status.write_command({"type": "frobnicate"})
        loop = make_loop(300)
        loop._check_commands()
        self.assertEqual(loop.state.remaining_time, 300)


class TestNotifications(unittest.TestCase):
    def test_fires_at_10min_threshold(self):
        loop = make_loop(601)
        with mock.patch.object(main, "_notify") as notif:
            loop._check_notifications()
        notif.assert_not_called()

        loop.state.remaining_time = 599
        with mock.patch.object(main, "_notify") as notif:
            loop._check_notifications()
        notif.assert_called_once()
        self.assertIn("10 minutes", notif.call_args[0][0])

    def test_fires_at_5min_threshold(self):
        loop = make_loop(301)
        with mock.patch.object(main, "_notify"):
            loop._check_notifications()  # fires 10-min
        loop.state.remaining_time = 299
        with mock.patch.object(main, "_notify") as notif:
            loop._check_notifications()
        notif.assert_called_once()
        self.assertIn("5 minutes", notif.call_args[0][0])

    def test_fires_at_2min_threshold(self):
        loop = make_loop(121)
        with mock.patch.object(main, "_notify"):
            loop._check_notifications()  # fires 10-min
        loop.state.remaining_time = 119
        with mock.patch.object(main, "_notify") as notif:
            loop._check_notifications()
        self.assertIn("2 minutes", notif.call_args[0][0])
        self.assertEqual(notif.call_args[1]["urgency"], "critical")

    def test_doesnt_refire_on_repeated_ticks_below_threshold(self):
        loop = make_loop(599)
        with mock.patch.object(main, "_notify") as notif:
            loop._check_notifications()
            loop._check_notifications()
        self.assertEqual(notif.call_count, 1)

    def test_refires_after_refill_above_threshold(self):
        loop = make_loop(599)
        with mock.patch.object(main, "_notify"):
            loop._check_notifications()
        # idle refill pushes back above 10-min threshold
        loop.state.remaining_time = 700
        with mock.patch.object(main, "_notify"):
            loop._check_notifications()  # threshold cleared
        # drops below again
        loop.state.remaining_time = 599
        with mock.patch.object(main, "_notify") as notif:
            loop._check_notifications()
        notif.assert_called_once()

    def test_grace_notification_fires_on_grace_start(self):
        loop = make_loop(0)
        loop.grace_start = time.time() - 5
        with mock.patch.object(main, "_notify") as notif:
            loop._check_notifications()
        # remaining=0 is below all thresholds, so multiple notifications fire;
        # verify the grace one is among them
        calls = [c[0][0] for c in notif.call_args_list]
        grace_calls = [msg for msg in calls if "Shutting down" in msg]
        self.assertEqual(len(grace_calls), 1)
        self.assertIn("critical", str(notif.call_args_list[0]))

    def test_grace_notification_clears_when_grace_ends(self):
        loop = make_loop(0)
        loop.grace_start = time.time() - 5
        with mock.patch.object(main, "_notify"):
            loop._check_notifications()
        self.assertIn("grace", loop._notified)
        # idle refill — grace window cancelled
        loop.state.remaining_time = 100
        loop.grace_start = None
        with mock.patch.object(main, "_notify"):
            loop._check_notifications()
        self.assertNotIn("grace", loop._notified)

    def test_notify_send_unavailable_is_silent(self):
        loop = make_loop(599)
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            loop._check_notifications()  # must not raise


class TestWriteStatusOSError(unittest.TestCase):
    def test_warns_once_then_silent(self):
        import io
        loop = make_loop(900)
        with mock.patch("status.write_status", side_effect=OSError("no tmpfs")):
            buf = io.StringIO()
            with mock.patch("sys.stderr", buf):
                loop._write_status()
                after_first = buf.getvalue()
                loop._write_status()
                after_second = buf.getvalue()
        self.assertIn("WARNING", after_first)
        self.assertEqual(after_first, after_second)  # no new output on second call


class TestLiveStatus(unittest.TestCase):
    def test_loop_publishes_snapshot(self):
        import status as status_mod
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": tmp}):
            loop = make_loop(900)
            loop.grace_start = time.time() - 10
            loop._write_status()
            snap = status_mod.read_status()
        self.assertEqual(snap["remaining_seconds"], 900)
        self.assertEqual(snap["max_seconds"], 3600)
        self.assertAlmostEqual(snap["grace_remaining"], 50, delta=2)
        self.assertIn("history", snap)


if __name__ == "__main__":
    unittest.main()
