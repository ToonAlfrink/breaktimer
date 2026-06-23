"""Process/app blocking: kill running processes that appear in tier block lists.

Four independent tiers, each backed by an owner-edited file in STATE_DIR:

  blocklist-apps.txt          — always blocked (gaming clients, media players, etc.)
  blocklist-apps-active.txt   — blocked only while the timer is active (work-session
                                enforcement: distracting apps killed during sessions)
  blocklist-apps-strict.txt   — additionally blocked when daily refill is gone
                                (day-is-over enforcement: everything locked down)
  blocklist-apps-schedule.txt — blocked during configured time windows, regardless of
                                timer state. Same # HH:MM-HH:MM window header format
                                as blocklist-schedule.txt.

File format: one process name per line (bare names, no path). Names are matched
case-insensitively and exactly (whole process name via pgrep -ix). The owner lists
the name that appears in `ps` / `pgrep` output, e.g. "steam", "discord", "firefox".

apply(is_active, strict) is called alongside blocklist.apply() in the adjustment
tick every 10 s. It is idempotent — once a process is dead, pgrep finds nothing
and the call is a no-op. Each kill is logged with process name, PID, and the tier
that triggered it (why-it-acted trail). Processes owned by other users are silently
skipped (PermissionError from os.kill).
"""
import datetime
import logging
import os
import re
import signal
import subprocess

log = logging.getLogger("breaktimer.apps")

# Set by the core after it resolves STATE_DIR, so this module has no circular dep.
app_blocklist_file: str | None = None           # always-blocked tier
app_blocklist_active_file: str | None = None    # work-session tier
app_blocklist_strict_file: str | None = None    # strict tier
app_blocklist_schedule_file: str | None = None  # schedule tier

# Regex for window header lines in blocklist-apps-schedule.txt, e.g. "# 22:00-08:00"
_WINDOW_RE = re.compile(r"^#\s*(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})\s*$")


def _minutes_since_midnight() -> int:
    """Return minutes elapsed since midnight (0–1439)."""
    now = datetime.datetime.now()
    return now.hour * 60 + now.minute


def _in_window(start_min: int, end_min: int, now_min: int) -> bool:
    """Is now_min within [start_min, end_min)? Handles midnight wrap-around.

    A zero-length window (start == end) is never active.
    """
    if start_min < end_min:       # same-day:    e.g. 09:00–17:00
        return start_min <= now_min < end_min
    elif start_min > end_min:     # wrap-around: e.g. 22:00–08:00
        return now_min >= start_min or now_min < end_min
    return False


def _read_names(path: str | None) -> list[str]:
    """Return app names from a flat file (deduplicated, lowercased, sorted).

    Returns an empty list if path is None, missing, or blank.
    """
    if not path:
        return []
    try:
        with open(path) as f:
            lines = f.readlines()
    except OSError:
        return []
    seen: set[str] = set()
    names: list[str] = []
    for raw in lines:
        name = raw.strip().lower()
        if name and not name.startswith("#") and name not in seen:
            seen.add(name)
            names.append(name)
    return sorted(names)


def _read_names_scheduled(path: str | None, now_min: int | None = None) -> list[str]:
    """Parse a schedule file and return app names whose window is currently active.

    Structured-comment headers (# HH:MM-HH:MM) define windows; app names listed
    below each header are included when that window is active. Names before the
    first window header are ignored.
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
    names: list[str] = []
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
            name = stripped.lower()
            if name not in seen:
                seen.add(name)
                names.append(name)
    return sorted(names)


def _find_pids(name: str) -> list[int]:
    """Return PIDs of processes whose name exactly matches (case-insensitive).

    Uses pgrep -ix: -i for case-insensitive, -x for exact (full name) match.
    Returns an empty list if pgrep is absent or finds nothing.
    """
    try:
        result = subprocess.run(
            ["pgrep", "-ix", name],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            return [int(p) for p in result.stdout.strip().split("\n") if p.strip()]
    except FileNotFoundError:
        pass
    return []


def _kill(pid: int) -> bool:
    """Send SIGTERM to pid. Returns True if delivered, False if gone or denied."""
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def read_names() -> list[str]:
    """Return always-blocked app names from blocklist-apps.txt."""
    return _read_names(app_blocklist_file)


def read_names_active() -> list[str]:
    """Return work-session app names from blocklist-apps-active.txt."""
    return _read_names(app_blocklist_active_file)


def read_names_strict() -> list[str]:
    """Return strict-tier app names from blocklist-apps-strict.txt."""
    return _read_names(app_blocklist_strict_file)


def read_names_schedule(now_min: int | None = None) -> list[str]:
    """Return schedule-tier app names from blocklist-apps-schedule.txt active right now."""
    return _read_names_scheduled(app_blocklist_schedule_file, now_min)


def apply(is_active: bool = False, strict: bool = False, _now_min: int | None = None) -> None:
    """Kill any running process whose name appears in the active tier union.

    is_active:  include blocklist-apps-active.txt (work-session enforcement)
    strict:     include blocklist-apps-strict.txt (day-is-over enforcement)
    _now_min:   minutes-since-midnight override (0–1439); for testing only.

    Each killed process is logged with name, PID, and which tier(s) triggered
    it. Processes belonging to other users are silently skipped.
    """
    always_names   = set(_read_names(app_blocklist_file))
    active_names   = set(_read_names(app_blocklist_active_file)) if is_active else set()
    strict_names   = set(_read_names(app_blocklist_strict_file)) if strict else set()
    schedule_names = set(_read_names_scheduled(app_blocklist_schedule_file, _now_min))

    # Map each name to the tier(s) that triggered it (for the log).
    tier_map: dict[str, list[str]] = {}
    for name in always_names:
        tier_map.setdefault(name, []).append("always")
    for name in active_names:
        tier_map.setdefault(name, []).append("active")
    for name in strict_names:
        tier_map.setdefault(name, []).append("strict")
    for name in schedule_names:
        tier_map.setdefault(name, []).append("schedule")

    for name, tiers in sorted(tier_map.items()):
        for pid in _find_pids(name):
            if _kill(pid):
                log.info(
                    "app-block: killed %s (pid %d) [%s]",
                    name, pid, "+".join(tiers),
                )
