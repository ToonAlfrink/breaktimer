"""Tests for the domain blocklist: /etc/hosts management.

The blocklist module maintains a marked-off sinkhole block in /etc/hosts,
rewriting it atomically and logging every mutation via the why-it-acted trail.

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

def _set_blocklist(tmpdir, content):
    """Write a blocklist.txt into tmpdir and point the module at it."""
    path = os.path.join(tmpdir, "blocklist.txt")
    with open(path, "w") as f:
        f.write(content)
    blocklist.blocklist_file = path
    return path


def _clear_blocklist(tmpdir):
    """Point the module at a non-existent blocklist.txt."""
    blocklist.blocklist_file = os.path.join(tmpdir, "blocklist.txt")


class InTempDir(unittest.TestCase):
    """Each test gets an isolated tmpdir and a clean module state."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # Reset module-level state between tests.
        blocklist.blocklist_file = None
        blocklist._last_written = None
        blocklist._write_failed = False

    def tearDown(self):
        self._tmp.cleanup()

    @property
    def tmpdir(self):
        return self._tmp.name


# ---------------------------------------------------------------------------
# read_domains
# ---------------------------------------------------------------------------

class TestReadDomains(InTempDir):
    def test_empty_file_returns_empty(self):
        _set_blocklist(self.tmpdir, "")
        self.assertEqual(blocklist.read_domains(), [])

    def test_missing_file_returns_empty(self):
        _clear_blocklist(self.tmpdir)
        self.assertEqual(blocklist.read_domains(), [])

    def test_returns_sorted_lowercase_domains(self):
        _set_blocklist(self.tmpdir, "Reddit.com\n9gag.com\n")
        self.assertEqual(blocklist.read_domains(), ["9gag.com", "reddit.com"])

    def test_strips_blank_lines_and_comments(self):
        _set_blocklist(self.tmpdir, "# a comment\n\nexample.com\n")
        self.assertEqual(blocklist.read_domains(), ["example.com"])

    def test_deduplicates(self):
        _set_blocklist(self.tmpdir, "example.com\nexample.com\n")
        self.assertEqual(blocklist.read_domains(), ["example.com"])

    def test_no_blocklist_file_configured_returns_empty(self):
        # blocklist_file is None — module not yet initialised
        blocklist.blocklist_file = None
        self.assertEqual(blocklist.read_domains(), [])


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
        # Should NOT add www.www.example.com
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
        # Both 'example.com' and 'www.example.com' supplied
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

    def _apply(self):
        blocklist.apply()

    def test_apply_with_domains_writes_sinkhole_block(self):
        _set_blocklist(self.tmpdir, "example.com\n")
        self._apply()
        with open(self._hosts) as f:
            content = f.read()
        self.assertIn("0.0.0.0 example.com", content)
        self.assertIn("# BEGIN breaktimer-blocklist", content)

    def test_apply_is_idempotent(self):
        _set_blocklist(self.tmpdir, "example.com\n")
        self._apply()
        mtime1 = os.stat(self._hosts).st_mtime_ns
        # Reset _last_written so second apply can potentially re-write (but shouldn't need to).
        # Actually _last_written tracks the block string, not the file — second call no-ops.
        self._apply()
        mtime2 = os.stat(self._hosts).st_mtime_ns
        self.assertEqual(mtime1, mtime2, "second apply with same domains must not rewrite the file")

    def test_apply_logs_sinkhole_action(self):
        _set_blocklist(self.tmpdir, "example.com\n")
        with self.assertLogs("breaktimer.blocklist", level="INFO") as cm:
            self._apply()
        self.assertTrue(any("sinkholed" in m and "example.com" in m for m in cm.output))

    def test_apply_removes_block_when_blocklist_emptied(self):
        _set_blocklist(self.tmpdir, "example.com\n")
        self._apply()
        # Now clear the blocklist
        _set_blocklist(self.tmpdir, "")
        blocklist._last_written = None  # force re-evaluation
        self._apply()
        with open(self._hosts) as f:
            content = f.read()
        self.assertNotIn("BEGIN breaktimer-blocklist", content)
        self.assertNotIn("example.com", content)

    def test_apply_logs_removal(self):
        _set_blocklist(self.tmpdir, "example.com\n")
        self._apply()
        _set_blocklist(self.tmpdir, "")
        blocklist._last_written = None
        with self.assertLogs("breaktimer.blocklist", level="INFO") as cm:
            self._apply()
        self.assertTrue(any("removed" in m for m in cm.output))

    def test_apply_no_log_when_nothing_changes(self):
        _set_blocklist(self.tmpdir, "example.com\n")
        self._apply()  # first apply — logs
        # second apply — must be silent
        with self.assertNoLogs("breaktimer.blocklist", level="INFO"):
            self._apply()

    def test_apply_preserves_existing_hosts_content(self):
        _set_blocklist(self.tmpdir, "blocked.com\n")
        self._apply()
        with open(self._hosts) as f:
            content = f.read()
        self.assertIn("127.0.0.1 localhost", content)

    def test_apply_is_atomic_no_tmp_file_left(self):
        _set_blocklist(self.tmpdir, "example.com\n")
        self._apply()
        files = os.listdir(self.tmpdir)
        self.assertFalse(
            any(f.endswith(".breaktimer-tmp") for f in files),
            "atomic write must leave no temp file behind",
        )

    def test_write_failure_logs_warning_once(self):
        _set_blocklist(self.tmpdir, "example.com\n")
        with mock.patch.object(blocklist, "_write_hosts", return_value=OSError("permission denied")):
            with self.assertLogs("breaktimer.blocklist", level="WARNING") as cm:
                blocklist.apply()
                blocklist.apply()  # second call must stay silent
        self.assertEqual(len([m for m in cm.output if "cannot write" in m]), 1)

    def test_write_failure_does_not_update_last_written(self):
        _set_blocklist(self.tmpdir, "example.com\n")
        with mock.patch.object(blocklist, "_write_hosts", return_value=OSError("denied")):
            blocklist.apply()
        self.assertIsNone(blocklist._last_written)

    def test_write_failure_recovery_logs_again(self):
        """After a write failure, a successful write must still log the action."""
        _set_blocklist(self.tmpdir, "example.com\n")
        # First call fails
        with mock.patch.object(blocklist, "_write_hosts", return_value=OSError("denied")):
            blocklist.apply()
        # Second call succeeds — must log the sinkhole action
        with self.assertLogs("breaktimer.blocklist", level="INFO") as cm:
            self._apply()
        self.assertTrue(any("sinkholed" in m for m in cm.output))


