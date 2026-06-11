import argparse
import time
import json
import os
import subprocess
import threading
import sys
from dataclasses import dataclass, field
import status
from status import SECONDS_PER_MINUTE, today_str
from brightness_control import set_brightness_by_fraction
from mouse_sensitivity_control import set_sensitivity_by_fraction, save_original_sensitivity, restore_original_sensitivity

STATE_FILE = "state.json"
SAVE_INTERVAL_SECONDS = 10

# (threshold_seconds, urgency, message) — fired once per descent through each level,
# reset when the timer refills back above the threshold.
_NOTIFY_THRESHOLDS = [
    (600, "normal",   "10 minutes remaining"),
    (300, "normal",   "5 minutes remaining — wrap up soon"),
    (120, "critical", "2 minutes remaining — save your work now"),
]


def _fmt_hours(seconds):
    return f"{seconds / 3600:g}h"


def _notify(body, urgency="normal"):
    """Fire a desktop notification; silently no-ops if notify-send is absent."""
    try:
        subprocess.run(
            ["notify-send", f"--urgency={urgency}", "--app-name=breaktimer", body],
            check=False, timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


@dataclass
class TimerState:
    remaining_time: float
    daily_work_totals: dict = field(default_factory=dict)
    last_saved_time: float = None
    is_active: bool = True

    def to_dict(self):
        """The durable subset, for JSON persistence."""
        return {
            "remaining_time": self.remaining_time,
            "daily_work_totals": self.daily_work_totals,
            "last_saved_time": self.last_saved_time,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            remaining_time=data.get("remaining_time", float("inf")),
            daily_work_totals=data.get("daily_work_totals", {}),
            last_saved_time=data.get("last_saved_time")
        )


class ActivityMonitor:
    def __init__(self):
        self.last_activity_time = time.time()
        self.is_running = False
        self.libinput_process = None
        self.monitor_thread = None
        self.lock = threading.Lock()
    
    def start(self):
        self.is_running = True
        self.monitor_thread = threading.Thread(target=self._monitor_thread, daemon=True)
        self.monitor_thread.start()
    
    def stop(self):
        self.is_running = False
        if self.libinput_process:
            self.libinput_process.terminate()
            self.libinput_process.wait()
    
    def get_last_activity_time(self):
        with self.lock:
            return self.last_activity_time
    
    def set_last_activity_time(self, timestamp):
        with self.lock:
            self.last_activity_time = timestamp
    
    def _monitor_thread(self):
        try:
            self.libinput_process = subprocess.Popen(
                ['libinput', 'debug-events'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            
            for line in self.libinput_process.stdout:
                if not self.is_running:
                    break
                    
                if line.strip():
                    with self.lock:
                        self.last_activity_time = time.time()
                    
        except Exception as e:
            print(f"\nWARNING: activity monitor failed: {e}", file=sys.stderr)
        finally:
            if self.libinput_process:
                self.libinput_process.terminate()
                self.libinput_process.wait()


def save_state_to_file(state):
    """Saves state atomically (write temp, then rename) — this app powers the
    machine off, so a save interrupted mid-write must not corrupt the file."""
    state.last_saved_time = time.time()
    state_to_save = state.to_dict()

    tmp_path = STATE_FILE + ".tmp"
    with open(tmp_path, 'w') as f:
        json.dump(state_to_save, f, indent=4)
    os.replace(tmp_path, STATE_FILE)

def load_state_from_file():
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, 'r') as f:
            data = json.load(f)
        return TimerState.from_dict(data)
    except (json.JSONDecodeError, ValueError, KeyError) as e:
        print(f"WARNING: could not read state file ({e}), starting fresh", file=sys.stderr)
        return None

def compute_offline_duration_seconds(state):
    if not state.last_saved_time:
        return 0.0
    return max(0.0, time.time() - state.last_saved_time)

def execute_shutdown():
    shutdown_commands = [
        ['sudo', '-n', 'shutdown', '-h', 'now'],
        ['shutdown', '-h', 'now'],
        ['systemctl', 'poweroff']
    ]

    for cmd in shutdown_commands:
        try:
            subprocess.run(cmd, check=True)
            return
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue

    print("ERROR: all shutdown commands failed — timer expired but machine is still on", file=sys.stderr)


class TimerLoop:
    """The headless timer core: ticks once a second, adjusts the mana bar,
    publishes the live snapshot, and powers the machine off at zero."""

    ACTIVITY_THRESHOLD_SECONDS = SECONDS_PER_MINUTE
    ADJUSTMENT_INTERVAL_SECONDS = 10
    GRACE_SECONDS = 60

    def __init__(self, state, offline_duration_seconds, activity_monitor, mana_max_seconds,
                 mana_replenish_seconds, daily_budget_seconds, daily_limit_seconds):
        self.state = state
        self.activity_monitor = activity_monitor
        self.mana_max_seconds = mana_max_seconds
        self.mana_replenish_seconds = mana_replenish_seconds
        self.daily_budget_seconds = daily_budget_seconds
        self.daily_limit_seconds = daily_limit_seconds
        self.last_loop_time = time.time() - offline_duration_seconds
        self.last_save_time = time.time()
        self.last_adjustment_time = time.time()
        self.grace_start = None
        self._status_write_warned = False
        # Pre-populate with thresholds already crossed so a restart below a level
        # doesn't re-fire notifications the user already saw in the previous process.
        self._notified = {
            threshold
            for threshold, _, _ in _NOTIFY_THRESHOLDS
            if self.state.remaining_time <= threshold
        }
        today_total = self.state.daily_work_totals.get(today_str(), 0)
        for name, threshold in (("budget", daily_budget_seconds),
                                ("limit", daily_limit_seconds)):
            if today_total >= threshold:
                self._notified.add(f"daily-{name}-{today_str()}")
    
    def _update_activity_status(self, current_loop_time, time_since_last_loop):
        last_activity_time = self.activity_monitor.get_last_activity_time()
        elapsed = current_loop_time - last_activity_time

        if time_since_last_loop > self.ACTIVITY_THRESHOLD_SECONDS:
            self.activity_monitor.set_last_activity_time(current_loop_time - time_since_last_loop)
            self.state.is_active = False
        else:
            self.state.is_active = elapsed <= self.ACTIVITY_THRESHOLD_SECONDS
    
    def _refill_multiplier(self):
        """Refill fatigue: 1.0 until the daily budget, decaying linearly to 0.0
        at the daily limit. Past the limit breaks no longer refill, so the bar
        is finite and the day ends when it drains."""
        today_total = self.state.daily_work_totals.get(today_str(), 0)
        if today_total <= self.daily_budget_seconds:
            return 1.0
        span = self.daily_limit_seconds - self.daily_budget_seconds
        return max(0.0, 1.0 - (today_total - self.daily_budget_seconds) / span)

    def _adjust_timer(self, time_since_last_loop):
        today = today_str()
        if self.state.is_active:
            self.state.remaining_time -= time_since_last_loop
            current_day_total = self.state.daily_work_totals.get(today, 0)
            self.state.daily_work_totals[today] = current_day_total + time_since_last_loop
        else:
            rate = (self.mana_max_seconds / self.mana_replenish_seconds) * self._refill_multiplier()
            self.state.remaining_time += time_since_last_loop * rate
            self.state.remaining_time = min(self.state.remaining_time, self.mana_max_seconds)
    
    def _check_shutdown(self):
        if self.state.remaining_time <= 0:
            self.state.remaining_time = 0.0
            now = time.time()
            if self.grace_start is None:
                self.grace_start = now
            if now - self.grace_start >= self.GRACE_SECONDS:
                save_state_to_file(self.state)
                execute_shutdown()
                return True
        else:
            # Timer refilled (user went idle) — cancel any active grace window
            self.grace_start = None
        return False
    
    def _apply_adjustments(self, remaining_fraction, current_loop_time):
        if current_loop_time - self.last_adjustment_time >= self.ADJUSTMENT_INTERVAL_SECONDS:
            set_brightness_by_fraction(remaining_fraction)
            set_sensitivity_by_fraction(remaining_fraction)
            self.last_adjustment_time = current_loop_time
    
    def _grace_remaining(self):
        """Seconds left in the shutdown grace window, or None if not in it."""
        if self.grace_start is None:
            return None
        return max(0.0, self.GRACE_SECONDS - (time.time() - self.grace_start))

    def _check_notifications(self):
        remaining = self.state.remaining_time
        grace = self._grace_remaining()

        if grace is not None:
            if "grace" not in self._notified:
                self._notified.add("grace")
                if self._refill_multiplier() > 0:
                    msg = f"Shutting down in {int(grace) + 1}s — go idle to cancel"
                else:
                    msg = f"Day limit reached — shutting down in {int(grace) + 1}s"
                _notify(msg, urgency="critical")
        else:
            self._notified.discard("grace")

        for threshold, urgency, msg in _NOTIFY_THRESHOLDS:
            if remaining <= threshold:
                if threshold not in self._notified:
                    self._notified.add(threshold)
                    _notify(msg, urgency=urgency)
            else:
                self._notified.discard(threshold)

        # Daily fatigue crossings fire once per day; totals only rise within a day.
        today = today_str()
        today_total = self.state.daily_work_totals.get(today, 0)
        for name, threshold, urgency, msg in (
            ("budget", self.daily_budget_seconds, "normal",
             f"{_fmt_hours(self.daily_budget_seconds)} worked today — breaks now refill slower"),
            ("limit", self.daily_limit_seconds, "critical",
             f"{_fmt_hours(self.daily_limit_seconds)} worked today — no refill left, "
             "shutdown when the bar empties"),
        ):
            key = f"daily-{name}-{today}"
            if today_total >= threshold and key not in self._notified:
                self._notified.add(key)
                _notify(msg, urgency=urgency)

    def _write_status(self):
        """Publish the live snapshot for ambient surfaces (see status.py)."""
        payload = {
            "remaining_seconds": self.state.remaining_time,
            "max_seconds": self.mana_max_seconds,
            "is_active": self.state.is_active,
            "grace_remaining": self._grace_remaining(),
            "refill_rate": self._refill_multiplier(),
            "history": status.format_history_line(self.state.daily_work_totals),
        }
        try:
            status.write_status(payload)
        except OSError as e:
            if not self._status_write_warned:
                print(f"WARNING: cannot publish live status: {e}", file=sys.stderr)
                self._status_write_warned = True

    def run(self):
        while True:
            current_loop_time = time.time()
            time_since_last_loop = current_loop_time - self.last_loop_time

            self._update_activity_status(current_loop_time, time_since_last_loop)
            self._adjust_timer(time_since_last_loop)
            if self._check_shutdown():
                sys.exit(0)
            
            remaining_fraction = self.state.remaining_time / self.mana_max_seconds
            self._apply_adjustments(remaining_fraction, current_loop_time)
            self._check_notifications()
            self._write_status()

            if current_loop_time - self.last_save_time >= SAVE_INTERVAL_SECONDS:
                save_state_to_file(self.state)
                self.last_save_time = current_loop_time

            self.last_loop_time = current_loop_time
            time.sleep(1)


def parse_arguments():
    parser = argparse.ArgumentParser(description="A countdown timer with activity tracking.")
    parser.add_argument(
        "--start-minutes",
        type=float,
        default=None,
        help="Override starting time in minutes (default: starts at --deplete-minutes cap)."
    )
    parser.add_argument(
        "--deplete-minutes",
        type=float,
        default=60,
        help="Bar cap and minutes to deplete from full (X)."
    )
    parser.add_argument(
        "--replenish-minutes",
        type=float,
        default=20,
        help="Minutes to replenish from empty to full (Y)."
    )
    parser.add_argument(
        "--daily-budget-minutes",
        type=float,
        default=480,
        help="Daily work total past which idle refill starts slowing (default 8h)."
    )
    parser.add_argument(
        "--daily-limit-minutes",
        type=float,
        default=600,
        help="Daily work total at which idle refill stops entirely (default 10h)."
    )
    args = parser.parse_args()
    if args.deplete_minutes <= 0 or args.replenish_minutes <= 0:
        parser.error("--deplete-minutes and --replenish-minutes must be positive")
    if not 0 < args.daily_budget_minutes < args.daily_limit_minutes:
        parser.error("--daily-budget-minutes must be positive and below --daily-limit-minutes")
    return args

def initialize_state(args, mana_max_seconds):
    """Load saved state (or start full), clamping remaining time to the bar cap."""
    state = load_state_from_file() or TimerState(remaining_time=mana_max_seconds)
    if args.start_minutes is not None:
        state.remaining_time = args.start_minutes * SECONDS_PER_MINUTE
    state.remaining_time = min(state.remaining_time, mana_max_seconds)
    return state

def main():
    lock = status.acquire_singleton_lock("core")
    if lock is None:
        print("breaktimer core already running — exiting", file=sys.stderr)
        sys.exit(1)

    args = parse_arguments()
    mana_max_seconds = args.deplete_minutes * SECONDS_PER_MINUTE
    mana_replenish_seconds = args.replenish_minutes * SECONDS_PER_MINUTE

    state = initialize_state(args, mana_max_seconds)

    activity_monitor = ActivityMonitor()
    activity_monitor.start()

    save_original_sensitivity()

    offline_duration_seconds = compute_offline_duration_seconds(state)
    activity_monitor.set_last_activity_time(time.time() - offline_duration_seconds)

    try:
        timer_loop = TimerLoop(
            state,
            offline_duration_seconds,
            activity_monitor,
            mana_max_seconds,
            mana_replenish_seconds,
            args.daily_budget_minutes * SECONDS_PER_MINUTE,
            args.daily_limit_minutes * SECONDS_PER_MINUTE,
        )
        timer_loop.run()

    except KeyboardInterrupt:
        save_state_to_file(state)
    finally:
        activity_monitor.stop()
        restore_original_sensitivity()

if __name__ == "__main__":
    main() 