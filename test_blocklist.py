"""Tests for the domain blocklist: /etc/hosts management.

The blocklist module maintains a marked-off sinkhole block in /etc/hosts,
rewriting it atomically and logging every mutation via the why-it-acted trail.
Four tiers: always-blocked, work-session-blocked (is_active),
strict/day-limit-blocked (strict), and schedule-blocked (time windows).

Run: python3 -m unittest -q
"""
import logging
import os
import tempfile
import unittest
from unittest import mock

import blocklist
import status

logging.getLogger("breaktimer").addHandler(logging.NullHandler())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_file(path, content):
    with open(path, "w") as f:
        f.write(content)


def _set_always(tmpdir, content):
    path = os.path.join(tmpdir, "blocklist.txt")
    _write_file(path, content)
    return path


def _set_active(tmpdir, content):
    path = os.path.join(tmpdir, "blocklist-active.txt")
    _write_file(path, content)
    return path


def _set_strict(tmpdir, content):
    path = os.path.join(tmpdir, "blocklist-strict.txt")
    _write_file(path, content)
    return path


def _set_schedule(tmpdir, content):
    path = os.path.join(tmpdir, "blocklist-schedule.txt")
    _write_file(path, content)
    return path


class InTempDir(unittest.TestCase):
    """Each test gets an isolated tmpdir, a temp hosts file, and a clean Blocklist instance."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # Create a temporary hosts file for the Blocklist to write to
        fh = tempfile.NamedTemporaryFile(mode="w", suffix="-hosts", delete=False)
        fh.close()
        self._hosts_file = fh.name
        self.bl = blocklist.Blocklist(self._tmp.name, hosts_path=self._hosts_file)
        # Disable DoH sinkholing so tests can check exact /etc/hosts content.
        self.bl.doh_domains = frozenset()

    def tearDown(self):
        try:
            os.unlink(self._hosts_file)
        except OSError:
            pass
        self._tmp.cleanup()

    @property
    def tmpdir(self):
        return self._tmp.name


# ---------------------------------------------------------------------------
# _block_lines
# ---------------------------------------------------------------------------

class TestBlockLines(unittest.TestCase):
    def test_empty_domains_returns_empty_string(self):
        self.assertEqual(blocklist._block_lines([]), "")

    def test_bare_domain_gets_www_variant(self):
        block = blocklist._block_lines(["example.com"])
        self.assertIn("0.0.0.0 example.com", block)
        self.assertIn("0.0.0.0 www.example.com", block)

    def test_www_domain_not_doubled(self):
        block = blocklist._block_lines(["www.example.com"])
        self.assertIn("0.0.0.0 www.example.com", block)
        self.assertNotIn("www.www.example.com", block)

    def test_block_has_begin_and_end_markers(self):
        block = blocklist._block_lines(["example.com"])
        self.assertTrue(block.startswith("# BEGIN breaktimer-blocklist\n"))
        self.assertIn("# END breaktimer-blocklist", block)

    def test_multiple_domains(self):
        block = blocklist._block_lines(["alpha.com", "beta.com"])
        self.assertIn("0.0.0.0 alpha.com", block)
        self.assertIn("0.0.0.0 beta.com", block)

    def test_no_duplicate_entries_when_www_already_in_list(self):
        block = blocklist._block_lines(["example.com", "www.example.com"])
        self.assertEqual(block.count("www.example.com"), 1)


# ---------------------------------------------------------------------------
# _splice
# ---------------------------------------------------------------------------

class TestSplice(unittest.TestCase):
    _SIMPLE = "127.0.0.1 localhost\n"

    def test_appends_block_when_no_markers(self):
        result = blocklist._splice(self._SIMPLE, "# BEGIN breaktimer-blocklist\n0.0.0.0 x.com\n# END breaktimer-blocklist\n")
        self.assertIn("0.0.0.0 x.com", result)
        self.assertIn("127.0.0.1 localhost", result)

    def test_replaces_existing_block(self):
        existing = (
            "127.0.0.1 localhost\n"
            "# BEGIN breaktimer-blocklist\n"
            "0.0.0.0 old.com\n"
            "# END breaktimer-blocklist\n"
        )
        new_block = "# BEGIN breaktimer-blocklist\n0.0.0.0 new.com\n# END breaktimer-blocklist\n"
        result = blocklist._splice(existing, new_block)
        self.assertIn("0.0.0.0 new.com", result)
        self.assertNotIn("old.com", result)
        self.assertIn("127.0.0.1 localhost", result)

    def test_removes_block_when_empty(self):
        existing = (
            "127.0.0.1 localhost\n"
            "# BEGIN breaktimer-blocklist\n"
            "0.0.0.0 x.com\n"
            "# END breaktimer-blocklist\n"
        )
        result = blocklist._splice(existing, "")
        self.assertNotIn("BEGIN", result)
        self.assertNotIn("x.com", result)
        self.assertIn("127.0.0.1 localhost", result)

    def test_no_triple_blank_lines_after_removal(self):
        existing = "127.0.0.1 localhost\n\n# BEGIN breaktimer-blocklist\n0.0.0.0 x.com\n# END breaktimer-blocklist\n\n"
        result = blocklist._splice(existing, "")
        self.assertNotIn("\n\n\n", result)

    def test_remove_from_hosts_without_block_is_noop(self):
        result = blocklist._splice(self._SIMPLE, "")
        self.assertEqual(result.strip(), self._SIMPLE.strip())


# ---------------------------------------------------------------------------
# apply() — end-to-end with a temp hosts file
# ---------------------------------------------------------------------------

class TestApply(InTempDir):
    """apply() writes /etc/hosts only when content changes, logs each mutation."""

    def setUp(self):
        super().setUp()
        with open(self._hosts_file, "w") as f:
            f.write("127.0.0.1 localhost\n")

    def test_apply_with_domains_writes_sinkhole_block(self):
        _set_always(self.tmpdir, "example.com\n")
        self.bl.apply()
        with open(self._hosts_file) as f:
            content = f.read()
        self.assertIn("0.0.0.0 example.com", content)
        self.assertIn("# BEGIN breaktimer-blocklist", content)

    def test_apply_is_idempotent(self):
        _set_always(self.tmpdir, "example.com\n")
        self.bl.apply()
        mtime1 = os.stat(self._hosts_file).st_mtime_ns
        self.bl.apply()
        mtime2 = os.stat(self._hosts_file).st_mtime_ns
        self.assertEqual(mtime1, mtime2, "second apply with same domains must not rewrite the file")

    def test_apply_logs_sinkhole_action(self):
        _set_always(self.tmpdir, "example.com\n")
        with self.assertLogs("breaktimer.blocklist", level="INFO") as cm:
            self.bl.apply()
        self.assertTrue(any("sinkholed" in m and "example.com" in m for m in cm.output))

    def test_apply_removes_block_when_all_files_emptied(self):
        _set_always(self.tmpdir, "example.com\n")
        self.bl.apply()
        # Clear the always-blocked list
        _set_always(self.tmpdir, "")
        self.bl._last_written = None  # force re-evaluation
        self.bl.apply()
        with open(self._hosts_file) as f:
            content = f.read()
        self.assertNotIn("BEGIN breaktimer-blocklist", content)
        self.assertNotIn("example.com", content)

    def test_apply_logs_removal(self):
        _set_always(self.tmpdir, "example.com\n")
        self.bl.apply()
        _set_always(self.tmpdir, "")
        self.bl._last_written = None
        with self.assertLogs("breaktimer.blocklist", level="INFO") as cm:
            self.bl.apply()
        self.assertTrue(any("removed" in m for m in cm.output))

    def test_apply_no_log_when_nothing_changes(self):
        _set_always(self.tmpdir, "example.com\n")
        self.bl.apply()  # first apply — logs
        with self.assertNoLogs("breaktimer.blocklist", level="INFO"):
            self.bl.apply()

    def test_apply_preserves_existing_hosts_content(self):
        _set_always(self.tmpdir, "blocked.com\n")
        self.bl.apply()
        with open(self._hosts_file) as f:
            content = f.read()
        self.assertIn("127.0.0.1 localhost", content)

    def test_apply_is_atomic_no_tmp_file_left(self):
        _set_always(self.tmpdir, "example.com\n")
        self.bl.apply()
        # The atomic_write creates path+".tmp" then renames it; verify it is gone
        tmp_path = self._hosts_file + ".tmp"
        self.assertFalse(
            os.path.exists(tmp_path),
            "atomic write must leave no temp file behind",
        )

    def test_write_failure_logs_warning_once(self):
        _set_always(self.tmpdir, "example.com\n")
        with mock.patch("status.atomic_write", side_effect=OSError("permission denied")):
            with self.assertLogs("breaktimer.blocklist", level="WARNING") as cm:
                self.bl.apply()
                self.bl.apply()  # second call must stay silent
        self.assertEqual(len([m for m in cm.output if "cannot write" in m]), 1)

    def test_write_failure_does_not_update_last_written(self):
        _set_always(self.tmpdir, "example.com\n")
        with mock.patch("status.atomic_write", side_effect=OSError("denied")):
            self.bl.apply()
        self.assertIsNone(self.bl._last_written)

    def test_write_failure_recovery_logs_again(self):
        """After a write failure, a successful write must still log the action."""
        _set_always(self.tmpdir, "example.com\n")
        with mock.patch("status.atomic_write", side_effect=OSError("denied")):
            self.bl.apply()
        with self.assertLogs("breaktimer.blocklist", level="INFO") as cm:
            self.bl.apply()
        self.assertTrue(any("sinkholed" in m for m in cm.output))

    def test_mtime_recorded_after_write(self):
        """After a successful apply(), _last_written_mtime_ns must be set."""
        _set_always(self.tmpdir, "example.com\n")
        self.bl.apply()
        self.assertIsNotNone(self.bl._last_written_mtime_ns)

    def test_external_modification_triggers_warning_and_restore(self):
        """If /etc/hosts mtime changes between apply() calls, log WARNING and rewrite."""
        _set_always(self.tmpdir, "example.com\n")
        self.bl.apply()
        # Simulate external edit: touch the file to bump its mtime.
        import time as _time
        _time.sleep(0.01)  # ensure mtime changes on filesystems with coarse resolution
        with open(self._hosts_file, "a") as f:
            f.write("# external edit\n")
        with self.assertLogs("breaktimer.blocklist", level="WARNING") as cm:
            self.bl.apply()
        self.assertTrue(any("modified externally" in m for m in cm.output))
        # Block must be restored in the file.
        with open(self._hosts_file) as f:
            content = f.read()
        self.assertIn("0.0.0.0 example.com", content)

    def test_no_warning_when_mtime_unchanged(self):
        """No tamper warning when mtime hasn't changed."""
        _set_always(self.tmpdir, "example.com\n")
        self.bl.apply()
        # Second apply with same domains and unchanged mtime — must be silent.
        with self.assertNoLogs("breaktimer.blocklist", level="WARNING"):
            self.bl.apply()

    def test_mtime_updated_after_tamper_restore(self):
        """After a tamper-triggered rewrite, mtime is refreshed so next tick is quiet."""
        _set_always(self.tmpdir, "example.com\n")
        self.bl.apply()
        import time as _time
        _time.sleep(0.01)
        with open(self._hosts_file, "a") as f:
            f.write("# external edit\n")
        with self.assertLogs("breaktimer.blocklist", level="WARNING"):
            self.bl.apply()
        # Now mtime is fresh; a third apply must not warn again.
        with self.assertNoLogs("breaktimer.blocklist", level="WARNING"):
            self.bl.apply()


