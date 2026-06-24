"""Domain blocklist: sinkhole configurable sets of domains to 0.0.0.0 in /etc/hosts.

Four independent tiers, each backed by an owner-edited file in STATE_DIR:

  blocklist.txt          — always blocked (permanent distractions; gambling, news, etc.)
  blocklist-active.txt   — blocked only while the timer is active (work-session enforcement:
                           sites unavailable during a work session, accessible again on a
                           break when the bar is refilling)
  blocklist-strict.txt   — additionally blocked when daily refill is gone (day is over;
                           enforces shutdown once the limit is reached)
  blocklist-schedule.txt — blocked during configured time windows, regardless of timer state.
                           File format: structured-comment window headers gate the domains
                           below them:

                               # 22:00-08:00
                               reddit.com
                               youtube.com

                               # 09:00-17:00
                               news.ycombinator.com

                           Times are 24-h; wrap-around (22:00-08:00 = 10 pm to 8 am) is
                           supported. Domains before the first window header are ignored.

The owner places one domain per line in each file (bare hostnames, no 'www.' prefix
needed — the module adds both bare and www. variants). The core calls
apply(is_active, strict) each tick (1 Hz); the module rewrites the /etc/hosts block
atomically only when the content has changed, and logs every mutation via the
why-it-acted trail.

DoH sinkholing: when any user-configured domains are blocked, well-known
DNS-over-HTTPS server hostnames are automatically added to the sinkhole so
browsers cannot bypass /etc/hosts by switching to DoH. The list lives in
DOH_SERVER_DOMAINS and can be overridden (e.g. set to frozenset() in tests).

The /etc/hosts block is demarcated by:
    # BEGIN breaktimer-blocklist
    ...
    # END breaktimer-blocklist

Everything outside that region is preserved untouched. If the markers are absent
the block is appended. If all applicable files are missing or empty the block is
removed.

Writing /etc/hosts requires the file to be writable by the running user.
The core service must declare ReadWritePaths=/etc/hosts and /etc/hosts must
be user- or group-writable (e.g. chown user /etc/hosts, or add the user to a
group that owns /etc/hosts). When the write fails, the module logs a warning
once and stays quiet until the next successful write.
"""
import logging
import os
import re

import status

log = logging.getLogger("breaktimer.blocklist")

HOSTS_PATH = "/etc/hosts"

_BLOCK_BEGIN = "# BEGIN breaktimer-blocklist"
_BLOCK_END = "# END breaktimer-blocklist"

# Well-known DoH providers — automatically added to the sinkhole whenever any
# user-configured domains are blocked, so browsers cannot bypass /etc/hosts via
# DNS-over-HTTPS. Set to frozenset() to disable (e.g. in tests).
DOH_SERVER_DOMAINS: frozenset = frozenset([
    "cloudflare-dns.com",
    "dns.adguard.com",
    "dns.google",
    "dns.nextdns.io",
    "dns.quad9.net",
    "doh.opendns.com",
    "mozilla.cloudflare-dns.com",
])


def _block_lines(domains: list[str]) -> str:
    """Build the /etc/hosts block content (with markers) for the given domains.

    Each bare domain gets both 'domain.com' and 'www.domain.com' sinkhole
    entries unless the owner already supplied the www. prefix explicitly.
    """
    if not domains:
        return ""

    entries: list[str] = []
    seen: set[str] = set()
    for d in domains:
        candidates = [d]
        if not d.startswith("www."):
            candidates.append("www." + d)
        for c in candidates:
            if c not in seen:
                seen.add(c)
                entries.append(f"0.0.0.0 {c}")

    body = "\n".join(entries)
    return f"{_BLOCK_BEGIN}\n{body}\n{_BLOCK_END}\n"


def _splice(hosts_text: str, block: str) -> str:
    """Replace or insert the breaktimer block in the /etc/hosts text.

    If block is empty (no domains), remove any existing markers and the lines
    between them. If non-empty, replace the existing markers or append.
    """
    pattern = re.compile(
        r"^# BEGIN breaktimer-blocklist\n.*?^# END breaktimer-blocklist\n?",
        re.MULTILINE | re.DOTALL,
    )
    if not block:
        result = pattern.sub("", hosts_text)
        # Collapse any triple+ blank lines left behind.
        result = re.sub(r"\n{3,}", "\n\n", result)
        return result

    if pattern.search(hosts_text):
        return pattern.sub(block, hosts_text)

    # No existing block — append with a blank separator.
    return hosts_text.rstrip("\n") + "\n\n" + block


