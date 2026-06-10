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