# ---------------------------------------------------------------------------
# Why-it-acted trail invariants (mirrors TestActionTrail in test_main.py)
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
        _set_blocklist(self.tmpdir, "alpha.com\nbeta.com\n")
        with self.assertLogs("breaktimer.blocklist", level="INFO") as cm:
            blocklist.apply()
        log_text = "\n".join(cm.output)
        self.assertIn("alpha.com", log_text)
        self.assertIn("beta.com", log_text)

    def test_domain_count_appears_in_log(self):
        _set_blocklist(self.tmpdir, "a.com\nb.com\nc.com\n")
        with self.assertLogs("breaktimer.blocklist", level="INFO") as cm:
            blocklist.apply()
        self.assertTrue(any("3 domain" in m for m in cm.output))


# ---------------------------------------------------------------------------
# Integration with main.py dispatch — blocklist.apply goes through effects worker
# ---------------------------------------------------------------------------

class TestBlocklistIntegration(unittest.TestCase):
    """apply() is dispatched via the effects worker like brightness and sensitivity."""

    def test_apply_dispatched_during_adjustments(self):
        """_apply_adjustments must dispatch blocklist.apply alongside the other effects."""
        import main
        from main import TimerState, TimerLoop
        import time

        seen = []

        state = TimerState(remaining_time=3600)
        monitor = mock.Mock()
        monitor.is_healthy.return_value = True
        monitor.get_last_activity_time.return_value = time.monotonic()

        loop = TimerLoop(state, 0, monitor, 3600, 1200, 8 * 3600, 10 * 3600,
                         dispatch=seen.append)
        loop.last_adjustment_time = time.monotonic() - 999

        loop._apply_adjustments(1.0, time.monotonic())

        # Three effects: brightness, sensitivity, blocklist
        self.assertEqual(len(seen), 3,
                         "expected 3 dispatched effects: brightness, sensitivity, blocklist.apply")
        # The last one should be blocklist.apply
        self.assertIs(seen[2], blocklist.apply)


if __name__ == "__main__":
    unittest.main()
