"""Live status bridge between the timer core and display surfaces.

The core (main.py) publishes a small JSON snapshot every tick; any UI
(ambient.py, or a future surface) reads it. The file lives in
XDG_RUNTIME_DIR (tmpfs): it is runtime-only state, costs no disk writes,
and vanishes at logout. Either process can restart without the other.
"""
import fcntl
import json
import os
import re
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, fields
from datetime import datetime

SECONDS_PER_MINUTE = 60


def _runtime_dir():
    d = os.environ.get("XDG_RUNTIME_DIR")
    if d:
        return d
    # /run/user/<uid> has mode 700 and is managed by systemd-logind.
    # Never fall back to /tmp which is world-readable.
    return f"/run/user/{os.getuid()}"


def status_path():
    return os.path.join(_runtime_dir(), "breaktimer-status.json")


def write_status(payload):
    """Atomically write the live status snapshot (owner-readable only)."""
    path = status_path()
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, path)


def read_status(max_age_seconds=5.0):
    """Return the latest status dict, or None if missing, stale, or corrupt."""
    path = status_path()
    try:
        if time.time() - os.stat(path).st_mtime > max_age_seconds:
            return None
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


@dataclass
class Snapshot:
    """The contract between the timer core and every display surface.

    The core builds one each tick and calls publish(); ambient.py and the CLI
    call read(). This dataclass IS the schema — the single place field names,
    types, and absence-semantics live, so neither side hard-codes string keys.

    Every field carries a default that reads as "unknown / not active", which
    serves two ends: a surface that catches a malformed or older snapshot
    degrades gracefully instead of crashing, and read() tolerates a producer on
    either side of a restart adding or dropping a field (the two services
    restart independently). The core always sets all fields explicitly.
    """
    remaining_seconds: float = 0.0
    max_seconds: float = 0.0
    is_active: bool = False
    grace_remaining: float | None = None   # seconds left in shutdown grace, or None
    refill_rate: float = 1.0               # idle-refill fatigue multiplier (1.0 = none)
    history: str = ""                      # one-line work-history summary

    def publish(self):
        write_status(asdict(self))

    @classmethod
    def read(cls, max_age_seconds=5.0):
        """Latest snapshot, or None if missing, stale, corrupt, or malformed."""
        data = read_status(max_age_seconds)
        if not isinstance(data, dict):
            return None
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


def state_dir():
    """Return the persistent state directory path ($XDG_STATE_HOME/breaktimer)."""
    base = os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
    return os.path.join(base, "breaktimer")


def brightness_pause_path():
    return os.path.join(_runtime_dir(), "breaktimer-brightness-pause")


def phone_activity_path():
    return os.path.join(_runtime_dir(), "breaktimer-phone-activity.json")


def write_phone_ping():
    """Atomically record the current wall-clock time as a phone activity ping."""
    path = phone_activity_path()
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"last_ping": time.time()}, f)
    os.replace(tmp, path)


def read_phone_ping():
    """Return wall-clock timestamp of the last phone ping, or None if missing/corrupt."""
    try:
        with open(phone_activity_path()) as f:
            data = json.load(f)
        ts = data.get("last_ping")
        return float(ts) if ts is not None else None
    except (OSError, ValueError, TypeError):
        return None


