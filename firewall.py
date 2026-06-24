"""nftables-based firewall rules: block direct connections to well-known DoH server IPs.

Complements /etc/hosts DoH domain sinkholing (blocklist.py). When a browser
has hard-coded DoH server IP addresses rather than domain names, /etc/hosts
cannot intercept the lookup — the browser goes straight to the IP. This module
adds nftables OUTPUT rules that DROP outbound TCP/UDP connections to known DoH
IPs on ports 443 (HTTPS) and 853 (DNS-over-TLS), forcing browsers to fall back
to system DNS where /etc/hosts blocks take effect.

Table layout:

  table inet breaktimer
    chain output { type filter hook output priority 0 ; policy accept ; }
      ip  daddr { <DOH_SERVER_IPS>  } tcp dport { 443, 853 } drop
      ip  daddr { <DOH_SERVER_IPS>  } udp dport 853          drop
      ip6 daddr { <DOH_SERVER_IPS6> } tcp dport { 443, 853 } drop
      ip6 daddr { <DOH_SERVER_IPS6> } udp dport 853          drop

Requires CAP_NET_ADMIN. Add to breaktimer-core.service:

  AmbientCapabilities=CAP_NET_ADMIN

Falls back gracefully if nft is absent or permissions are denied: logs a
warning once, stays quiet until the service restarts, and /etc/hosts sinkholing
continues to handle domain-based DoH bypass.

apply() is called every tick (1 Hz) via the effects worker. It verifies the
nftables table still exists on each call; if an external process removed it,
rules are restored immediately — same tamper-detection pattern as the /etc/hosts
mtime check in blocklist.py.

Call cleanup() at shutdown to remove the table and restore normal routing.
"""
import logging
import subprocess

log = logging.getLogger("breaktimer.firewall")

_TABLE = "breaktimer"
_CHAIN = "output"

# Well-known DoH providers' IP addresses. Blocking these prevents browsers
# from bypassing /etc/hosts by connecting directly to DoH servers via
# hard-coded IPs rather than resolving their domain names first.
# Set to frozenset() to disable (e.g. in tests or to isolate exact assertions).
DOH_SERVER_IPS: frozenset = frozenset([
    # Cloudflare
    "1.0.0.1", "1.1.1.1",
    # Google
    "8.8.4.4", "8.8.8.8",
    # Quad9
    "9.9.9.9", "149.112.112.112",
    # AdGuard
    "94.140.14.14", "94.140.15.15",
    # OpenDNS
    "208.67.220.220", "208.67.222.222",
    # NextDNS
    "45.90.28.0", "45.90.30.0",
])

DOH_SERVER_IPS6: frozenset = frozenset([
    # Cloudflare
    "2606:4700:4700::1001", "2606:4700:4700::1111",
    # Google
    "2001:4860:4860::8844", "2001:4860:4860::8888",
    # Quad9
    "2620:fe::9", "2620:fe::fe",
    # AdGuard
    "2a10:50c0::ad1:ff", "2a10:50c0::ad2:ff",
    # OpenDNS
    "2620:119:35::35", "2620:119:53::53",
])


def _build_script(doh_ips=None, doh_ips6=None) -> str:
    """Build the nft script that installs breaktimer's DoH-blocking rules."""
    if doh_ips is None:
        doh_ips = DOH_SERVER_IPS
    if doh_ips6 is None:
        doh_ips6 = DOH_SERVER_IPS6
    ips4 = ", ".join(sorted(doh_ips))
    lines = [
        f"add table inet {_TABLE}",
        f"add chain inet {_TABLE} {_CHAIN}"
        " { type filter hook output priority 0 ; policy accept ; }",
        f"add rule inet {_TABLE} {_CHAIN} ip daddr {{ {ips4} }}"
        " tcp dport { 443, 853 } drop",
        f"add rule inet {_TABLE} {_CHAIN} ip daddr {{ {ips4} }}"
        " udp dport 853 drop",
    ]
    if doh_ips6:
        ips6 = ", ".join(sorted(doh_ips6))
        lines.extend([
            f"add rule inet {_TABLE} {_CHAIN} ip6 daddr {{ {ips6} }}"
            " tcp dport { 443, 853 } drop",
            f"add rule inet {_TABLE} {_CHAIN} ip6 daddr {{ {ips6} }}"
            " udp dport 853 drop",
        ])
    return "\n".join(lines) + "\n"