# ---------------------------------------------------------------------------
# Tier-based blocking
# ---------------------------------------------------------------------------

class TestTiers(InTempDir):
    """Tiers: always, active (work-session), strict (day-limit)."""

    def setUp(self):
        super().setUp()
        with open(self._hosts_file, "w") as f:
            f.write("127.0.0.1 localhost\n")

    def _hosts_content(self):
        with open(self._hosts_file) as f:
            return f.read()

    def test_always_blocked_regardless_of_is_active_and_strict(self):
        _set_always(self.tmpdir, "gambling.com\n")
        self.bl.apply(is_active=False, strict=False)
        self.assertIn("gambling.com", self._hosts_content())

    def test_active_tier_blocked_when_is_active_true(self):
        _set_active(self.tmpdir, "reddit.com\n")
        self.bl.apply(is_active=True, strict=False)
        self.assertIn("reddit.com", self._hosts_content())

    def test_active_tier_NOT_blocked_when_is_active_false(self):
        _set_active(self.tmpdir, "reddit.com\n")
        self.bl.apply(is_active=False, strict=False)
        self.assertNotIn("reddit.com", self._hosts_content())

    def test_strict_tier_blocked_when_strict_true(self):
        _set_strict(self.tmpdir, "youtube.com\n")
        self.bl.apply(is_active=False, strict=True)
        self.assertIn("youtube.com", self._hosts_content())

    def test_strict_tier_NOT_blocked_when_strict_false(self):
        _set_strict(self.tmpdir, "youtube.com\n")
        self.bl.apply(is_active=False, strict=False)
        self.assertNotIn("youtube.com", self._hosts_content())

    def test_all_three_tiers_combined(self):
        _set_always(self.tmpdir, "gambling.com\n")
        _set_active(self.tmpdir, "reddit.com\n")
        _set_strict(self.tmpdir, "youtube.com\n")
        self.bl.apply(is_active=True, strict=True)
        content = self._hosts_content()
        self.assertIn("gambling.com", content)
        self.assertIn("reddit.com", content)
        self.assertIn("youtube.com", content)

    def test_active_tier_added_when_session_starts(self):
        """Transitioning from idle to active must add work-session domains."""
        _set_active(self.tmpdir, "twitter.com\n")
        # Start idle — active sites not blocked
        self.bl.apply(is_active=False, strict=False)
        self.assertNotIn("twitter.com", self._hosts_content())
        # Now active — active sites blocked
        self.bl._last_written = None
        self.bl.apply(is_active=True, strict=False)
        self.assertIn("twitter.com", self._hosts_content())

    def test_active_tier_removed_on_break(self):
        """Going idle (break) must lift work-session blocks."""
        _set_active(self.tmpdir, "twitter.com\n")
        self.bl.apply(is_active=True, strict=False)
        self.assertIn("twitter.com", self._hosts_content())
        self.bl._last_written = None
        self.bl.apply(is_active=False, strict=False)
        self.assertNotIn("twitter.com", self._hosts_content())

    def test_strict_tier_added_when_daily_limit_reached(self):
        """Crossing the daily limit must apply strict domains."""
        _set_strict(self.tmpdir, "news.ycombinator.com\n")
        self.bl.apply(is_active=False, strict=False)
        self.assertNotIn("news.ycombinator.com", self._hosts_content())
        self.bl._last_written = None
        self.bl.apply(is_active=False, strict=True)
        self.assertIn("news.ycombinator.com", self._hosts_content())

    def test_domain_in_multiple_tiers_appears_once(self):
        """A domain present in both always and active must appear exactly once."""
        _set_always(self.tmpdir, "example.com\n")
        _set_active(self.tmpdir, "example.com\n")
        self.bl.apply(is_active=True, strict=False)
        content = self._hosts_content()
        self.assertEqual(content.count("0.0.0.0 example.com"), 1)

    def test_log_includes_tier_labels(self):
        _set_always(self.tmpdir, "gambling.com\n")
        _set_active(self.tmpdir, "reddit.com\n")
        with self.assertLogs("breaktimer.blocklist", level="INFO") as cm:
            self.bl.apply(is_active=True, strict=False)
        log_text = "\n".join(cm.output)
        self.assertIn("always:", log_text)
        self.assertIn("active:", log_text)

    def test_log_strict_tier_label(self):
        _set_strict(self.tmpdir, "youtube.com\n")
        with self.assertLogs("breaktimer.blocklist", level="INFO") as cm:
            self.bl.apply(is_active=False, strict=True)
        self.assertTrue(any("strict:" in m for m in cm.output))

    def test_no_block_when_all_tiers_empty_and_inactive(self):
        """No /etc/hosts block written when all files absent and tier flags off."""
        self.bl.apply(is_active=False, strict=False)
        with open(self._hosts_file) as f:
            content = f.read()
        self.assertNotIn("BEGIN breaktimer-blocklist", content)

    def test_idempotent_across_consistent_tier_calls(self):
        """Repeated apply() with same tiers must not rewrite the file."""
        _set_always(self.tmpdir, "a.com\n")
        _set_active(self.tmpdir, "b.com\n")
        self.bl.apply(is_active=True, strict=False)
        mtime1 = os.stat(self._hosts_file).st_mtime_ns
        self.bl.apply(is_active=True, strict=False)
        mtime2 = os.stat(self._hosts_file).st_mtime_ns
        self.assertEqual(mtime1, mtime2)