def _read_hosts(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except OSError as e:
        log.warning("blocklist: cannot read %s: %s", path, e)
        return ""


class Blocklist:
    """Manages /etc/hosts domain blocklist as instance state."""

    def __init__(self, state_dir: str, hosts_path: str = HOSTS_PATH, doh_domains=None):
        self._tiers: status.TierSet = status.TierSet.for_prefix(state_dir, "blocklist")
        self._hosts_path = hosts_path
        self.doh_domains = doh_domains if doh_domains is not None else DOH_SERVER_DOMAINS
        # Last block actually written — skip the filesystem round-trip when unchanged.
        self._last_written: str | None = None
        # mtime_ns of hosts_path at the moment we last wrote it.
        self._last_written_mtime_ns: int | None = None
        # Suppress repeated write-failure warnings: log once, stay quiet.
        self._write_failed: bool = False

    @property
    def tiers(self) -> status.TierSet:
        return self._tiers

    def apply(self, is_active: bool = False, strict: bool = False, _now_min: int | None = None) -> None:
        """Sync /etc/hosts with the union of applicable tier lists.

        is_active:  include blocklist-active.txt (work-session enforcement)
        strict:     include blocklist-strict.txt (day-is-over enforcement)
        _now_min:   minutes since midnight override (0–1439); for testing only.
                    Omit to use the current wall-clock time for schedule evaluation.

        Called every tick (1 Hz) from the timer core via the effects worker.
        No-ops when the computed block matches what was last written — unless the
        file's mtime changed between calls, which means an external process tampered
        with /etc/hosts; in that case a WARNING is logged and the block is restored
        immediately. Logs each real mutation with the domain count, active tiers,
        and domain names.

        When any user-configured domains are blocked, well-known DoH server hostnames
        (doh_domains) are automatically added to the sinkhole so browsers cannot bypass
        /etc/hosts via DNS-over-HTTPS.
        """
        breakdown = self._tiers.breakdown(is_active, strict, _now_min)
        user_domains: set[str] = set().union(*breakdown.values())
        # When any user-configured domains are blocked, also sinkhole well-known DoH
        # providers so browsers cannot route around /etc/hosts via DNS-over-HTTPS.
        doh_domains = self.doh_domains if user_domains else frozenset()
        all_domains = sorted(user_domains | doh_domains)
        block = _block_lines(all_domains)

        if block == self._last_written:
            # Content hasn't changed according to our records — but check whether an
            # external process edited the hosts file since our last write.
            if self._last_written_mtime_ns is not None:
                try:
                    current_mtime_ns = os.stat(self._hosts_path).st_mtime_ns
                except OSError:
                    current_mtime_ns = None
                if current_mtime_ns != self._last_written_mtime_ns:
                    log.warning(
                        "blocklist: %s was modified externally (mtime changed) — restoring block",
                        self._hosts_path,
                    )
                    self._last_written = None  # force rewrite below
                else:
                    return  # nothing changed — skip the filesystem round-trip
            else:
                return

        hosts = _read_hosts(self._hosts_path)
        new_hosts = _splice(hosts, block)

        try:
            status.atomic_write(self._hosts_path, new_hosts, mode=0o644)
        except OSError as e:
            if not self._write_failed:
                log.warning(
                    "blocklist: cannot write %s: %s  "
                    "(make /etc/hosts user-writable or add ReadWritePaths=/etc/hosts "
                    "to breaktimer-core.service)",
                    self._hosts_path, e,
                )
                self._write_failed = True
            return

        self._write_failed = False
        self._last_written = block
        try:
            self._last_written_mtime_ns = os.stat(self._hosts_path).st_mtime_ns
        except OSError:
            self._last_written_mtime_ns = None
        if all_domains:
            tiers = [f"{t}:{len(items)}" for t, items in breakdown.items() if items]
            if doh_domains:
                tiers.append(f"doh:{len(doh_domains)}")
            log.info(
                "blocklist: sinkholed %d domain(s) [%s] in %s: %s",
                len(all_domains),
                " ".join(tiers) if tiers else "none",
                self._hosts_path,
                ", ".join(all_domains),
            )
        else:
            log.info(
                "blocklist: removed sinkhole block from %s (all tier files empty or absent)",
                self._hosts_path,
            )
