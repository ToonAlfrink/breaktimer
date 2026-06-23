"""Domain blocklist: sinkhole a configurable set of domains to 0.0.0.0 in /etc/hosts.

The owner places one domain per line in STATE_DIR/blocklist.txt (bare hostnames,
no 'www.' prefix needed — the module adds both bare and www. variants).  The
core calls apply() each adjustment tick; the module rewrites the /etc/hosts
block atomically only when the content has changed, and logs every mutation via
the why-it-acted trail.

The /etc/hosts block is demarcated by:
    # BEGIN breaktimer-blocklist
    ...
    # END breaktimer-blocklist

Everything outside that region is preserved untouched.  If the markers are
absent the block is appended.  If blocklist.txt is missing or empty the block
is removed.

Writing /etc/hosts requires the file to be writable by the running user.
The core service must declare ReadWritePaths=/etc/hosts and /etc/hosts must
be user- or group-writable (e.g. chown user /etc/hosts, or add the user to a
group that owns /etc/hosts).  When the write fails, the module logs a warning
once and stays quiet until the next successful write.
"""
import logging
import os
import re

log = logging.getLogger("breaktimer.blocklist")

HOSTS_PATH = "/etc/hosts"

_BLOCK_BEGIN = "# BEGIN breaktimer-blocklist"
_BLOCK_END = "# END breaktimer-blocklist"

# Set by the core after it resolves STATE_DIR, so this module has no circular dep.
blocklist_file: str | None = None

# Last block actually written — skip the filesystem round-trip when unchanged.
_last_written: str | None = None
# Suppress repeated write-failure warnings: log once, stay quiet.
_write_failed = False


def read_domains() -> list[str]:
    """Return the owner-configured domains from blocklist.txt (deduplicated, sorted).

    Returns an empty list if the file is missing or blank — the block is then
    removed from /etc/hosts rather than leaving stale sinkhole entries.
    """
    if not blocklist_file:
        return []
    try:
        with open(blocklist_file) as f:
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
    between them.  If non-empty, replace the existing markers or append.
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


def apply() -> None:
    """Sync /etc/hosts with the current blocklist.txt.

    Called each adjustment tick from the timer core via the effects worker.
    No-ops when the computed block matches what was last written.  Logs each
    real mutation with the domain count and names.
    """
    global _last_written, _write_failed

    domains = read_domains()
    block = _block_lines(domains)

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
    if domains:
        log.info(
            "blocklist: sinkholed %d domain(s) in %s: %s",
            len(domains), HOSTS_PATH, ", ".join(domains),
        )
    else:
        log.info(
            "blocklist: removed sinkhole block from %s (blocklist.txt empty or absent)",
            HOSTS_PATH,
        )
