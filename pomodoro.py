import argparse
import time
import json
import os
import subprocess
import threading
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime
from brightness_control import set_brightness_by_fraction
from mouse_sensitivity_control import set_sensitivity_by_fraction, save_original_sensitivity, restore_original_sensitivity

SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600

TIMER_MAX_SECONDS = SECONDS_PER_HOUR

STATE_FILE = "pomodoro_state.json"
SAVE_INTERVAL_SECONDS = 10


@dataclass
class TimerState:
    """Encapsulates all timer state data."""
    remaining_time: float
    daily_work_totals: dict = field(default_factory=dict)
    last_saved_time: float = None
    is_active: bool = True
    elapsed_since_last_activity: float = 0.0
    
    def to_dict(self):
        """Convert to dictionary for JSON serialization."""
        return {k: v for k, v in asdict(self).items() if v is not None}
    
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

def seconds_to_hours(seconds):
    """Convert seconds to hours."""
    return seconds / SECONDS_PER_HOUR

def format_time(seconds):
    """Format seconds as MM:SS."""
    seconds = int(max(0, seconds))
    minutes, secs = divmod(seconds, SECONDS_PER_MINUTE)
    return f"{minutes}:{secs:02d}"


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
            pass
        finally:
            if self.libinput_process:
                self.libinput_process.terminate()
                self.libinput_process.wait()


def save_state_to_file(state):
    """Saves a cleaned version of the current state to a JSON file directly."""
    state.last_saved_time = time.time()
    state_to_save = state.to_dict()

    with open(STATE_FILE, 'w') as f:
        json.dump(state_to_save, f, indent=4)