def acquire_singleton_lock(name):
    """Take an exclusive runtime lock for this process's lifetime.

    Returns the open lock file (keep a reference — the lock dies with it)
    or None if another instance already holds it.
    """
    path = os.path.join(_runtime_dir(), f"breaktimer-{name}.lock")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    f = os.fdopen(fd, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except OSError:
        f.close()
        return None


def today_str():
    return datetime.now().strftime('%Y-%m-%d')


def format_time(seconds):
    """Format seconds as M:SS or H:MM:SS when an hour or more."""
    seconds = int(max(0, seconds))
    if seconds >= 3600:
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        return f"{hours}:{minutes:02d}:{secs:02d}"
    minutes, secs = divmod(seconds, SECONDS_PER_MINUTE)
    return f"{minutes}:{secs:02d}"


_SPARK_CHARS = "▁▂▃▄▅▆▇█"


def format_history_line(daily_work_totals):
    """One line: today's hours, 7-day avg with delta, 12-month sparkline."""
    totals = daily_work_totals
    today = today_str()
    today_month = today[:7]
    past_days = sorted(d for d in totals if d < today)

    today_secs = totals.get(today, 0)
    today_h = today_secs / 3600
    week = past_days[-7:]
    avg_7d = sum(totals[d] for d in week) / len(week) / 3600 if week else 0

    monthly = defaultdict(float)
    for d, v in totals.items():
        if d[:7] != today_month:
            monthly[d[:7]] += v
    past_months = sorted(monthly)[-12:]
    if past_months:
        vals = [monthly[m] for m in past_months]
        lo, hi = min(vals), max(vals)
        if hi > lo:
            spark = "".join(
                _SPARK_CHARS[min(7, int((v - lo) / (hi - lo) * 8))]
                for v in vals
            )
        else:
            spark = _SPARK_CHARS[4] * len(past_months)
    else:
        spark = ""

    def _fmt_hours(h):
        if h < 1:
            return f"{int(round(h * 60))}m"
        return f"{h:.1f}h"

    parts = [f"{_fmt_hours(today_h)} today"]
    if avg_7d:
        diff = today_h - avg_7d
        sign = "+" if diff >= 0 else ""
        parts.append(f"avg {_fmt_hours(avg_7d)}  {sign}{diff:.1f}h")
    if spark:
        parts.append(spark)
    return "  ".join(parts)


# ---------------------------------------------------------------------------
# Flat-file item reader — shared by blocklist.py and app_blocking.py
# ---------------------------------------------------------------------------

def read_items(path: str | None) -> list[str]:
    """Read a flat list of items from a file (comments and blanks stripped, deduped, sorted).

    Returns an empty list if path is None, missing, or blank.
    Format: one item per line; lines starting with '#' and blank lines are ignored;
    items are lowercased and deduplicated. Used by blocklist.py and app_blocking.py
    to read their tier files (domains and process names share the same format).
    """
    if not path:
        return []
    try:
        with open(path) as f:
            lines = f.readlines()
    except OSError:
        return []
    seen: set[str] = set()
    items: list[str] = []
    for raw in lines:
        item = raw.strip().lower()
        if item and not item.startswith("#") and item not in seen:
            seen.add(item)
            items.append(item)
    return sorted(items)


# ---------------------------------------------------------------------------
# Schedule-window utilities — shared by blocklist.py and app_blocking.py
# ---------------------------------------------------------------------------

# Matches structured time-window comment headers, e.g. "# 22:00-08:00"
WINDOW_RE = re.compile(r"^#\s*(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})\s*$")


def minutes_since_midnight() -> int:
    """Return minutes elapsed since midnight (0–1439)."""
    now = datetime.now()
    return now.hour * 60 + now.minute


def in_window(start_min: int, end_min: int, now_min: int) -> bool:
    """Is now_min within [start_min, end_min)? Handles midnight wrap-around.

    A zero-length window (start == end) is never active.
    """
    if start_min < end_min:       # same-day:    e.g. 09:00–17:00
        return start_min <= now_min < end_min
    elif start_min > end_min:     # wrap-around: e.g. 22:00–08:00
        return now_min >= start_min or now_min < end_min
    return False


def fmt_window(start_min: int, end_min: int) -> str:
    """Format a time window as 'HH:MM-HH:MM'."""
    return (
        f"{start_min // 60:02d}:{start_min % 60:02d}"
        f"-{end_min // 60:02d}:{end_min % 60:02d}"
    )


def parse_schedule_file(
    path: str | None, now_min: int | None = None
) -> list[tuple[int, int, list[str], bool]]:
    """Parse a schedule file with # HH:MM-HH:MM window headers.

    Returns (start_min, end_min, items, is_active_now) for each non-empty window.
    Items are lowercased strings. Lines before the first window header are ignored.
    Empty windows (no items) are omitted.

    Used by blocklist.py and app_blocking.py for domain and app-name blocking.
    The file format is shared: one item per line, time windows gate them.
    """
    if not path:
        return []
    if now_min is None:
        now_min = minutes_since_midnight()
    try:
        with open(path) as f:
            lines = f.readlines()
    except OSError:
        return []

    result: list[tuple[int, int, list[str], bool]] = []
    current_window: tuple[int, int] | None = None
    current_items: list[str] = []
    seen: set[str] = set()

    def _flush():
        if current_window is not None and current_items:
            is_active = in_window(current_window[0], current_window[1], now_min)
            result.append((current_window[0], current_window[1], list(current_items), is_active))

    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            continue
        m = WINDOW_RE.match(stripped)
        if m:
            _flush()
            current_items = []
            seen = set()
            sh, sm = m.group(1).split(":")
            eh, em = m.group(2).split(":")
            current_window = (int(sh) * 60 + int(sm), int(eh) * 60 + int(em))
            continue
        if stripped.startswith("#"):
            continue
        if current_window is None:
            continue
        item = stripped.lower()
        if item not in seen:
            seen.add(item)
            current_items.append(item)

    _flush()
    return result


def active_schedule_items(
    path: str | None, now_min: int | None = None
) -> list[str]:
    """Return items from schedule-file windows that are currently active.

    Filters parse_schedule_file() to active windows, deduplicates across overlapping
    windows, and returns a sorted list. Used by blocklist.py and app_blocking.py.
    """
    seen: set[str] = set()
    items: list[str] = []
    for _, _, window_items, is_active in parse_schedule_file(path, now_min):
        if is_active:
            for item in window_items:
                if item not in seen:
                    seen.add(item)
                    items.append(item)
    return sorted(items)


# ---------------------------------------------------------------------------
# Four-tier blocking configuration — shared by blocklist.py and app_blocking.py
# ---------------------------------------------------------------------------

@dataclass
class TierSet:
    """Paths for the four blocking tiers: always / active / strict / schedule.

    Each tier is an owner-edited flat file. init() in blocklist.py and
    app_blocking.py constructs one via for_prefix(); apply() in both calls
    breakdown() to resolve which items are active without repeating the
    tier-union logic.
    """
    always: str | None
    active: str | None
    strict: str | None
    schedule: str | None

    @classmethod
    def for_prefix(cls, state_dir: str, prefix: str) -> "TierSet":
        """Construct paths from a state_dir and filename prefix.

        Convention: {prefix}.txt, {prefix}-active.txt, {prefix}-strict.txt,
        {prefix}-schedule.txt.
        """
        def p(suffix: str) -> str:
            return os.path.join(state_dir, f"{prefix}{suffix}")
        return cls(
            always=p(".txt"),
            active=p("-active.txt"),
            strict=p("-strict.txt"),
            schedule=p("-schedule.txt"),
        )

    def breakdown(
        self,
        is_active: bool = False,
        strict: bool = False,
        now_min: int | None = None,
    ) -> dict[str, frozenset[str]]:
        """Active items per tier: {'always': frozenset, 'active': frozenset, ...}."""
        return {
            "always":   frozenset(read_items(self.always)),
            "active":   frozenset(read_items(self.active)) if is_active else frozenset(),
            "strict":   frozenset(read_items(self.strict)) if strict else frozenset(),
            "schedule": frozenset(active_schedule_items(self.schedule, now_min)),
        }


# Shared mana-bar palette: bar fraction → colour (black → red → yellow → cyan → blue).
COLOR_STOPS = (
    (0.00, 130, 0, 0),
    (0.25, 255, 60, 0),
    (0.50, 255, 220, 0),
    (0.75, 0, 220, 200),
    (1.00, 30, 80, 255),
)


def color_for_fraction(fraction):
    """Interpolated (r, g, b) for a bar fraction, clamped to [0, 1]."""
    fraction = max(0.0, min(1.0, fraction))
    for i in range(len(COLOR_STOPS) - 1):
        lo, r0, g0, b0 = COLOR_STOPS[i]
        hi, r1, g1, b1 = COLOR_STOPS[i + 1]
        if lo <= fraction <= hi:
            t = (fraction - lo) / (hi - lo)
            return (
                int(r0 + t * (r1 - r0)),
                int(g0 + t * (g1 - g0)),
                int(b0 + t * (b1 - b0)),
            )
    return COLOR_STOPS[-1][1:]
