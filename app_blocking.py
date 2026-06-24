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

apply(is_active, strict) is called every tick (1 Hz) from the timer core. It is
idempotent — once a process is dead, pgrep finds nothing and the call is a no-op.
Each kill is logged with process name, PID, and the tier that triggered it
(why-it-acted trail). Processes owned by other users are silently skipped
(PermissionError from os.kill).
"""
import logging
import os
import signal
import subprocess

import status

log = logging.getLogger("breaktimer.apps")

# Set by the core after it resolves STATE_DIR, so this module has no circular dep.
app_blocklist_file: str | None = None           # always-blocked tier
app_blocklist_active_file: str | None = None    # work-session tier
app_blocklist_strict_file: str | None = None    # strict tier
app_blocklist_schedule_file: str | None = None  # schedule tier


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
    return status.active_schedule_items(app_blocklist_schedule_file, now_min)


def read_schedule_windows(now_min: int | None = None) -> list[tuple[int, int, list[str], bool]]:
    """Return all schedule windows from blocklist-apps-schedule.txt with their active state.

    Each entry is (start_min, end_min, names, is_active_now). Returns all
    windows regardless of whether they are currently active — useful for
    displaying the full schedule configuration in 'breaktimer blocklist'.
    """
    return status.parse_schedule_file(app_blocklist_schedule_file, now_min)


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
    schedule_names = set(status.active_schedule_items(app_blocklist_schedule_file, _now_min))

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