# ---------------------------------------------------------------------------
# Why-it-acted trail invariants
# ---------------------------------------------------------------------------

class TestBlocklistActionTrail(InTempDir):
    """Each mutation to /etc/hosts must be logged — nothing the daemon does to
    the machine is silent."""

    def setUp(self):
        super().setUp()
        with open(self._hosts_file, "w") as f:
            f.write("127.0.0.1 localhost\n")

    def test_every_domain_appears_in_log(self):
        _set_always(self.tmpdir, "alpha.com\nbeta.com\n")
        with self.assertLogs("breaktimer.blocklist", level="INFO") as cm:
            self.bl.apply()
        log_text = "\n".join(cm.output)
        self.assertIn("alpha.com", log_text)
        self.assertIn("beta.com", log_text)

    def test_domain_count_appears_in_log(self):
        _set_always(self.tmpdir, "a.com\nb.com\nc.com\n")
        with self.assertLogs("breaktimer.blocklist", level="INFO") as cm:
            self.bl.apply()
        self.assertTrue(any("3 domain" in m for m in cm.output))

    def test_active_domains_appear_in_log(self):
        _set_active(self.tmpdir, "twitter.com\n")
        with self.assertLogs("breaktimer.blocklist", level="INFO") as cm:
            self.bl.apply(is_active=True)
        self.assertTrue(any("twitter.com" in m for m in cm.output))


