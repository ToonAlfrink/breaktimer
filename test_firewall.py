"""Tests for firewall.py: nftables DoH-IP-blocking rule management.

Covers: install on first apply, no-op when table exists, tamper detection and
restore, graceful degradation (nft absent / permission denied), single-warning
suppression, cleanup, and the why-it-acted log trail.

Run: python3 -m unittest -q
"""
import logging
import subprocess
import unittest
from unittest import mock

import firewall

logging.getLogger("breaktimer").addHandler(logging.NullHandler())


def _run_ok(*args, **kwargs):
    return mock.MagicMock(returncode=0, stdout="", stderr="")


def _run_fail(*args, **kwargs):
    cmd = args[0] if args else kwargs.get("args", ["nft"])
    # Only raise on the -f - install call; let delete silently pass.
    if "-f" in cmd:
        raise subprocess.CalledProcessError(1, cmd, stderr="Operation not permitted")
    return mock.MagicMock(returncode=1, stdout="", stderr="")


def _run_missing(*args, **kwargs):
    raise FileNotFoundError("nft: not found")


class _FirewallTest(unittest.TestCase):
    """Create a fresh Firewall instance before each test."""

    def setUp(self):
        self.fw = firewall.Firewall(
            doh_ips=firewall.DOH_SERVER_IPS,
            doh_ips6=firewall.DOH_SERVER_IPS6,
        )


# ---------------------------------------------------------------------------
# apply() — top-level logic
# ---------------------------------------------------------------------------

class TestFirewallApply(_FirewallTest):
    def test_installs_rules_on_first_apply(self):
        with mock.patch("firewall._install_rules") as m_install, \
             mock.patch("firewall._table_exists", return_value=True):
            self.fw.apply()
        m_install.assert_called_once()
        self.assertTrue(self.fw._rules_installed)

    def test_noop_when_table_exists(self):
        self.fw._rules_installed = True
        with mock.patch("firewall._table_exists", return_value=True) as m_check, \
             mock.patch("firewall._install_rules") as m_install:
            self.fw.apply()
        m_install.assert_not_called()
        m_check.assert_called_once()

    def test_restores_rules_when_table_deleted(self):
        self.fw._rules_installed = True
        with mock.patch("firewall._table_exists", return_value=False), \
             mock.patch("firewall._install_rules") as m_install:
            self.fw.apply()
        m_install.assert_called_once()
        self.assertTrue(self.fw._rules_installed)

    def test_noop_when_apply_failed(self):
        self.fw._apply_failed = True
        with mock.patch("firewall._install_rules") as m_install, \
             mock.patch("firewall._table_exists") as m_check:
            self.fw.apply()
        m_install.assert_not_called()
        m_check.assert_not_called()

    def test_noop_when_doh_ips_empty(self):
        fw = firewall.Firewall(doh_ips=frozenset())
        with mock.patch("firewall._install_rules") as m_install:
            fw.apply()
        m_install.assert_not_called()

    def test_graceful_when_nft_missing(self):
        with mock.patch("firewall._install_rules",
                        side_effect=FileNotFoundError("nft not found")):
            self.fw.apply()  # must not raise
        self.assertFalse(self.fw._rules_installed)
        self.assertTrue(self.fw._apply_failed)

    def test_graceful_when_permission_denied(self):
        err = subprocess.CalledProcessError(1, ["nft"], stderr="Operation not permitted")
        with mock.patch("firewall._install_rules", side_effect=err):
            self.fw.apply()  # must not raise
        self.assertFalse(self.fw._rules_installed)
        self.assertTrue(self.fw._apply_failed)

    def test_warns_once_on_repeated_nft_missing(self):
        with mock.patch("firewall._install_rules",
                        side_effect=FileNotFoundError("nft not found")), \
             self.assertLogs("breaktimer.firewall", level="WARNING") as cm:
            self.fw.apply()
            self.fw.apply()
        warns = [m for m in cm.output if "WARNING" in m]
        self.assertEqual(len(warns), 1, "expected exactly one warning, got: %s" % warns)

    def test_warns_once_on_repeated_permission_denied(self):
        err = subprocess.CalledProcessError(1, ["nft"], stderr="denied")
        with mock.patch("firewall._install_rules", side_effect=err), \
             self.assertLogs("breaktimer.firewall", level="WARNING") as cm:
            self.fw.apply()
            self.fw.apply()
        warns = [m for m in cm.output if "WARNING" in m]
        self.assertEqual(len(warns), 1)

    def test_is_active_strict_accepted(self):
        """apply() is interface-compatible with blocklist.apply()."""
        with mock.patch("firewall._install_rules"), \
             mock.patch("firewall._table_exists", return_value=True):
            self.fw.apply(is_active=True, strict=True)  # must not raise
        self.assertTrue(self.fw._rules_installed)

    def test_log_trail_on_install(self):
        with mock.patch("firewall._install_rules"), \
             mock.patch("firewall._table_exists", return_value=True), \
             self.assertLogs("breaktimer.firewall", level="INFO") as cm:
            self.fw.apply()
        self.assertTrue(
            any("installed" in m and "DoH" in m for m in cm.output),
            cm.output,
        )

    def test_log_trail_on_tamper_detection(self):
        self.fw._rules_installed = True
        with mock.patch("firewall._table_exists", return_value=False), \
             mock.patch("firewall._install_rules"), \
             self.assertLogs("breaktimer.firewall", level="WARNING") as cm:
            self.fw.apply()
        self.assertTrue(
            any("deleted externally" in m for m in cm.output),
            cm.output,
        )

    def test_log_trail_nft_missing(self):
        with mock.patch("firewall._install_rules",
                        side_effect=FileNotFoundError()), \
             self.assertLogs("breaktimer.firewall", level="WARNING") as cm:
            self.fw.apply()
        self.assertTrue(any("nft not found" in m for m in cm.output), cm.output)

    def test_log_trail_permission_denied(self):
        err = subprocess.CalledProcessError(1, ["nft"], stderr="Operation not permitted")
        with mock.patch("firewall._install_rules", side_effect=err), \
             self.assertLogs("breaktimer.firewall", level="WARNING") as cm:
            self.fw.apply()
        self.assertTrue(
            any("AmbientCapabilities" in m for m in cm.output),
            cm.output,
        )


