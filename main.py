import argparse
import time
import json
import os
import subprocess
import threading
import sys
from dataclasses import dataclass, field
from datetime import datetime
import status
from brightness_control import set_brightness_by_fraction
from mouse_sensitivity_control import set_sensitivity_by_fraction, save_original_sensitivity, restore_original_sensitivity

SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600

TIMER_MAX_SECONDS = SECONDS_PER_HOUR

STATE_FILE = "state.json"
SAVE_INTERVAL_SECONDS = 10


@dataclass
class TimerState:
    """Timer state: the durable fields below persist to disk; the activity
    fields are per-tick runtime status and are not saved."""
    remaining_time: float
    daily_work_totals: dict = field(default_factory=dict)
    last_saved_time: float = None
    is_active: bool = True
    elapsed_since_last_activity: float = 0.0

    def to_dict(self):
        """The durable subset, for JSON persistence."""
        return {
            "remaining_time": self.remaining_time,
            "daily_work_totals": self.daily_work_totals,
            "last_saved_time": self.last_saved_time,
        }

    @classmethod
    def from_dict(cls, data):
        """Create from dictionary loaded from JSON."""
        return cls(
            remaining_time=data.get("remaining_time", TIMER_MAX_SECONDS),
            daily_work_totals=data.get("daily_work_totals", {}),
            last_saved_time=data.get("last_saved_time")
        )


def today_str():
    """Returns today's date as YYYY-MM-DD string."""
    return datetime.now().strftime('%Y-%m-%d')