class TestScheduleTierApply(InTempDir):
    """apply() integrates the schedule tier with /etc/hosts and the log trail."""

    def setUp(self):
        super().setUp()
        with open(self._hosts_file, "w") as f:
            f.write("127.0.0.1 localhost\n")

    def _hosts_content(self):
        with open(self._hosts_file) as f:
            return f.read()

    def test_schedule_domains_blocked_during_active_window(self):
        _set_schedule(self.tmpdir, "# 09:00-17:00\nschedule.com\n")
        self.bl.apply(_now_min=12 * 60)
        self.assertIn("schedule.com", self._hosts_content())

    def test_schedule_domains_not_blocked_outside_window(self):
        _set_schedule(self.tmpdir, "# 09:00-17:00\nschedule.com\n")
        self.bl.apply(_now_min=20 * 60)
        self.assertNotIn("schedule.com", self._hosts_content())

    def test_schedule_tier_combined_with_always(self):
        _set_always(self.tmpdir, "always.com\n")
        _set_schedule(self.tmpdir, "# 09:00-17:00\nschedule.com\n")
        self.bl.apply(_now_min=12 * 60)
        content = self._hosts_content()
        self.assertIn("always.com", content)
        self.assertIn("schedule.com", content)

    def test_schedule_domain_in_always_appears_once(self):
        _set_always(self.tmpdir, "dup.com\n")
        _set_schedule(self.tmpdir, "# 09:00-17:00\ndup.com\n")
        self.bl.apply(_now_min=12 * 60)
        self.assertEqual(self._hosts_content().count("0.0.0.0 dup.com"), 1)

    def test_schedule_tier_label_in_log(self):
        _set_schedule(self.tmpdir, "# 09:00-17:00\nschedule.com\n")
        with self.assertLogs("breaktimer.blocklist", level="INFO") as cm:
            self.bl.apply(_now_min=12 * 60)
        self.assertTrue(any("schedule:" in m for m in cm.output))

    def test_schedule_domain_appears_in_log(self):
        _set_schedule(self.tmpdir, "# 09:00-17:00\nschedule.com\n")
        with self.assertLogs("breaktimer.blocklist", level="INFO") as cm:
            self.bl.apply(_now_min=12 * 60)
        self.assertTrue(any("schedule.com" in m for m in cm.output))

    def test_schedule_independent_of_is_active(self):
        """Schedule tier fires even when is_active=False."""
        _set_schedule(self.tmpdir, "# 09:00-17:00\nquiet.com\n")
        self.bl.apply(is_active=False, _now_min=12 * 60)
        self.assertIn("quiet.com", self._hosts_content())

    def test_schedule_independent_of_strict(self):
        """Schedule tier fires even when strict=False."""
        _set_schedule(self.tmpdir, "# 09:00-17:00\nquiet.com\n")
        self.bl.apply(strict=False, _now_min=12 * 60)
        self.assertIn("quiet.com", self._hosts_content())

    def test_schedule_block_lifted_when_window_passes(self):
        """Domains are unblocked automatically when the window ends."""
        _set_schedule(self.tmpdir, "# 09:00-17:00\nschedule.com\n")
        self.bl.apply(_now_min=12 * 60)           # inside window
        self.assertIn("schedule.com", self._hosts_content())
        self.bl._last_written = None              # simulate tick at a later time
        self.bl.apply(_now_min=20 * 60)           # outside window
        self.assertNotIn("schedule.com", self._hosts_content())


