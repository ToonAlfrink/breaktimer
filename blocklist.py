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
apply(is_active, strict) each adjustment tick; the module rewrites the /etc/hosts
block atomically only when the content has changed, and logs every mutation via
the why-it-acted trail.

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
import datetime
import logging
import os
import re

log = logging.getLogger("breaktimer.blocklist")

HOSTS_PATH = "/etc/hosts"

_BLOCK_BEGIN = "# BEGIN breaktimer-blocklist"
_BLOCK_END = "# END breaktimer-blocklist"

# Set by the core after it resolves STATE_DIR, so this module has no circular dep.
blocklist_file: str | None = None           # always-blocked tier
blocklist_active_file: str | None = None    # work-session tier (blocked while timer active)
blocklist_strict_file: str | None = None    # strict tier (blocked when daily refill is gone)
blocklist_schedule_file: str | None = None  # schedule tier (blocked during time windows)

# Regex for window header lines in blocklist-schedule.txt, e.g. "# 22:00-08:00"
_WINDOW_RE = re.compile(r"^#\s*(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})\s*$")

# Last block actually written — skip the filesystem round-trip when unchanged.
_last_written: str | None = None
# Suppress repeated write-failure warnings: log once, stay quiet.
_write_failed = False


def _read_file_domains(path: str | None) -> list[str]:
    """Return domains from a single file (deduplicated, sorted).

    Returns an empty list if path is None, missing, or blank.
    """
    if not path:
        return []
    try:
        with open(path) as f:
            lines = f.readlines()
    except OSError:
        return []

    domains: list[str] = []
    seen: set[str] = set()
    for raw in lines:
        d = raw.strip()
        if d and not d.startswith("#"):
            d = d.lower()
            if d not in seen:
                seen.add(d)
                domains.append(d)
    return sorted(domains)


def _minutes_since_midnight() -> int:
    """Return minutes elapsed since midnight (0–1439)."""
    now = datetime.datetime.now()
    return now.hour * 60 + now.minute


def _in_window(start_min: int, end_min: int, now_min: int) -> bool:
    """Is now_min within [start_min, end_min)? Handles midnight wrap-around.

    A zero-length window (start == end) is never active.
    """
    if start_min < end_min:       # same-day:      e.g. 09:00–17:00
        return start_min <= now_min < end_min
    elif start_min > end_min:     # wrap-around:   e.g. 22:00–08:00
        return now_min >= start_min or now_min < end_min
    return False


def _read_file_domains_scheduled(path: str | None, now_min: int | None = None) -> list[str]:
    """Parse a schedule file and return domains whose window is currently active.

    Structured-comment headers (# HH:MM-HH:MM) define windows; the domains
    listed below each header are included when that window is active.
    Domains that appear before the first window header are ignored.
    """
    if not path:
        return []
    if now_min is None:
        now_min = _minutes_since_midnight()
    try:
        with open(path) as f:
            lines = f.readlines()
    except OSError:
        return []

    current_window: tuple[int, int] | None = None
    domains: list[str] = []
    seen: set[str] = set()

    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            continue
        m = _WINDOW_RE.match(stripped)
        if m:
            sh, sm = m.group(1).split(":")
            eh, em = m.group(2).split(":")
            current_window = (int(sh) * 60 + int(sm), int(eh) * 60 + int(em))
            continue
        if stripped.startswith("#"):
            continue
        if current_window is None:
            continue
        if _in_window(current_window[0], current_window[1], now_min):
            d = stripped.lower()
            if d not in seen:
                seen.add(d)
                domains.append(d)

    return sorted(domains)


def read_domains() -> list[str]:
    """Return always-blocked domains from blocklist.txt (deduplicated, sorted).

    Returns an empty list if the file is missing or blank.
    """
    return _read_file_domains(blocklist_file)


def read_domains_active() -> list[str]:
    """Return work-session domains from blocklist-active.txt (deduplicated, sorted)."""
    return _read_file_domains(blocklist_active_file)


def read_domains_strict() -> list[str]:
    """Return strict-tier domains from blocklist-strict.txt (deduplicated, sorted)."""
    return _read_file_domains(blocklist_strict_file)


def read_domains_schedule(now_min: int | None = None) -> list[str]:
    """Return schedule-tier domains from blocklist-schedule.txt active right now.

    now_min: minutes since midnight (0–1439). Pass an explicit value for testing;
    omit to use the current wall-clock time.
    """
    return _read_file_domains_scheduled(blocklist_schedule_file, now_min)


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


def _read_hosts() -> str:
    try:
        with open(HOSTS_PATH) as f:
            return f.read()
    except OSError as e:
        log.warning("blocklist: cannot read %s: %s", HOSTS_PATH, e)
        return ""


def _write_hosts(content: str) -> Exception | None:
    """Write /etc/hosts atomically (temp-then-rename).

    Returns None on success, the OSError on failure.
    """
    tmp = HOSTS_PATH + ".breaktimer-tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.rename(tmp, HOSTS_PATH)
        return None
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return e


def apply(is_active: bool = False, strict: bool = False, _now_min: int | None = None) -> None:
    """Sync /etc/hosts with the union of applicable tier lists.

    is_active:  include blocklist-active.txt (work-session enforcement)
    strict:     include blocklist-strict.txt (day-is-over enforcement)
    _now_min:   minutes since midnight override (0–1439); for testing only.
                Omit to use the current wall-clock time for schedule evaluation.

    Called each adjustment tick from the timer core via the effects worker.
    No-ops when the computed block matches what was last written. Logs each
    real mutation with the domain count, active tiers, and domain names.
    """
    global _last_written, _write_failed

    always_domains   = set(_read_file_domains(blocklist_file))
    active_domains   = set(_read_file_domains(blocklist_active_file)) if is_active else set()
    strict_domains   = set(_read_file_domains(blocklist_strict_file)) if strict else set()
    schedule_domains = set(_read_file_domains_scheduled(blocklist_schedule_file, _now_min))

    all_domains = sorted(always_domains | active_domains | strict_domains | schedule_domains)
    block = _block_lines(all_domains)

    if block == _last_written:
        return  # nothing changed — skip the filesystem round-trip

    hosts = _read_hosts()
    new_hosts = _splice(hosts, block)

    err = _write_hosts(new_hosts)
    if err is not None:
        if not _write_failed:
            log.warning(
                "blocklist: cannot write %s: %s  "
                "(make /etc/hosts user-writable or add ReadWritePaths=/etc/hosts "
                "to breaktimer-core.service)",
                HOSTS_PATH, err,
            )
            _write_failed = True
        return

    _write_failed = False
    _last_written = block
    if all_domains:
        tiers = []
        if always_domains:
            tiers.append(f"always:{len(always_domains)}")
        if is_active and active_domains:
            tiers.append(f"active:{len(active_domains)}")
        if strict and strict_domains:
            tiers.append(f"strict:{len(strict_domains)}")
        if schedule_domains:
            tiers.append(f"schedule:{len(schedule_domains)}")
        log.info(
            "blocklist: sinkholed %d domain(s) [%s] in %s: %s",
            len(all_domains),
            " ".join(tiers) if tiers else "none",
            HOSTS_PATH,
            ", ".join(all_domains),
        )
    else:
        log.info(
            "blocklist: removed sinkhole block from %s (all tier files empty or absent)",
            HOSTS_PATH,
        )
