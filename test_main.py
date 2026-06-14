"""Smoke tests for the shutdown-power core.

This app is trusted to power off the machine, so the load-bearing arithmetic —
state persistence, depletion/replenishment, and the shutdown grace window —
is pinned here against regression.

Run: python3 -m unittest -q
"""
import argparse
import json
import os
import subprocess
import sys
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

    def __init__(self, healthy=True):
        self._t = time.monotonic()
        self._healthy = healthy

    def get_last_activity_time(self):
        return self._t

    def set_last_activity_time(self, t):
        self._t = t

    def is_healthy(self):
        return self._healthy


def make_loop(remaining, max_seconds=3600, replenish_seconds=1200,
              daily_budget_seconds=8 * 3600, daily_limit_seconds=10 * 3600,
              today_total=None):
    state = TimerState(remaining_time=remaining)
    if today_total is not None:
        state.daily_work_totals[main.today_str()] = today_total
    return TimerLoop(state, 0, StubMonitor(), max_seconds, replenish_seconds,
                     daily_budget_seconds, daily_limit_seconds)


class InTempDir(unittest.TestCase):
    """Each test gets a private temp state dir so STATE_FILE never touches real state."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        state_dir = os.path.join(self._tmp.name, "breaktimer")
        os.makedirs(state_dir, mode=0o700)
        state_file = os.path.join(state_dir, "state.json")
        self._dir_patch = mock.patch.object(main, "STATE_DIR", state_dir)
        self._file_patch = mock.patch.object(main, "STATE_FILE", state_file)
        self._dir_patch.start()
        self._file_patch.start()

    def tearDown(self):
        self._file_patch.stop()
        self._dir_patch.stop()
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
        self.assertEqual(os.listdir(main.STATE_DIR), ["state.json"])

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
        loop.grace_start = time.monotonic() - TimerLoop.GRACE_SECONDS - 1
        with mock.patch.object(main, "execute_shutdown") as shutdown:
            self.assertTrue(loop._check_shutdown())
        shutdown.assert_called_once()
        self.assertIsNotNone(load_state_from_file())

    def test_refill_cancels_grace_window(self):
        loop = make_loop(120)
        loop.grace_start = time.monotonic()
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

    def test_unhealthy_monitor_always_marks_active(self):
        # If libinput can't run we can't see input — conservatively assume active
        # so the bar drains rather than refilling forever.
        loop = make_loop(600)
        loop.activity_monitor = StubMonitor(healthy=False)
        now = time.time()
        loop.activity_monitor.set_last_activity_time(now - 300)  # would be idle if healthy
        loop._update_activity_status(now, 1)
        self.assertTrue(loop.state.is_active)


class TestClockResilience(InTempDir):
    """A clock that lurches forward (NTP step, resume-from-suspend) must not let a
    single tick drain the bar and trip the uncancellable shutdown grace."""

    def _run_one_tick(self, loop, jumped):
        with mock.patch("main.time.monotonic", return_value=jumped), \
             mock.patch("main.time.sleep", side_effect=StopIteration), \
             mock.patch.object(main, "execute_shutdown") as shutdown, \
             mock.patch.object(main, "set_brightness_by_fraction"), \
             mock.patch.object(main, "set_sensitivity_by_fraction"), \
             mock.patch.object(main, "_notify"), \
             mock.patch.object(status, "write_status"):
            with self.assertRaises(StopIteration):
                loop.run()
        return shutdown

    def test_giant_tick_meters_at_most_max_and_does_not_shut_down(self):
        # Worst case: monitor is down (so the tick is forced active and drains).
        loop = make_loop(3600)
        loop.activity_monitor = StubMonitor(healthy=False)
        shutdown = self._run_one_tick(loop, loop.last_loop_time + 4 * 3600)
        self.assertGreaterEqual(loop.state.remaining_time, 3600 - loop.MAX_TICK_SECONDS)
        shutdown.assert_not_called()

    def test_giant_tick_with_healthy_monitor_reads_as_idle(self):
        # A large gap is downtime, not work — it must never drain the bar.
        loop = make_loop(1800)
        shutdown = self._run_one_tick(loop, loop.last_loop_time + 4 * 3600)
        self.assertGreaterEqual(loop.state.remaining_time, 1800)
        shutdown.assert_not_called()


class TestMonitorHealthNotification(unittest.TestCase):
    def test_fires_critical_when_monitor_goes_down(self):
        loop = make_loop(600)
        loop.activity_monitor = StubMonitor(healthy=False)
        with mock.patch.object(main, "_notify") as notif:
            loop._check_monitor_health()
        notif.assert_called_once()
        self.assertEqual(notif.call_args.kwargs["urgency"], "critical")

    def test_fires_only_once_not_every_tick(self):
        loop = make_loop(600)
        loop.activity_monitor = StubMonitor(healthy=False)
        with mock.patch.object(main, "_notify") as notif:
            loop._check_monitor_health()
            loop._check_monitor_health()
        self.assertEqual(notif.call_count, 1)

    def test_notifies_recovery_and_clears_flag(self):
        loop = make_loop(600)
        loop.activity_monitor = StubMonitor(healthy=False)
        with mock.patch.object(main, "_notify"):
            loop._check_monitor_health()
        self.assertIn("monitor-down", loop._notified)
        loop.activity_monitor = StubMonitor(healthy=True)
        with mock.patch.object(main, "_notify") as notif:
            loop._check_monitor_health()
        self.assertNotIn("monitor-down", loop._notified)
        notif.assert_called_once()

    def test_no_noise_when_healthy(self):
        loop = make_loop(600)  # StubMonitor defaults to healthy=True
        with mock.patch.object(main, "_notify") as notif:
            loop._check_monitor_health()
        notif.assert_not_called()


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

    def test_saved_zero_not_inflated_to_cap(self):
        # remaining_time=0 saved before a limit-triggered shutdown must survive
        # a service restart unchanged — this is what makes re-shut-on-login work.
        save_state_to_file(TimerState(remaining_time=0))
        state = initialize_state(self._args(), 3600)
        self.assertEqual(state.remaining_time, 0)


class TestExecuteShutdown(unittest.TestCase):
    """execute_shutdown uses absolute paths, no sudo, and falls through on failure."""

    def test_first_command_is_busctl_dbus(self):
        with mock.patch("subprocess.run") as run:
            main.execute_shutdown()
        first = run.call_args_list[0].args[0]
        self.assertEqual(first[0], '/usr/bin/busctl')
        self.assertIn('PowerOff', first)

    def test_no_sudo_in_any_command(self):
        import io
        calls = []
        def side_effect(cmd, **kw):
            calls.append(cmd)
            raise subprocess.CalledProcessError(1, cmd)
        with mock.patch("subprocess.run", side_effect=side_effect), \
             mock.patch("sys.stderr", io.StringIO()):
            main.execute_shutdown()
        self.assertFalse(any("sudo" in c[0] for c in calls), "sudo must not appear in any shutdown command")

    def test_falls_through_busctl_to_systemctl_to_shutdown(self):
        calls = []
        def side_effect(cmd, **kw):
            calls.append(cmd[0])
            if 'busctl' not in cmd[0]:
                raise subprocess.CalledProcessError(1, cmd)
        with mock.patch("subprocess.run", side_effect=side_effect):
            main.execute_shutdown()
        self.assertEqual(calls, ['/usr/bin/busctl'])

    def test_all_fail_logs_error_to_stderr(self):
        import io
        buf = io.StringIO()
        with mock.patch("subprocess.run",
                        side_effect=subprocess.CalledProcessError(1, [])):
            with mock.patch("sys.stderr", buf):
                main.execute_shutdown()
        self.assertIn("ERROR", buf.getvalue())


class TestRestartAfterShutdown(InTempDir):
    """Service restarts on login with remaining_time=0 saved — it must re-enter
    the grace window and power off again until midnight resets the daily total."""

    def test_restart_at_zero_past_limit_enters_grace(self):
        state = TimerState(remaining_time=0)
        state.daily_work_totals[main.today_str()] = 10 * 3600
        save_state_to_file(state)
        loaded = load_state_from_file()

        loop = TimerLoop(loaded, 0, StubMonitor(), 3600, 1200, 8 * 3600, 10 * 3600)
        loop.state.is_active = False
        loop._adjust_timer(1)
        with mock.patch.object(main, "execute_shutdown"):
            loop._check_shutdown()
        self.assertIsNotNone(loop.grace_start,
                             "grace must start on restart at zero past the daily limit")

    def test_yesterday_limit_does_not_block_today(self):
        # A large total filed under a past date must not throttle today's refill.
        loop = make_loop(600)
        loop.state.daily_work_totals["2000-01-01"] = 11 * 3600
        self.assertEqual(loop._refill_multiplier(), 1.0)


class TestGraceRemaining(unittest.TestCase):
    def test_returns_none_when_not_in_grace(self):
        self.assertIsNone(make_loop(100)._grace_remaining())

    def test_returns_countdown_during_grace(self):
        loop = make_loop(0)
        loop.grace_start = time.monotonic() - 10
        self.assertAlmostEqual(loop._grace_remaining(), TimerLoop.GRACE_SECONDS - 10, delta=1)

    def test_clamps_at_zero_when_elapsed(self):
        loop = make_loop(0)
        loop.grace_start = time.monotonic() - TimerLoop.GRACE_SECONDS - 10
        self.assertEqual(loop._grace_remaining(), 0.0)


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
            loop._check_notifications()  # fires 10-min and 5-min (both thresholds crossed)
        loop.state.remaining_time = 119
        with mock.patch.object(main, "_notify") as notif:
            loop._check_notifications()
        self.assertIn("2 minutes", notif.call_args[0][0])
        self.assertEqual(notif.call_args[1]["urgency"], "critical")

    def test_doesnt_refire_on_repeated_ticks_below_threshold(self):
        # Cross the threshold from above, then verify no second notification.
        loop = make_loop(601)
        loop.state.remaining_time = 599
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
        loop.grace_start = time.monotonic() - 5
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
        loop.grace_start = time.monotonic() - 5
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

    def test_no_startup_spam_when_already_below_threshold(self):
        # Process restart while remaining < all thresholds must not re-fire
        # notifications the user already saw before the crash.
        loop = make_loop(90)  # below 10-min, 5-min, and 2-min
        with mock.patch.object(main, "_notify") as notif:
            loop._check_notifications()
        notif.assert_not_called()

    def test_startup_below_one_threshold_fires_only_new_descent(self):
        # Started at 400s (below 10-min but above 5-min): 10-min pre-populated,
        # so no notification until timer crosses the 5-min level.
        loop = make_loop(400)
        with mock.patch.object(main, "_notify") as notif:
            loop._check_notifications()
        notif.assert_not_called()

        loop.state.remaining_time = 299
        with mock.patch.object(main, "_notify") as notif:
            loop._check_notifications()
        notif.assert_called_once()
        self.assertIn("5 minutes", notif.call_args[0][0])

    def test_grace_notification_refires_after_grace_cancelled_and_new_grace_starts(self):
        loop = make_loop(0)
        loop.grace_start = time.monotonic() - 5
        with mock.patch.object(main, "_notify"):
            loop._check_notifications()
        self.assertIn("grace", loop._notified)

        # idle refill cancels the grace window
        loop.state.remaining_time = 300
        loop.grace_start = None
        with mock.patch.object(main, "_notify"):
            loop._check_notifications()
        self.assertNotIn("grace", loop._notified)

        # timer depletes to 0 again and a new grace window opens
        loop.state.remaining_time = 0
        loop.grace_start = time.monotonic() - 2
        with mock.patch.object(main, "_notify") as notif:
            loop._check_notifications()
        grace_calls = [c[0][0] for c in notif.call_args_list if "Shutting down" in c[0][0]]
        self.assertEqual(len(grace_calls), 1)


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
            loop.grace_start = time.monotonic() - 10
            loop._write_status()
            snap = status_mod.read_status()
        self.assertEqual(snap["remaining_seconds"], 900)
        self.assertEqual(snap["max_seconds"], 3600)
        self.assertAlmostEqual(snap["grace_remaining"], 50, delta=2)
        self.assertIn("history", snap)
        self.assertFalse(snap["monitor_down"])  # StubMonitor is healthy


class TestUnconditionalLimit(unittest.TestCase):
    """Pin the invariant: the timer limit cannot be extended externally.

    Capability stripped the entire extend pathway (command channel, extend
    handler, click UI) in commit 70fe335. These tests make it impossible to
    accidentally re-introduce an escape hatch without breaking the suite.
    """

    def test_no_extend_method_on_timer_loop(self):
        self.assertFalse(hasattr(TimerLoop, "extend"),
                         "TimerLoop must have no extend method — the limit is unconditional")

    def test_no_check_commands_method_on_timer_loop(self):
        self.assertFalse(hasattr(TimerLoop, "_check_commands"),
                         "TimerLoop must not poll a command channel — polling was removed with extend")

    def test_active_timer_strictly_depletes(self):
        loop = make_loop(600)
        loop.state.is_active = True
        for _ in range(5):
            before = loop.state.remaining_time
            loop._adjust_timer(10)
            self.assertLess(loop.state.remaining_time, before,
                            "each active tick must decrease remaining_time — no silent refill")


class TestCLI(unittest.TestCase):
    _CLI = os.path.join(os.path.dirname(os.path.abspath(main.__file__)), "breaktimer")

    def test_extend_subcommand_exits_nonzero(self):
        result = subprocess.run(
            [sys.executable, self._CLI, "extend"],
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0,
                            "breaktimer extend must exit non-zero — the subcommand was removed")


class TestRefillFatigue(unittest.TestCase):
    """The day has gravity: idle refill decays past the daily budget and
    stops at the daily limit, so an over-long day ends when the bar drains."""

    def test_full_rate_below_budget(self):
        self.assertEqual(make_loop(600, today_total=7 * 3600)._refill_multiplier(), 1.0)

    def test_full_rate_at_budget(self):
        self.assertEqual(make_loop(600, today_total=8 * 3600)._refill_multiplier(), 1.0)

    def test_half_rate_midway_between_budget_and_limit(self):
        self.assertAlmostEqual(make_loop(600, today_total=9 * 3600)._refill_multiplier(), 0.5)

    def test_zero_rate_at_limit(self):
        self.assertEqual(make_loop(600, today_total=10 * 3600)._refill_multiplier(), 0.0)

    def test_zero_rate_beyond_limit(self):
        self.assertEqual(make_loop(600, today_total=11 * 3600)._refill_multiplier(), 0.0)

    def test_no_total_today_means_full_rate(self):
        self.assertEqual(make_loop(600)._refill_multiplier(), 1.0)

    def test_replenish_scaled_by_fatigue(self):
        # 3x base refill at half fatigue → 1.5x
        loop = make_loop(600, today_total=9 * 3600)
        loop.state.is_active = False
        loop._adjust_timer(10)
        self.assertAlmostEqual(loop.state.remaining_time, 615)

    def test_no_replenish_at_daily_limit(self):
        loop = make_loop(600, today_total=10 * 3600)
        loop.state.is_active = False
        loop._adjust_timer(100)
        self.assertEqual(loop.state.remaining_time, 600)

    def test_active_depletion_unaffected_by_fatigue(self):
        loop = make_loop(600, today_total=11 * 3600)
        loop.state.is_active = True
        loop._adjust_timer(10)
        self.assertEqual(loop.state.remaining_time, 590)

    def test_grace_not_cancellable_past_limit(self):
        # idle no longer refills, so remaining stays 0 and grace runs out
        loop = make_loop(0, today_total=10 * 3600)
        loop.state.is_active = False
        loop._adjust_timer(30)
        with mock.patch.object(main, "execute_shutdown") as shutdown:
            self.assertFalse(loop._check_shutdown())
            self.assertIsNotNone(loop.grace_start)
            loop.grace_start = time.monotonic() - TimerLoop.GRACE_SECONDS - 1
            with mock.patch.object(main, "save_state_to_file"):
                self.assertTrue(loop._check_shutdown())
        shutdown.assert_called_once()

    def test_status_payload_includes_refill_rate(self):
        loop = make_loop(600, today_total=9 * 3600)
        with mock.patch.object(status, "write_status") as write:
            loop._write_status()
        self.assertAlmostEqual(write.call_args[0][0]["refill_rate"], 0.5)


class TestDailyNotifications(unittest.TestCase):
    def test_budget_crossing_fires_once(self):
        loop = make_loop(600, today_total=8 * 3600 - 5)
        loop.state.is_active = True
        with mock.patch.object(main, "_notify") as notif:
            loop._adjust_timer(10)
            loop._check_notifications()
            loop._adjust_timer(10)
            loop._check_notifications()
        budget_calls = [c for c in notif.call_args_list if "refill slower" in c.args[0]]
        self.assertEqual(len(budget_calls), 1)
        self.assertIn("8h worked today", budget_calls[0].args[0])

    def test_limit_crossing_fires_critical(self):
        loop = make_loop(600, today_total=10 * 3600 - 5)
        loop.state.is_active = True
        with mock.patch.object(main, "_notify") as notif:
            loop._adjust_timer(10)
            loop._check_notifications()
        limit_calls = [c for c in notif.call_args_list if "no refill left" in c.args[0]]
        self.assertEqual(len(limit_calls), 1)
        self.assertEqual(limit_calls[0].kwargs.get("urgency"), "critical")

    def test_no_startup_spam_when_already_past_thresholds(self):
        loop = make_loop(600, today_total=11 * 3600)
        with mock.patch.object(main, "_notify") as notif:
            loop._check_notifications()
        notif.assert_not_called()

    def test_grace_message_honest_when_no_refill_left(self):
        loop = make_loop(0, today_total=10 * 3600)
        loop.grace_start = time.monotonic()
        with mock.patch.object(main, "_notify") as notif:
            loop._check_notifications()
        grace_calls = [c for c in notif.call_args_list if "Shutting down" in c.args[0]
                       or "shutting down" in c.args[0]]
        self.assertEqual(len(grace_calls), 1)
        self.assertNotIn("go idle", grace_calls[0].args[0])
        self.assertIn("Day limit", grace_calls[0].args[0])

    def test_grace_message_offers_idle_escape_below_limit(self):
        loop = make_loop(0, today_total=3600)
        loop.grace_start = time.monotonic()
        with mock.patch.object(main, "_notify") as notif:
            loop._check_notifications()
        self.assertIn("go idle to cancel", notif.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