# ---------------------------------------------------------------------------
# cleanup()
# ---------------------------------------------------------------------------

class TestFirewallCleanup(_FirewallTest):
    def test_cleanup_removes_table(self):
        self.fw._rules_installed = True
        with mock.patch("subprocess.run", side_effect=_run_ok) as mock_run:
            self.fw.cleanup()
        self.assertFalse(self.fw._rules_installed)
        # Verify nft delete table was called
        cmds = [c.args[0] for c in mock_run.call_args_list]
        self.assertIn(["nft", "delete", "table", "inet", "breaktimer"], cmds)

    def test_cleanup_noop_when_not_installed(self):
        with mock.patch("subprocess.run") as mock_run:
            self.fw.cleanup()
        mock_run.assert_not_called()

    def test_cleanup_graceful_on_nft_error(self):
        self.fw._rules_installed = True
        with mock.patch("subprocess.run",
                        side_effect=subprocess.CalledProcessError(1, ["nft"])):
            self.fw.cleanup()  # must not raise

    def test_cleanup_graceful_on_nft_missing(self):
        self.fw._rules_installed = True
        with mock.patch("subprocess.run", side_effect=FileNotFoundError()):
            self.fw.cleanup()  # must not raise

    def test_log_trail_on_cleanup(self):
        self.fw._rules_installed = True
        with mock.patch("subprocess.run", side_effect=_run_ok), \
             self.assertLogs("breaktimer.firewall", level="INFO") as cm:
            self.fw.cleanup()
        self.assertTrue(any("removed" in m for m in cm.output), cm.output)


# ---------------------------------------------------------------------------
# _table_exists()
# ---------------------------------------------------------------------------

class TestFirewallTableExists(unittest.TestCase):
    def test_true_when_nft_succeeds(self):
        with mock.patch("subprocess.run", return_value=mock.MagicMock(returncode=0)):
            self.assertTrue(firewall._table_exists())

    def test_false_when_nft_returns_nonzero(self):
        with mock.patch("subprocess.run", return_value=mock.MagicMock(returncode=1)):
            self.assertFalse(firewall._table_exists())

    def test_false_when_nft_missing(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError()):
            self.assertFalse(firewall._table_exists())


