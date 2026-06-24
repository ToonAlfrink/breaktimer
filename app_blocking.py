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

apply(is_active, strict) is called every tick (1 Hz) from the timer core. Signal
escalation: SIGTERM is sent on first contact (lets the app save state); if the
process is still alive after SIGKILL_DELAY_TICKS apply() calls, SIGKILL is sent.
Each signal is logged with process name, PID, and the tier that triggered it
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

# Signal escalation: SIGTERM on first contact, SIGKILL if the process is still
# alive after this many apply() calls.  At 1 Hz that equals 5 seconds.
SIGKILL_DELAY_TICKS: int = 5

# pid → _apply_count value when SIGTERM was first delivered.
_sigterm_tick: dict[int, int] = {}
# Monotonic apply() call counter (never resets between ticks; tests reset it).
_apply_count: int = 0


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


def _send_signal(pid: int, sig: signal.Signals) -> bool:
    """Deliver sig to pid.  Returns True if delivered, False if gone or denied."""
    try:
        os.kill(pid, sig)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def apply(is_active: bool = False, strict: bool = False, _now_min: int | None = None) -> None:
    """Kill any running process whose name appears in the active tier union.

    is_active:  include blocklist-apps-active.txt (work-session enforcement)
    strict:     include blocklist-apps-strict.txt (day-is-over enforcement)
    _now_min:   minutes-since-midnight override (0–1439); for testing only.

    Signal escalation: SIGTERM on first contact (gives the app a chance to save
    state); SIGKILL after SIGKILL_DELAY_TICKS apply() calls if the process is
    still alive.  Each signal is logged with name, PID, and triggering tier(s).
    Processes belonging to other users are silently skipped.
    """
    global _apply_count
    _apply_count += 1

    always_names   = set(status.read_items(app_blocklist_file))
    active_names   = set(status.read_items(app_blocklist_active_file)) if is_active else set()
    strict_names   = set(status.read_items(app_blocklist_strict_file)) if strict else set()
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

    in_scope: set[int] = set()
    for name, tiers in sorted(tier_map.items()):
        tier_label = "+".join(tiers)
        for pid in _find_pids(name):
            in_scope.add(pid)
            if pid not in _sigterm_tick:
                if _send_signal(pid, signal.SIGTERM):
                    _sigterm_tick[pid] = _apply_count
                    log.info("app-block: SIGTERM %s (pid %d) [%s]", name, pid, tier_label)
            elif _apply_count - _sigterm_tick[pid] >= SIGKILL_DELAY_TICKS:
                if _send_signal(pid, signal.SIGKILL):
                    log.info(
                        "app-block: SIGKILL %s (pid %d) [%s] (survived SIGTERM)",
                        name, pid, tier_label,
                    )
                # Whether SIGKILL was delivered or not, clear pending state so the
                # next tick starts a fresh SIGTERM cycle (handles zombie edge case).
                _sigterm_tick.pop(pid, None)

    # Forget PIDs no longer in scope (tier deactivated, process already gone, etc.)
    for pid in list(_sigterm_tick):
        if pid not in in_scope:
            del _sigterm_tick[pid]