def _table_exists() -> bool:
    """Return True if the breaktimer nftables table is present."""
    try:
        result = subprocess.run(
            ["nft", "list", "table", "inet", _TABLE],
            capture_output=True, text=True,
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


def _install_rules(script: str) -> None:
    """Create the breaktimer nftables table and install DoH-blocking rules.

    Removes any stale table first so rules are always exactly what we wrote.
    Raises FileNotFoundError if nft is absent, CalledProcessError on failure.
    """
    # Flush stale table (ignore errors — may not exist yet).
    subprocess.run(
        ["nft", "delete", "table", "inet", _TABLE],
        capture_output=True, text=True,
    )
    subprocess.run(
        ["nft", "-f", "-"],
        input=script, text=True,
        capture_output=True, check=True,
    )


class Firewall:
    """Manages nftables DoH-IP-blocking rules as instance state."""

    def __init__(self, doh_ips=None, doh_ips6=None):
        self._doh_ips = doh_ips if doh_ips is not None else DOH_SERVER_IPS
        self._doh_ips6 = doh_ips6 if doh_ips6 is not None else DOH_SERVER_IPS6
        # True once the table is successfully installed; reset by cleanup() or on failure.
        self._rules_installed: bool = False
        # Suppress repeated failure warnings: log once, stay quiet until restart.
        self._apply_failed: bool = False

    def apply(self, is_active: bool = False, strict: bool = False) -> None:
        """Ensure nftables DoH-IP-blocking rules are in place.

        is_active and strict are accepted for interface consistency with
        blocklist.apply() and app_blocking.apply(). The firewall rules are
        installed unconditionally while the service is running — any blocking
        configuration benefits from firewall-level DoH bypass prevention.

        Called every tick (1 Hz). No-ops quickly when the table already exists.
        Detects external deletion and restores rules immediately (tamper-resistance).
        """
        if not self._doh_ips:
            return  # disabled (empty frozenset; e.g. in tests)

        if self._apply_failed:
            return  # logged once; nft absent or permission denied

        if self._rules_installed:
            if _table_exists():
                return  # rules still in place — nothing to do
            log.warning(
                "firewall: nftables table '%s' deleted externally — restoring DoH-blocking rules",
                _TABLE,
            )
            self._rules_installed = False

        try:
            script = _build_script(self._doh_ips, self._doh_ips6)
            _install_rules(script)
            self._rules_installed = True
            self._apply_failed = False
            log.info(
                "firewall: installed DoH-blocking rules (%d IPv4, %d IPv6 addresses, ports 443/853)",
                len(self._doh_ips), len(self._doh_ips6),
            )
        except FileNotFoundError:
            self._apply_failed = True
            log.warning(
                "firewall: nft not found — DoH IP blocking inactive "
                "(domain-level DoH sinkholing via /etc/hosts still active)"
            )
        except subprocess.CalledProcessError as e:
            self._apply_failed = True
            log.warning(
                "firewall: nft failed (%s) — DoH IP blocking inactive; "
                "add AmbientCapabilities=CAP_NET_ADMIN to breaktimer-core.service to enable",
                (e.stderr or "").strip() or str(e),
            )

    def cleanup(self) -> None:
        """Remove the breaktimer nftables table (call at process shutdown)."""
        if not self._rules_installed:
            return
        try:
            subprocess.run(
                ["nft", "delete", "table", "inet", _TABLE],
                capture_output=True, text=True, check=True,
            )
            self._rules_installed = False
            log.info("firewall: removed nftables table '%s'", _TABLE)
        except (FileNotFoundError, subprocess.CalledProcessError) as e:
            log.warning("firewall: could not remove table '%s': %s", _TABLE, e)
