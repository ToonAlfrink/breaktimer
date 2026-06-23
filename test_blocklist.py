"""Tests for the domain blocklist: /etc/hosts management.

The blocklist module maintains a marked-off sinkhole block in /etc/hosts,
rewriting it atomically and logging every mutation via the why-it-acted trail.
Three tiers: always-blocked, work-session-blocked (is_active), and
strict/day-limit-blocked (strict).

Run: python3 -m unittest -q
"""
import logging
import os
import tempfile
import unittest
from unittest import mock

import blocklist

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
    blocklist.blocklist_file = path
    return path


def _set_active(tmpdir, content):
    path = os.path.join(tmpdir, "blocklist-active.txt")
    _write_file(path, content)
    blocklist.blocklist_active_file = path
    return path


def _set_strict(tmpdir, content):
    path = os.path.join(tmpdir, "blocklist-strict.txt")
    _write_file(path, content)
    blocklist.blocklist_strict_file = path
    return path


class InTempDir(unittest.TestCase):
    """Each test gets an isolated tmpdir and a clean module state."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # Reset module-level state between tests.
        blocklist.blocklist_file = None
        blocklist.blocklist_active_file = None
        blocklist.blocklist_strict_file = None
        blocklist._last_written = None
        blocklist._write_failed = False

    def tearDown(self):
        self._tmp.cleanup()

    @property
    def tmpdir(self):
        return self._tmp.name


# ---------------------------------------------------------------------------
# _read_file_domains / read_domains / read_domains_active / read_domains_strict
# ---------------------------------------------------------------------------

class TestReadDomains(InTempDir):
    def test_empty_file_returns_empty(self):
        _set_always(self.tmpdir, "")
        self.assertEqual(blocklist.read_domains(), [])

    def test_missing_file_returns_empty(self):
        blocklist.blocklist_file = os.path.join(self.tmpdir, "missing.txt")
        self.assertEqual(blocklist.read_domains(), [])

    def test_returns_sorted_lowercase_domains(self):
        _set_always(self.tmpdir, "Reddit.com\n9gag.com\n")
        self.assertEqual(blocklist.read_domains(), ["9gag.com", "reddit.com"])

    def test_strips_blank_lines_and_comments(self):
        _set_always(self.tmpdir, "# a comment\n\nexample.com\n")
        self.assertEqual(blocklist.read_domains(), ["example.com"])

    def test_deduplicates(self):
        _set_always(self.tmpdir, "example.com\nexample.com\n")
        self.assertEqual(blocklist.read_domains(), ["example.com"])

    def test_no_file_configured_returns_empty(self):
        blocklist.blocklist_file = None
        self.assertEqual(blocklist.read_domains(), [])

    def test_read_domains_active_empty_when_file_missing(self):
        self.assertEqual(blocklist.read_domains_active(), [])

    def test_read_domains_active_returns_domains(self):
        _set_active(self.tmpdir, "twitter.com\n")
        self.assertEqual(blocklist.read_domains_active(), ["twitter.com"])

    def test_read_domains_strict_empty_when_file_missing(self):
        self.assertEqual(blocklist.read_domains_strict(), [])

    def test_read_domains_strict_returns_domains(self):
        _set_strict(self.tmpdir, "news.ycombinator.com\n")
        self.assertEqual(blocklist.read_domains_strict(), ["news.ycombinator.com"])


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
# apply() — end-to-end with mocked /etc/hosts
# ---------------------------------------------------------------------------

class TestApply(InTempDir):
    """apply() writes /etc/hosts only when content changes, logs each mutation."""

    def setUp(self):
        super().setUp()
        # Redirect HOSTS_PATH to a temp file so we never touch the real one.
        self._hosts = os.path.join(self.tmpdir, "hosts")
        with open(self._hosts, "w") as f:
            f.write("127.0.0.1 localhost\n")
        self._hosts_patch = mock.patch.object(blocklist, "HOSTS_PATH", self._hosts)
        self._hosts_patch.start()

    def tearDown(self):
        self._hosts_patch.stop()
        super().tearDown()

    def test_apply_with_domains_writes_sinkhole_block(self):
        _set_always(self.tmpdir, "example.com\n")
        blocklist.apply()
        with open(self._hosts) as f:
            content = f.read()
        self.assertIn("0.0.0.0 example.com", content)
        self.assertIn("# BEGIN breaktimer-blocklist", content)

    def test_apply_is_idempotent(self):
        _set_always(self.tmpdir, "example.com\n")
        blocklist.apply()
        mtime1 = os.stat(self._hosts).st_mtime_ns
        blocklist.apply()
        mtime2 = os.stat(self._hosts).st_mtime_ns
        self.assertEqual(mtime1, mtime2, "second apply with same domains must not rewrite the file")

    def test_apply_logs_sinkhole_action(self):
        _set_always(self.tmpdir, "example.com\n")
        with self.assertLogs("breaktimer.blocklist", level="INFO") as cm:
            blocklist.apply()
        self.assertTrue(any("sinkholed" in m and "example.com" in m for m in cm.output))

    def test_apply_removes_block_when_all_files_emptied(self):
        _set_always(self.tmpdir, "example.com\n")
        blocklist.apply()
        # Clear the always-blocked list
        _set_always(self.tmpdir, "")
        blocklist._last_written = None  # force re-evaluation
        blocklist.apply()
        with open(self._hosts) as f:
            content = f.read()
        self.assertNotIn("BEGIN breaktimer-blocklist", content)
        self.assertNotIn("example.com", content)

    def test_apply_logs_removal(self):
        _set_always(self.tmpdir, "example.com\n")
        blocklist.apply()
        _set_always(self.tmpdir, "")
        blocklist._last_written = None
        with self.assertLogs("breaktimer.blocklist", level="INFO") as cm:
            blocklist.apply()
        self.assertTrue(any("removed" in m for m in cm.output))

    def test_apply_no_log_when_nothing_changes(self):
        _set_always(self.tmpdir, "example.com\n")
        blocklist.apply()  # first apply — logs
        with self.assertNoLogs("breaktimer.blocklist", level="INFO"):
            blocklist.apply()

    def test_apply_preserves_existing_hosts_content(self):
        _set_always(self.tmpdir, "blocked.com\n")
        blocklist.apply()
        with open(self._hosts) as f:
            content = f.read()
        self.assertIn("127.0.0.1 localhost", content)

    def test_apply_is_atomic_no_tmp_file_left(self):
        _set_always(self.tmpdir, "example.com\n")
        blocklist.apply()
        files = os.listdir(self.tmpdir)
        self.assertFalse(
            any(f.endswith(".breaktimer-tmp") for f in files),
            "atomic write must leave no temp file behind",
        )

    def test_write_failure_logs_warning_once(self):
        _set_always(self.tmpdir, "example.com\n")
        with mock.patch.object(blocklist, "_write_hosts", return_value=OSError("permission denied")):
            with self.assertLogs("breaktimer.blocklist", level="WARNING") as cm:
                blocklist.apply()
                blocklist.apply()  # second call must stay silent
        self.assertEqual(len([m for m in cm.output if "cannot write" in m]), 1)

    def test_write_failure_does_not_update_last_written(self):
        _set_always(self.tmpdir, "example.com\n")
        with mock.patch.object(blocklist, "_write_hosts", return_value=OSError("denied")):
            blocklist.apply()
        self.assertIsNone(blocklist._last_written)

    def test_write_failure_recovery_logs_again(self):
        """After a write failure, a successful write must still log the action."""
        _set_always(self.tmpdir, "example.com\n")
        with mock.patch.object(blocklist, "_write_hosts", return_value=OSError("denied")):
            blocklist.apply()
        with self.assertLogs("breaktimer.blocklist", level="INFO") as cm:
            blocklist.apply()
        self.assertTrue(any("sinkholed" in m for m in cm.output))


# ---------------------------------------------------------------------------
# Tier-based blocking
# ---------------------------------------------------------------------------

class TestTiers(InTempDir):
    """Tiers: always, active (work-session), strict (day-limit)."""

    def setUp(self):
        super().setUp()
        self._hosts = os.path.join(self.tmpdir, "hosts")
        with open(self._hosts, "w") as f:
            f.write("127.0.0.1 localhost\n")
        self._hosts_patch = mock.patch.object(blocklist, "HOSTS_PATH", self._hosts)
        self._hosts_patch.start()

    def tearDown(self):
        self._hosts_patch.stop()
        super().tearDown()

    def _hosts_content(self):
        with open(self._hosts) as f:
            return f.read()

    def test_always_blocked_regardless_of_is_active_and_strict(self):
        _set_always(self.tmpdir, "gambling.com\n")
        blocklist.apply(is_active=False, strict=False)
        self.assertIn("gambling.com", self._hosts_content())

    def test_active_tier_blocked_when_is_active_true(self):
        _set_active(self.tmpdir, "reddit.com\n")
        blocklist.apply(is_active=True, strict=False)
        self.assertIn("reddit.com", self._hosts_content())

    def test_active_tier_NOT_blocked_when_is_active_false(self):
        _set_active(self.tmpdir, "reddit.com\n")
        blocklist.apply(is_active=False, strict=False)
        self.assertNotIn("reddit.com", self._hosts_content())

    def test_strict_tier_blocked_when_strict_true(self):
        _set_strict(self.tmpdir, "youtube.com\n")
        blocklist.apply(is_active=False, strict=True)
        self.assertIn("youtube.com", self._hosts_content())

    def test_strict_tier_NOT_blocked_when_strict_false(self):
        _set_strict(self.tmpdir, "youtube.com\n")
        blocklist.apply(is_active=False, strict=False)
        self.assertNotIn("youtube.com", self._hosts_content())

    def test_all_three_tiers_combined(self):
        _set_always(self.tmpdir, "gambling.com\n")
        _set_active(self.tmpdir, "reddit.com\n")
        _set_strict(self.tmpdir, "youtube.com\n")
        blocklist.apply(is_active=True, strict=True)
        content = self._hosts_content()
        self.assertIn("gambling.com", content)
        self.assertIn("reddit.com", content)
        self.assertIn("youtube.com", content)

    def test_active_tier_added_when_session_starts(self):
        """Transitioning from idle to active must add work-session domains."""
        _set_active(self.tmpdir, "twitter.com\n")
        # Start idle — active sites not blocked
        blocklist.apply(is_active=False, strict=False)
        self.assertNotIn("twitter.com", self._hosts_content())
        # Now active — active sites blocked
        blocklist._last_written = None
        blocklist.apply(is_active=True, strict=False)
        self.assertIn("twitter.com", self._hosts_content())

    def test_active_tier_removed_on_break(self):
        """Going idle (break) must lift work-session blocks."""
        _set_active(self.tmpdir, "twitter.com\n")
        blocklist.apply(is_active=True, strict=False)
        self.assertIn("twitter.com", self._hosts_content())
        blocklist._last_written = None
        blocklist.apply(is_active=False, strict=False)
        self.assertNotIn("twitter.com", self._hosts_content())

    def test_strict_tier_added_when_daily_limit_reached(self):
        """Crossing the daily limit must apply strict domains."""
        _set_strict(self.tmpdir, "news.ycombinator.com\n")
        blocklist.apply(is_active=False, strict=False)
        self.assertNotIn("news.ycombinator.com", self._hosts_content())
        blocklist._last_written = None
        blocklist.apply(is_active=False, strict=True)
        self.assertIn("news.ycombinator.com", self._hosts_content())

    def test_domain_in_multiple_tiers_appears_once(self):
        """A domain present in both always and active must appear exactly once."""
        _set_always(self.tmpdir, "example.com\n")
        _set_active(self.tmpdir, "example.com\n")
        blocklist.apply(is_active=True, strict=False)
        content = self._hosts_content()
        self.assertEqual(content.count("0.0.0.0 example.com"), 1)

    def test_log_includes_tier_labels(self):
        _set_always(self.tmpdir, "gambling.com\n")
        _set_active(self.tmpdir, "reddit.com\n")
        with self.assertLogs("breaktimer.blocklist", level="INFO") as cm:
            blocklist.apply(is_active=True, strict=False)
        log_text = "\n".join(cm.output)
        self.assertIn("always:", log_text)
        self.assertIn("active:", log_text)

    def test_log_strict_tier_label(self):
        _set_strict(self.tmpdir, "youtube.com\n")
        with self.assertLogs("breaktimer.blocklist", level="INFO") as cm:
            blocklist.apply(is_active=False, strict=True)
        self.assertTrue(any("strict:" in m for m in cm.output))

    def test_no_block_when_all_tiers_empty_and_inactive(self):
        """No /etc/hosts block written when all files absent and tier flags off."""
        blocklist.apply(is_active=False, strict=False)
        content = self._hosts_content()
        self.assertNotIn("BEGIN breaktimer-blocklist", content)

    def test_idempotent_across_consistent_tier_calls(self):
        """Repeated apply() with same tiers must not rewrite the file."""
        _set_always(self.tmpdir, "a.com\n")
        _set_active(self.tmpdir, "b.com\n")
        blocklist.apply(is_active=True, strict=False)
        mtime1 = os.stat(self._hosts).st_mtime_ns
        blocklist.apply(is_active=True, strict=False)
        mtime2 = os.stat(self._hosts).st_mtime_ns
        self.assertEqual(mtime1, mtime2)


# ---------------------------------------------------------------------------
# Why-it-acted trail invariants
# ---------------------------------------------------------------------------

class TestBlocklistActionTrail(InTempDir):
    """Each mutation to /etc/hosts must be logged — nothing the daemon does to
    the machine is silent."""

    def setUp(self):
        super().setUp()
        self._hosts = os.path.join(self.tmpdir, "hosts")
        with open(self._hosts, "w") as f:
            f.write("127.0.0.1 localhost\n")
        self._hosts_patch = mock.patch.object(blocklist, "HOSTS_PATH", self._hosts)
        self._hosts_patch.start()

    def tearDown(self):
        self._hosts_patch.stop()
        super().tearDown()

    def test_every_domain_appears_in_log(self):
        _set_always(self.tmpdir, "alpha.com\nbeta.com\n")
        with self.assertLogs("breaktimer.blocklist", level="INFO") as cm:
            blocklist.apply()
        log_text = "\n".join(cm.output)
        self.assertIn("alpha.com", log_text)
        self.assertIn("beta.com", log_text)

    def test_domain_count_appears_in_log(self):
        _set_always(self.tmpdir, "a.com\nb.com\nc.com\n")
        with self.assertLogs("breaktimer.blocklist", level="INFO") as cm:
            blocklist.apply()
        self.assertTrue(any("3 domain" in m for m in cm.output))

    def test_active_domains_appear_in_log(self):
        _set_active(self.tmpdir, "twitter.com\n")
        with self.assertLogs("breaktimer.blocklist", level="INFO") as cm:
            blocklist.apply(is_active=True)
        self.assertTrue(any("twitter.com" in m for m in cm.output))


# ---------------------------------------------------------------------------
# Integration with main.py dispatch
# ---------------------------------------------------------------------------

class TestBlocklistIntegration(unittest.TestCase):
    """apply() is dispatched via the effects worker with timer state baked in."""

    def test_apply_dispatched_during_adjustments(self):
        """_apply_adjustments must dispatch a blocklist effect with timer state."""
        import main
        from main import TimerState, TimerLoop
        import time

        dispatched = []

        state = TimerState(remaining_time=3600)
        monitor = mock.Mock()
        monitor.is_healthy.return_value = True
        monitor.get_last_activity_time.return_value = time.monotonic()

        loop = TimerLoop(state, 0, monitor, 3600, 1200, 8 * 3600, 10 * 3600,
                         dispatch=dispatched.append)
        loop.last_adjustment_time = time.monotonic() - 999

        loop._apply_adjustments(1.0, time.monotonic())

        # Three effects: brightness, sensitivity, blocklist
        self.assertEqual(len(dispatched), 3,
                         "expected 3 dispatched effects: brightness, sensitivity, blocklist")

    def test_blocklist_effect_calls_apply_with_timer_state(self):
        """The dispatched blocklist lambda must invoke blocklist.apply with is_active and strict."""
        import main
        from main import TimerState, TimerLoop
        import time

        dispatched = []

        state = TimerState(remaining_time=3600)
        state.is_active = True
        monitor = mock.Mock()
        monitor.is_healthy.return_value = True
        monitor.get_last_activity_time.return_value = time.monotonic()

        loop = TimerLoop(state, 0, monitor, 3600, 1200, 8 * 3600, 10 * 3600,
                         dispatch=dispatched.append)
        loop.last_adjustment_time = time.monotonic() - 999

        loop._apply_adjustments(1.0, time.monotonic())

        # Call the blocklist effect and verify it passes timer state to apply()
        blocklist_effect = dispatched[2]
        with mock.patch.object(blocklist, "apply") as mock_apply:
            blocklist_effect()
        mock_apply.assert_called_once_with(is_active=True, strict=False)

    def test_strict_true_when_refill_exhausted(self):
        """strict=True must be passed to apply() when daily limit is reached."""
        import main
        from main import TimerState, TimerLoop
        import time

        dispatched = []

        today = __import__("status").today_str()
        state = TimerState(remaining_time=100)
        state.daily_work_totals[today] = 10 * 3600  # at the daily limit → refill=0

        monitor = mock.Mock()
        monitor.is_healthy.return_value = True
        monitor.get_last_activity_time.return_value = time.monotonic()

        loop = TimerLoop(state, 0, monitor, 3600, 1200, 8 * 3600, 10 * 3600,
                         dispatch=dispatched.append)
        loop.last_adjustment_time = time.monotonic() - 999

        loop._apply_adjustments(0.5, time.monotonic())

        blocklist_effect = dispatched[2]
        with mock.patch.object(blocklist, "apply") as mock_apply:
            blocklist_effect()
        _, kwargs = mock_apply.call_args
        self.assertTrue(kwargs["strict"], "strict must be True when refill_multiplier=0")


if __name__ == "__main__":
    unittest.main()
