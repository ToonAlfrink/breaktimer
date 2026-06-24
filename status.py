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