class ActivityMonitor:
    """Encapsulates activity detection via libinput monitoring."""
    
    def __init__(self):
        self.last_activity_time = time.time()
        self.is_running = False
        self.libinput_process = None
        self.monitor_thread = None
        self.lock = threading.Lock()
    
    def start(self):
        """Start the activity monitoring thread."""
        self.is_running = True
        self.monitor_thread = threading.Thread(target=self._monitor_thread, daemon=True)
        self.monitor_thread.start()
    
    def stop(self):
        """Stop the activity monitoring thread."""
        self.is_running = False
        if self.libinput_process:
            self.libinput_process.terminate()
            self.libinput_process.wait()
    
    def get_last_activity_time(self):
        """Get the timestamp of the last detected activity."""
        with self.lock:
            return self.last_activity_time
    
    def set_last_activity_time(self, timestamp):
        """Set the timestamp of the last detected activity."""
        with self.lock:
            self.last_activity_time = timestamp
    
    def _monitor_thread(self):
        """Background thread to continuously monitor libinput events."""
        try:
            self.libinput_process = subprocess.Popen(
                ['libinput', 'debug-events'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                universal_newlines=True
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
    """Loads the state from a JSON file. Returns None if file not found or corrupt."""
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
    """Compute time elapsed since the last persisted state, used to treat downtime as idle.

    Prefers the explicit 'last_saved_time' we persist, with a fallback to the
    filesystem modification time of the state file if that key is missing.
    """
    saved_epoch = state.last_saved_time
    if not saved_epoch and os.path.exists(STATE_FILE):
        saved_epoch = os.path.getmtime(STATE_FILE)
    return max(0.0, time.time() - saved_epoch) if saved_epoch else 0.0

def execute_shutdown():
    """Execute system shutdown command. Logs to stderr if all attempts fail."""
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

    def __init__(self, state, offline_duration_seconds, activity_monitor, mana_max_seconds, mana_replenish_seconds):
        self.state = state
        self.activity_monitor = activity_monitor
        self.mana_max_seconds = mana_max_seconds
        self.mana_replenish_seconds = mana_replenish_seconds
        self.last_loop_time = time.time() - offline_duration_seconds if offline_duration_seconds > 0 else time.time()
        self.last_save_time = time.time()
        self.last_adjustment_time = time.time()
        self.state_lock = threading.Lock()
        self.grace_start = None
        self._status_write_warned = False
    
    def _update_activity_status(self, current_loop_time, time_since_last_loop):
        """Update activity detection status based on user input."""
        last_activity_time = self.activity_monitor.get_last_activity_time()
        self.state.elapsed_since_last_activity = current_loop_time - last_activity_time
        
        if time_since_last_loop > self.ACTIVITY_THRESHOLD_SECONDS:
            self.activity_monitor.set_last_activity_time(current_loop_time - time_since_last_loop)
            self.state.elapsed_since_last_activity = time_since_last_loop
            self.state.is_active = False
        else:
            self.state.is_active = self.state.elapsed_since_last_activity <= self.ACTIVITY_THRESHOLD_SECONDS
    
    def _adjust_timer(self, time_since_last_loop):
        """Adjust remaining time based on activity status."""
        today = today_str()
        if self.state.is_active:
            self.state.remaining_time -= time_since_last_loop
            current_day_total = self.state.daily_work_totals.get(today, 0)
            self.state.daily_work_totals[today] = current_day_total + time_since_last_loop
        else:
            self.state.remaining_time += time_since_last_loop * (self.mana_max_seconds / self.mana_replenish_seconds)
            self.state.remaining_time = min(self.state.remaining_time, self.mana_max_seconds)
    
    def _check_shutdown(self):
        """Check if timer reached zero; enter grace window, then shutdown."""
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
    
    def _update_state(self, current_loop_time, time_since_last_loop):
        """Update timer state for one tick; returns True if shutdown fired."""
        with self.state_lock:
            self._update_activity_status(current_loop_time, time_since_last_loop)
            self._adjust_timer(time_since_last_loop)
            return self._check_shutdown()
    
    def _apply_adjustments(self, remaining_fraction, current_loop_time):
        """Apply brightness and sensitivity adjustments if interval has elapsed."""
        if current_loop_time - self.last_adjustment_time >= self.ADJUSTMENT_INTERVAL_SECONDS:
            set_brightness_by_fraction(remaining_fraction)
            set_sensitivity_by_fraction(remaining_fraction)
            self.last_adjustment_time = current_loop_time
    
    _SPARK_CHARS = "▁▂▃▄▅▆▇█"

    def _format_history_line(self):
        """One line: today's hours, 7-day avg with delta, 28-day sparkline."""
        totals = self.state.daily_work_totals
        today = today_str()
        past_days = sorted(d for d in totals if d < today)

        today_h = totals.get(today, 0) / 3600
        week = past_days[-7:]
        avg_7d = sum(totals[d] for d in week) / len(week) / 3600 if week else 0

        spark_days = past_days[-28:]
        if spark_days:
            vals = [totals[d] for d in spark_days]
            lo, hi = min(vals), max(vals)
            if hi > lo:
                spark = "".join(
                    self._SPARK_CHARS[min(7, int((v - lo) / (hi - lo) * 8))]
                    for v in vals
                )
            else:
                spark = self._SPARK_CHARS[4] * len(spark_days)
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

    def _grace_remaining(self):
        """Seconds left in the shutdown grace window, or None if not in it."""
        if self.grace_start is None:
            return None
        return max(0.0, self.GRACE_SECONDS - (time.time() - self.grace_start))

    def _write_status(self):
        """Publish the live snapshot for ambient surfaces (see status.py)."""
        with self.state_lock:
            payload = {
                "remaining_seconds": self.state.remaining_time,
                "max_seconds": self.mana_max_seconds,
                "is_active": self.state.is_active,
                "grace_remaining": self._grace_remaining(),
                "history": self._format_history_line(),
            }
        try:
            status.write_status(payload)
        except OSError as e:
            if not self._status_write_warned:
                print(f"WARNING: cannot publish live status: {e}", file=sys.stderr)
                self._status_write_warned = True

    def run(self):
        """Execute the main timer loop."""
        while True:
            current_loop_time = time.time()
            time_since_last_loop = current_loop_time - self.last_loop_time
            
            if self._update_state(current_loop_time, time_since_last_loop):
                sys.exit(0)
            
            remaining_fraction = self.state.remaining_time / self.mana_max_seconds
            self._apply_adjustments(remaining_fraction, current_loop_time)
            self._write_status()

            if current_loop_time - self.last_save_time >= SAVE_INTERVAL_SECONDS:
                save_state_to_file(self.state)
                self.last_save_time = current_loop_time

            self.last_loop_time = current_loop_time
            time.sleep(1)


def parse_arguments():
    """Parses command-line arguments for the countdown timer."""
    parser = argparse.ArgumentParser(description="A countdown timer with activity tracking.")
    parser.add_argument(
        "--start-minutes",
        type=float,
        default=None,
        help="Starting remaining time in minutes (default: 60 minutes = 1 hour)."
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
    args = parser.parse_args()
    if args.deplete_minutes <= 0 or args.replenish_minutes <= 0:
        parser.error("--deplete-minutes and --replenish-minutes must be positive")
    return args

def initialize_state(args, mana_max_seconds):
    """Load saved state (or start full), clamping remaining time to the bar cap."""
    state = load_state_from_file() or TimerState(remaining_time=mana_max_seconds)
    if args.start_minutes is not None:
        state.remaining_time = args.start_minutes * SECONDS_PER_MINUTE
    state.remaining_time = min(state.remaining_time, mana_max_seconds)
    return state

def main():
    """Main function to run the countdown timer."""
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
    if offline_duration_seconds > 0:
        activity_monitor.set_last_activity_time(time.time() - offline_duration_seconds)

    try:
        timer_loop = TimerLoop(
            state,
            offline_duration_seconds,
            activity_monitor,
            mana_max_seconds,
            mana_replenish_seconds,
        )
        timer_loop.run()

    except KeyboardInterrupt:
        save_state_to_file(state)
    finally:
        activity_monitor.stop()
        restore_original_sensitivity()

if __name__ == "__main__":
    main() 