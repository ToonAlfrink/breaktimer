"""Live status bridge between the timer core and display surfaces.

The core (main.py) publishes a small JSON snapshot every tick; any UI
(ambient.py, or a future surface) reads it. The file lives in
XDG_RUNTIME_DIR (tmpfs): it is runtime-only state, costs no disk writes,
and vanishes at logout. Either process can restart without the other.
"""
import fcntl
import json
import os
import time
from collections import defaultdict
from datetime import datetime

SECONDS_PER_MINUTE = 60


def _runtime_dir():
    return os.environ.get("XDG_RUNTIME_DIR", "/tmp")


def status_path():
    return os.path.join(_runtime_dir(), "breaktimer-status.json")


def write_status(payload):
    """Atomically write the live status snapshot."""
    path = status_path()
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
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


def acquire_singleton_lock(name):
    """Take an exclusive runtime lock for this process's lifetime.

    Returns the open lock file (keep a reference — the lock dies with it)
    or None if another instance already holds it.
    """
    f = open(os.path.join(_runtime_dir(), f"breaktimer-{name}.lock"), "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except OSError:
        f.close()
        return None


def today_str():
    return datetime.now().strftime('%Y-%m-%d')


def format_time(seconds):
    """Format seconds as M:SS."""
    seconds = int(max(0, seconds))
    minutes, secs = divmod(seconds, SECONDS_PER_MINUTE)
    return f"{minutes}:{secs:02d}"


_SPARK_CHARS = "▁▂▃▄▅▆▇█"


def format_history_line(daily_work_totals):
    """One line: today's hours, 7-day avg with delta, 12-month sparkline."""
    totals = daily_work_totals
    today = today_str()
    today_month = today[:7]
    past_days = sorted(d for d in totals if d < today)

    today_h = totals.get(today, 0) / 3600
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

    parts = [f"{today_h:.1f}h today"]
    if avg_7d:
        diff = today_h - avg_7d
        sign = "+" if diff >= 0 else ""
        parts.append(f"avg {avg_7d:.1f}h  {sign}{diff:.1f}h")
    if spark:
        parts.append(spark)
    return "  ".join(parts)


# Shared mana-bar palette: bar fraction → colour (black → red → yellow → cyan → blue).
COLOR_STOPS = (
    (0.00, 0, 0, 0),
    (0.25, 255, 0, 0),
    (0.50, 255, 255, 0),
    (0.75, 0, 255, 255),
    (1.00, 0, 0, 255),
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