# ---------------------------------------------------------------------------
# _build_script() — pure function, no mocking needed
# ---------------------------------------------------------------------------

class TestFirewallBuildScript(unittest.TestCase):
    def _script(self):
        return firewall._build_script()

    def test_contains_table_creation(self):
        s = self._script()
        self.assertIn("add table inet breaktimer", s)

    def test_contains_chain_with_output_hook(self):
        s = self._script()
        self.assertIn("add chain inet breaktimer output", s)
        self.assertIn("type filter hook output", s)
        self.assertIn("policy accept", s)

    def test_contains_tcp_443_853_drop(self):
        s = self._script()
        self.assertIn("tcp dport { 443, 853 } drop", s)

    def test_contains_udp_853_drop(self):
        s = self._script()
        self.assertIn("udp dport 853 drop", s)

    def test_contains_ipv4_addresses(self):
        s = self._script()
        for ip in ("1.1.1.1", "8.8.8.8", "9.9.9.9"):
            self.assertIn(ip, s, f"expected {ip} in nft script")

    def test_contains_ipv6_rules_when_ips6_set(self):
        s = self._script()
        self.assertIn("ip6 daddr", s)
        self.assertIn("2606:4700:4700::1111", s)

    def test_no_ipv6_rules_when_ips6_empty(self):
        s = firewall._build_script(doh_ips6=frozenset())
        self.assertNotIn("ip6", s)

    def test_all_ipv4_ips_present(self):
        s = self._script()
        for ip in firewall.DOH_SERVER_IPS:
            self.assertIn(ip, s, f"expected {ip} in nft script")

    def test_all_ipv6_ips_present(self):
        s = self._script()
        for ip in firewall.DOH_SERVER_IPS6:
            self.assertIn(ip, s, f"expected {ip} in nft script")

    def test_script_ends_with_newline(self):
        self.assertTrue(self._script().endswith("\n"))


# ---------------------------------------------------------------------------
# _install_rules() — subprocess calls
# ---------------------------------------------------------------------------

class TestFirewallInstallRules(unittest.TestCase):
    def test_calls_nft_dash_f(self):
        with mock.patch("subprocess.run", side_effect=_run_ok) as mock_run:
            firewall._install_rules(firewall._build_script())
        cmds = [tuple(c.args[0]) for c in mock_run.call_args_list]
        self.assertIn(("nft", "-f", "-"), cmds)

    def test_passes_script_via_stdin(self):
        captured = {}
        def _capture(*args, **kwargs):
            if args and "-f" in args[0]:
                captured["script"] = kwargs.get("input", "")
            return mock.MagicMock(returncode=0)
        with mock.patch("subprocess.run", side_effect=_capture):
            firewall._install_rules(firewall._build_script())
        self.assertIn("add table inet breaktimer", captured.get("script", ""))

    def test_attempts_delete_before_add(self):
        calls = []
        def _track(*args, **kwargs):
            calls.append(args[0])
            if "-f" in args[0]:
                return mock.MagicMock(returncode=0, stdout="", stderr="")
            return mock.MagicMock(returncode=0, stdout="", stderr="")
        with mock.patch("subprocess.run", side_effect=_track):
            firewall._install_rules(firewall._build_script())
        # delete should precede the -f - call
        delete_idx = next(
            (i for i, c in enumerate(calls) if "delete" in c), None
        )
        install_idx = next(
            (i for i, c in enumerate(calls) if "-f" in c), None
        )
        self.assertIsNotNone(delete_idx)
        self.assertIsNotNone(install_idx)
        self.assertLess(delete_idx, install_idx)

    def test_raises_on_install_failure(self):
        def _side(*args, **kwargs):
            if "-f" in args[0]:
                raise subprocess.CalledProcessError(1, args[0], stderr="denied")
            return mock.MagicMock(returncode=0)
        with mock.patch("subprocess.run", side_effect=_side):
            with self.assertRaises(subprocess.CalledProcessError):
                firewall._install_rules(firewall._build_script())

    def test_raises_when_nft_missing(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError()):
            with self.assertRaises(FileNotFoundError):
                firewall._install_rules(firewall._build_script())