def load_state_from_file():
    """Loads the state from a JSON file. Returns None if file not found or error."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            data = json.load(f)
            return TimerState.from_dict(data)
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
    """Execute system shutdown command."""
    shutdown_commands = [
        ['sudo', '-n', 'shutdown', '-h', 'now'],
        ['shutdown', '-h', 'now'],
        ['systemctl', 'poweroff']
    ]
    
    for cmd in shutdown_commands:
        try:
            subprocess.run(cmd, check=True)
            return
        except subprocess.CalledProcessError:
            continue


class TimerLoop:
    """Encapsulates the main timer loop logic, following Single Responsibility Principle."""
    
    ACTIVITY_THRESHOLD_SECONDS = SECONDS_PER_MINUTE
    ADJUSTMENT_INTERVAL_SECONDS = 10
    
    def __init__(self, state, args, offline_duration_seconds, activity_monitor):
        self.state = state
        self.args = args
        self.activity_monitor = activity_monitor
        self.last_loop_time = time.time() - offline_duration_seconds if offline_duration_seconds > 0 else time.time()
        self.last_save_time = time.time()
        self.last_adjustment_time = time.time()
        self.state_lock = threading.Lock()
        self.previous_output_lines = 0
    
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
            self.state.remaining_time += time_since_last_loop
            self.state.remaining_time = min(self.state.remaining_time, TIMER_MAX_SECONDS)
    
    def _check_shutdown(self):
        """Check if timer reached zero and trigger shutdown if needed."""
        if self.state.remaining_time <= 0:
            execute_shutdown()
            return True
        return False
    
    def _update_state(self, current_loop_time, time_since_last_loop):
        """Update timer state based on activity and elapsed time.
        
        Args:
            current_loop_time: Current timestamp
            time_since_last_loop: Seconds since last loop iteration
            
        Returns:
            True if shutdown was triggered, False otherwise
        """
        with self.state_lock:
            self._update_activity_status(current_loop_time, time_since_last_loop)
            self._adjust_timer(time_since_last_loop)
            return self._check_shutdown()
    
    def _apply_adjustments(self, remaining_fraction, current_loop_time):
        """Apply brightness and sensitivity adjustments if interval has elapsed."""
        if current_loop_time - self.last_adjustment_time >= self.ADJUSTMENT_INTERVAL_SECONDS:
            set_brightness_by_fraction(remaining_fraction, TIMER_MAX_SECONDS)
            set_sensitivity_by_fraction(remaining_fraction, TIMER_MAX_SECONDS)
            self.last_adjustment_time = current_loop_time
    
    def _get_color_for_fraction(self, fraction):
        """Get ANSI color code with smooth gradient from blue to green to yellow to red to black."""
        fraction = max(0.0, min(1.0, fraction))
        
        if fraction > 0.75:
            t = (1.0 - fraction) / 0.25
            r = 0
            g = int(t * 255)
            b = int(255 - t * 255)
        elif fraction >= 0.5:
            t = (0.75 - fraction) / 0.25
            r = int(t * 255)
            g = 255
            b = 0
        elif fraction > 0.25:
            t = (0.5 - fraction) / 0.25
            r = 255
            g = int((1 - t) * 255)
            b = 0
        else:
            t = (0.25 - fraction) / 0.25
            r = int((1 - t) * 255)
            g = 0
            b = 0
        
        return f"\033[38;2;{r};{g};{b}m"
    
    def _create_mana_bar(self, remaining, max_time, bar_height):
        """Create a visual mana bar showing remaining time."""
        filled = max(0, min(int((remaining / max_time) * bar_height), bar_height))
        empty = bar_height - filled
        reset = "\033[0m"
        
        fraction = remaining / max_time
        color = self._get_color_for_fraction(fraction)
        
        bar = []
        for i in range(bar_height):
            if i < empty:
                bar.append("░")
            else:
                bar.append(f"{color}█{reset}")
        
        return bar
    
    def _output_status(self):
        """Display current timer status."""
        with self.state_lock:
            remaining = self.state.remaining_time
            is_active = self.state.is_active
        
        terminal_height = os.get_terminal_size().lines
        available_lines = terminal_height - 1
        
        status_icon = "●" if is_active else "○"
        percentage = (remaining / TIMER_MAX_SECONDS) * 100
        time_str = format_time(remaining)
        
        header = [f"{time_str} ({percentage:.1f}%)", status_icon]
        bar_height = max(1, available_lines - len(header))
        mana_bar = self._create_mana_bar(remaining, TIMER_MAX_SECONDS, bar_height)
        
        output = "\n".join(header + mana_bar)
        lines_to_move_up = len(header) + bar_height - 1
        
        print(output, end='', flush=True)
        print(f"\033[{lines_to_move_up}A\r", end='', flush=True)
    
    def run(self):
        """Execute the main timer loop."""
        while True:
            current_loop_time = time.time()
            time_since_last_loop = current_loop_time - self.last_loop_time
            
            if self._update_state(current_loop_time, time_since_last_loop):
                sys.exit(0)
            
            remaining_fraction = self.state.remaining_time / TIMER_MAX_SECONDS
            self._apply_adjustments(remaining_fraction, current_loop_time)
            self._output_status()

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
    return parser.parse_args()

def initialize_state(args):
    """Initialize timer state from file or create new state.
    
    Args:
        args: Parsed command-line arguments
        
    Returns:
        Initialized TimerState instance
    """
    state = load_state_from_file() or TimerState(remaining_time=TIMER_MAX_SECONDS)
    
    saved_time = state.remaining_time
    
    if args.start_minutes is not None:
        new_time = min(args.start_minutes * SECONDS_PER_MINUTE, TIMER_MAX_SECONDS)
        state.remaining_time = new_time
    else:
        state.remaining_time = min(saved_time, TIMER_MAX_SECONDS)
    
    state.is_active = True
    state.elapsed_since_last_activity = 0.0
    
    return state

def main():
    """Main function to run the countdown timer."""
    args = parse_arguments()

    state = initialize_state(args)

    activity_monitor = ActivityMonitor()
    activity_monitor.start()

    save_original_sensitivity()

    offline_duration_seconds = compute_offline_duration_seconds(state)
    gap = offline_duration_seconds if offline_duration_seconds > 0 else 0.0
    if gap > 0:
        activity_monitor.set_last_activity_time(time.time() - gap)

    try:
        timer_loop = TimerLoop(state, args, offline_duration_seconds, activity_monitor)
        timer_loop.run()

    except KeyboardInterrupt:
        save_state_to_file(state)
    except Exception as e:
        raise 
    finally:
        activity_monitor.stop()
        restore_original_sensitivity()

if __name__ == "__main__":
    main() 