# ---------------------------------------------------------------------------
# Integration with main.py dispatch
# ---------------------------------------------------------------------------

class _NoopBlocker:
    def apply(self, **kw): pass
    def cleanup(self): pass


class TestBlocklistIntegration(unittest.TestCase):
    """apply() is dispatched via the effects worker with timer state baked in.

    Blocking (_apply_blocking) runs every tick; hardware adjustments
    (_apply_hardware_adjustments) run every 10 s — the two paths are separate.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def test_blocking_dispatched_every_tick(self):
        """_apply_blocking must dispatch blocklist + app_blocking on every call."""
        import main
        from main import TimerState, TimerLoop, TimerConfig
        import time

        dispatched = []

        state = TimerState(remaining_time=3600)
        monitor = mock.Mock()
        monitor.is_healthy.return_value = True
        monitor.get_last_activity_time.return_value = time.monotonic()

        loop = TimerLoop(state, 0, monitor, TimerConfig(3600, 1200, 8 * 3600, 10 * 3600),
                         dispatch=dispatched.append,
                         blocklist=_NoopBlocker(), app_blocker=_NoopBlocker(),
                         firewall=_NoopBlocker())

        loop._apply_blocking()

        # Three effects: blocklist + app_blocking + firewall (no hardware effects here)
        self.assertEqual(len(dispatched), 3,
                         "expected 3 dispatched effects: blocklist, app_blocking, firewall")

    def test_blocklist_effect_calls_apply_with_timer_state(self):
        """The dispatched blocklist lambda must invoke bl.apply with is_active and strict."""
        import main
        from main import TimerState, TimerLoop, TimerConfig
        import time

        dispatched = []

        state = TimerState(remaining_time=3600)
        state.is_active = True
        monitor = mock.Mock()
        monitor.is_healthy.return_value = True
        monitor.get_last_activity_time.return_value = time.monotonic()

        bl = blocklist.Blocklist(self._tmp.name)
        loop = TimerLoop(state, 0, monitor, TimerConfig(3600, 1200, 8 * 3600, 10 * 3600),
                         dispatch=dispatched.append,
                         blocklist=bl, app_blocker=_NoopBlocker(),
                         firewall=_NoopBlocker())

        loop._apply_blocking()

        # Call the blocklist effect (first of three) and verify timer state forwarded
        blocklist_effect = dispatched[0]
        with mock.patch.object(bl, "apply") as mock_apply:
            blocklist_effect()
        mock_apply.assert_called_once_with(is_active=True, strict=False)

    def test_strict_true_when_refill_exhausted(self):
        """strict=True must be passed to apply() when daily limit is reached."""
        import main
        from main import TimerState, TimerLoop, TimerConfig
        import time

        dispatched = []

        today = __import__("status").today_str()
        state = TimerState(remaining_time=100)
        state.daily_work_totals[today] = 10 * 3600  # at the daily limit → refill=0

        monitor = mock.Mock()
        monitor.is_healthy.return_value = True
        monitor.get_last_activity_time.return_value = time.monotonic()

        bl = blocklist.Blocklist(self._tmp.name)
        loop = TimerLoop(state, 0, monitor, TimerConfig(3600, 1200, 8 * 3600, 10 * 3600),
                         dispatch=dispatched.append,
                         blocklist=bl, app_blocker=_NoopBlocker(),
                         firewall=_NoopBlocker())

        loop._apply_blocking()

        blocklist_effect = dispatched[0]
        with mock.patch.object(bl, "apply") as mock_apply:
            blocklist_effect()
        _, kwargs = mock_apply.call_args
        self.assertTrue(kwargs["strict"], "strict must be True when refill_multiplier=0")


# ---------------------------------------------------------------------------
# DoH sinkholing
# ---------------------------------------------------------------------------

class TestDoHSinkholing(InTempDir):
    """apply() automatically sinkholed DoH provider hostnames when any domains are blocked."""

    def setUp(self):
        super().setUp()
        with open(self._hosts_file, "w") as f:
            f.write("127.0.0.1 localhost\n")
        # Re-enable DoH sinkholing for this test class (InTempDir.setUp disabled it).
        self.bl.doh_domains = frozenset(["dns.google", "cloudflare-dns.com"])

    def _hosts_content(self):
        with open(self._hosts_file) as f:
            return f.read()

    def test_doh_domains_added_when_any_domain_blocked(self):
        _set_always(self.tmpdir, "reddit.com\n")
        self.bl.apply()
        content = self._hosts_content()
        self.assertIn("dns.google", content)
        self.assertIn("cloudflare-dns.com", content)

    def test_doh_domains_not_added_when_no_domains_blocked(self):
        # All tier files empty/absent — no blocks, no DoH sinkholing either.
        self.bl.apply()
        content = self._hosts_content()
        self.assertNotIn("dns.google", content)
        self.assertNotIn("cloudflare-dns.com", content)

    def test_doh_tier_label_in_log(self):
        _set_always(self.tmpdir, "reddit.com\n")
        with self.assertLogs("breaktimer.blocklist", level="INFO") as cm:
            self.bl.apply()
        self.assertTrue(any("doh:" in m for m in cm.output))

    def test_doh_not_added_when_doh_set_empty(self):
        """Setting doh_domains=frozenset() disables auto-sinkholing."""
        self.bl.doh_domains = frozenset()
        _set_always(self.tmpdir, "reddit.com\n")
        self.bl.apply()
        content = self._hosts_content()
        self.assertNotIn("dns.google", content)

    def test_user_domain_in_doh_set_appears_once(self):
        """If a user adds a DoH provider to their own blocklist, no duplicate entry."""
        self.bl.doh_domains = frozenset(["dns.google"])
        _set_always(self.tmpdir, "dns.google\n")
        self.bl.apply()
        self.assertEqual(self._hosts_content().count("0.0.0.0 dns.google"), 1)


# ---------------------------------------------------------------------------
# read_schedule_windows
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
