import argparse
import time
import json
import os
import subprocess
import threading
import sys
from datetime import datetime
from brightness_control import set_brightness_by_fraction
from mouse_sensitivity_control import set_sensitivity_by_fraction, save_original_sensitivity, restore_original_sensitivity

# Lock to protect shared state across threads
state_lock = threading.Lock()

TIMER_MAX_SECONDS = 3600.0

# Global variable to store the timestamp of the last detected activity
last_activity_time = time.time()
activity_detection_running = False
libinput_process = None

STATE_FILE = "pomodoro_state.json"
SHUTDOWN_TRIGGER_FILE = "shutdown_trigger.json"
SAVE_INTERVAL_SECONDS = 10

# Shutdown enforcement: Once daily work exceeds this limit, a shutdown trigger is written to disk.
# The system will shutdown after the grace period, even if the script is killed and restarted.
DAILY_WORK_LIMIT_SECONDS_DEFAULT = 8 * 60 * 60  # 8 hours (default, can be overridden via --daily-work-limit)
SHUTDOWN_GRACE_PERIOD_SECONDS = 10 * 60  # 10 minutes

 

def libinput_monitor_thread():
    """Background thread to continuously monitor libinput events."""
    global last_activity_time, activity_detection_running, libinput_process
    
    try:
        # Start libinput debug-events process
        libinput_process = subprocess.Popen(
            ['libinput', 'debug-events'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            universal_newlines=True
        )
        
        # Read output line by line
        for line in libinput_process.stdout:
            if not activity_detection_running:
                break
                
            # Any line from libinput means activity
            if line.strip():
                last_activity_time = time.time()
                
    except Exception as e:
        print(f"Warning: libinput monitoring failed: {e}")
    finally:
        if libinput_process:
            libinput_process.terminate()
            libinput_process.wait()

 

def save_state_to_file(state):
    """Saves a cleaned version of the current state to a JSON file directly."""
    state_to_save = state.copy()
    # Remove ephemeral or deprecated keys before saving
    state_to_save.pop('is_active', None)
    state_to_save.pop('elapsed_since_last_activity', None)
    state_to_save.pop('total_work_today_seconds', None)
    state_to_save.pop('current_mode', None)
    # Persist a wall-clock timestamp to account for offline time across restarts
    state_to_save['last_saved_time'] = time.time()

    with open(STATE_FILE, 'w') as f:
        json.dump(state_to_save, f, indent=4)
    # No error handling

def load_state_from_file():
    """Loads the state from a JSON file. Returns None if file not found or error."""
    if os.path.exists(STATE_FILE):
        # Attempt to load; will crash on error if file is corrupted
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    return None # File not found

def compute_offline_duration_seconds(state):
    """Compute time elapsed since the last persisted state, used to treat downtime as idle.

    Prefers the explicit 'last_saved_time' we persist, with a fallback to the
    filesystem modification time of the state file if that key is missing.
    """
    try:
        saved_epoch = state.get('last_saved_time')
    except Exception:
        saved_epoch = None
    if not saved_epoch:
        try:
            saved_epoch = os.path.getmtime(STATE_FILE) if os.path.exists(STATE_FILE) else None
        except Exception:
            saved_epoch = None
    return max(0.0, time.time() - saved_epoch) if saved_epoch else 0.0

def check_and_set_shutdown_trigger(total_work_seconds, daily_work_limit_seconds):
    """Check if work time exceeds limit and set shutdown trigger if needed.
    
    Args:
        total_work_seconds: Total work time in seconds for today
        daily_work_limit_seconds: Daily work limit in seconds
    
    Returns the shutdown timestamp if trigger is active, None otherwise.
    """
    today_str = datetime.now().strftime('%Y-%m-%d')
    current_time = time.time()
    
    # Check if shutdown trigger file exists
    if os.path.exists(SHUTDOWN_TRIGGER_FILE):
        try:
            with open(SHUTDOWN_TRIGGER_FILE, 'r') as f:
                trigger_data = json.load(f)
                # Check if it's for today
                if trigger_data.get('date') == today_str:
                    return trigger_data.get('shutdown_time')
        except (json.JSONDecodeError, IOError):
            pass
    
    # If total work exceeds limit and no trigger exists, create it
    if total_work_seconds >= daily_work_limit_seconds:
        shutdown_time = current_time + SHUTDOWN_GRACE_PERIOD_SECONDS
        trigger_data = {
            'date': today_str,
            'shutdown_time': shutdown_time,
            'work_time_when_triggered': total_work_seconds,
            'shutdown_count': 0
        }
        try:
            with open(SHUTDOWN_TRIGGER_FILE, 'w') as f:
                json.dump(trigger_data, f, indent=4)
            # Print warning when trigger is first set
            limit_hours = daily_work_limit_seconds / 3600
            print("\n" + "="*60)
            print(f"⚠️  {limit_hours:.0f}-HOUR WORK LIMIT EXCEEDED ⚠️")
            print(f"System will shutdown in {SHUTDOWN_GRACE_PERIOD_SECONDS // 60} minutes")
            print("This shutdown CANNOT be canceled by killing the script")
            print("="*60 + "\n")
            return shutdown_time
        except IOError:
            pass
    
    return None

def reset_shutdown_trigger():
    """Reset the shutdown trigger to give a new 5-minute window after shutdown."""
    today_str = datetime.now().strftime('%Y-%m-%d')
    current_time = time.time()
    
    if os.path.exists(SHUTDOWN_TRIGGER_FILE):
        try:
            with open(SHUTDOWN_TRIGGER_FILE, 'r') as f:
                trigger_data = json.load(f)
            
            # Only reset if it's still today's trigger
            if trigger_data.get('date') == today_str:
                new_shutdown_time = current_time + SHUTDOWN_GRACE_PERIOD_SECONDS
                trigger_data['shutdown_time'] = new_shutdown_time
                trigger_data['shutdown_count'] = trigger_data.get('shutdown_count', 0) + 1
                
                with open(SHUTDOWN_TRIGGER_FILE, 'w') as f:
                    json.dump(trigger_data, f, indent=4)
        except (json.JSONDecodeError, IOError):
            pass

def execute_shutdown(daily_work_limit_seconds):
    """Execute system shutdown command.
    
    Args:
        daily_work_limit_seconds: Daily work limit in seconds (for display)
    """
    # Reset the trigger before shutting down so we get a new 5-minute window on reboot
    reset_shutdown_trigger()
    
    limit_hours = daily_work_limit_seconds / 3600
    print("\n" + "="*60)
    print(f"SHUTTING DOWN: {limit_hours:.0f}-hour work limit exceeded")
    print("You will have another 5 minutes after restart")
    print("="*60)
    try:
        subprocess.run(['sudo', '-n', 'shutdown', '-h', 'now'], check=True)
    except subprocess.CalledProcessError:
        # If passwordless sudo doesn't work, try regular shutdown
        try:
            subprocess.run(['shutdown', '-h', 'now'], check=True)
        except subprocess.CalledProcessError:
            # Last resort: try systemctl
            try:
                subprocess.run(['systemctl', 'poweroff'], check=True)
            except subprocess.CalledProcessError:
                print("ERROR: Could not execute shutdown command")
                print("Please run: sudo shutdown -h now")


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
        "--daily-work-limit",
        type=float,
        default=None,
        help="Daily work limit in hours (default: 8). Once exceeded, system will shutdown after grace period."
    )
    return parser.parse_args()

def main():
    """Main function to run the countdown timer."""
    global last_activity_time, activity_detection_running
    args = parse_arguments()

    daily_work_limit_seconds = (args.daily_work_limit * 60 * 60) if args.daily_work_limit is not None else DAILY_WORK_LIMIT_SECONDS_DEFAULT
    
    print("Countdown Timer Started")
    print(f"Timer starts at: 1 hour (3600 seconds)")
    print(f"Daily work limit: {daily_work_limit_seconds / 3600:.1f} hours")
    if args.start_minutes is not None:
        print(f"Starting with {args.start_minutes} minutes")
    print("Using system-wide activity detection (works across all screens/workspaces)")

    state = load_state_from_file()
    if not state:
        print("No valid saved state found, starting fresh.")
        state = {
            "remaining_time": TIMER_MAX_SECONDS,
            "daily_work_totals": {}
        }
        if args.start_minutes is not None:
            state["remaining_time"] = min(args.start_minutes * 60.0, TIMER_MAX_SECONDS)
            print(f"Starting with {args.start_minutes} minutes remaining")
    else:
        original_time = state["remaining_time"] / 60.0
        
        if "current_mode" in state:
            del state["current_mode"]
        
        if args.start_minutes is not None:
            state["remaining_time"] = min(args.start_minutes * 60.0, TIMER_MAX_SECONDS)
            print(f"Overriding saved time ({original_time:.1f} min) with {args.start_minutes} minutes")
        else:
            state["remaining_time"] = min(state.get("remaining_time", TIMER_MAX_SECONDS), TIMER_MAX_SECONDS)
            print(f"Loaded saved state: {state['remaining_time'] / 60.0:.1f} minutes remaining")
    
    state["is_active"] = True 
    state["elapsed_since_last_activity"] = 0.0

    last_activity_time = time.time()

    # Start background activity monitoring threads
    activity_detection_running = True
    
    # Start libinput monitor thread
    libinput_thread = threading.Thread(target=libinput_monitor_thread, daemon=True)
    libinput_thread.start()

    save_original_sensitivity()

    last_save_time = time.time()
    last_adjustment_time = time.time()
    # Harmonized handling: treat both system sleep and powered-off downtime as the same gap
    offline_duration_seconds = compute_offline_duration_seconds(state)
    gap = offline_duration_seconds if offline_duration_seconds > 0 else 0.0
    # Seed both the loop baseline and last activity with the gap so the first iteration
    # applies the entire downtime as idle, and the UI reflects accurate 'Last Activity'
    if gap > 0:
        last_activity_time = time.time() - gap
        last_loop_time = time.time() - gap
    else:
        last_loop_time = time.time()
    loop_counter = 0
    
    # Activity detection threshold - used for both idle detection and sleep detection
    ACTIVITY_THRESHOLD_SECONDS = 60
    
    # Check for existing shutdown trigger on startup
    today_str = datetime.now().strftime('%Y-%m-%d')
    total_work_today = state["daily_work_totals"].get(today_str, 0)
    shutdown_time = check_and_set_shutdown_trigger(total_work_today, daily_work_limit_seconds)
    if shutdown_time:
        time_until_shutdown = shutdown_time - time.time()
        if time_until_shutdown <= 0:
            # If time has already passed, shutdown immediately
            execute_shutdown(daily_work_limit_seconds)
            sys.exit(0)
        else:
            limit_hours = daily_work_limit_seconds / 3600
            print(f"\nWARNING: Shutdown scheduled in {int(time_until_shutdown)} seconds!")
            print(f"{limit_hours:.0f}-hour work limit has been exceeded.\n")

    try:
        while True:
            current_loop_time = time.time()
            time_since_last_loop = current_loop_time - last_loop_time
            
            # Check shutdown trigger at every iteration (in case script was killed and restarted)
            today_str = datetime.now().strftime('%Y-%m-%d')
            total_work_today = state["daily_work_totals"].get(today_str, 0)
            shutdown_time = check_and_set_shutdown_trigger(total_work_today, daily_work_limit_seconds)
            if shutdown_time and time.time() >= shutdown_time:
                print("\n" * 5)
                execute_shutdown(daily_work_limit_seconds)
                sys.exit(0)
            
            # Mutate state under lock
            with state_lock:
                today_str = datetime.now().strftime('%Y-%m-%d') # Get current date string
                state["elapsed_since_last_activity"] = current_loop_time - last_activity_time
                state["is_active"] = state["elapsed_since_last_activity"] <= ACTIVITY_THRESHOLD_SECONDS
                
                # If we've been away for longer than the threshold, treat it as a gap
                # (covers both system sleep and powered-off downtime) and force idle
                if time_since_last_loop > ACTIVITY_THRESHOLD_SECONDS:
                    print(f"\nResumed after {time_since_last_loop:.1f} seconds (sleep or downtime)")
                    # Align last activity with the start of the gap so the UI reflects it
                    last_activity_time = current_loop_time - time_since_last_loop
                    state["elapsed_since_last_activity"] = time_since_last_loop
                    state["is_active"] = False
                
                if state["is_active"]:
                    state["remaining_time"] -= time_since_last_loop
                    current_day_total = state["daily_work_totals"].get(today_str, 0)
                    state["daily_work_totals"][today_str] = current_day_total + time_since_last_loop
                    
                    new_total = state["daily_work_totals"][today_str]
                    shutdown_time = check_and_set_shutdown_trigger(new_total, daily_work_limit_seconds)
                    if shutdown_time and time.time() >= shutdown_time:
                        print("\n" * 5)
                        execute_shutdown(daily_work_limit_seconds)
                        sys.exit(0)
                else:
                    state["remaining_time"] += time_since_last_loop
                    state["remaining_time"] = min(state["remaining_time"], TIMER_MAX_SECONDS)

                if state["remaining_time"] <= 0:
                    print("\n" * 5)
                    print("="*60)
                    print("⚠️  TIMER REACHED ZERO ⚠️")
                    print("Shutting down system...")
                    print("="*60)
                    execute_shutdown(daily_work_limit_seconds)
                    sys.exit(0)
                
                remaining_time_snapshot = state["remaining_time"]
            
            remaining_fraction = remaining_time_snapshot / TIMER_MAX_SECONDS
            
            if current_loop_time - last_adjustment_time >= 10:
                brightness_percentage = set_brightness_by_fraction(remaining_fraction, TIMER_MAX_SECONDS)
                sensitivity = set_sensitivity_by_fraction(remaining_fraction, TIMER_MAX_SECONDS)
                
                print(f"\n[Adjustment] Brightness: {brightness_percentage}%, Mouse Sensitivity: {sensitivity:.2f}")
                
                last_adjustment_time = current_loop_time
            
            output(state, args, daily_work_limit_seconds)

            if current_loop_time - last_save_time >= SAVE_INTERVAL_SECONDS:
                save_state_to_file(state)
                last_save_time = current_loop_time

            last_loop_time = current_loop_time
            loop_counter += 1
            time.sleep(1)

    except KeyboardInterrupt:
        print(" " * 120, end='\r') 
        print("\nCountdown Timer stopped.")
        save_state_to_file(state)
    except Exception as e:
        print(f"\nUnexpected error: {e}")
        raise 
    finally:
        activity_detection_running = False
        if libinput_process:
            libinput_process.terminate()
        print("Activity monitoring stopped.")
        restore_original_sensitivity()

def output(state, args, daily_work_limit_seconds):
    """Handles all per-second console output.
    
    Args:
        state: Current timer state
        args: Command-line arguments
        daily_work_limit_seconds: Daily work limit in seconds
    """
    with state_lock:
        remaining_time_val = state.get("remaining_time", 0)
        elapsed_activity_val = state.get("elapsed_since_last_activity", 0)
        today_str = datetime.now().strftime('%Y-%m-%d')
        total_work_val = state["daily_work_totals"].get(today_str, 0)
    
    remaining_seconds = int(max(0, remaining_time_val))
    remaining_minutes = remaining_seconds // 60
    remaining_secs = remaining_seconds % 60
    display_time_str = f"{remaining_minutes}:{remaining_secs:02d}"
    
    elapsed_minutes, elapsed_seconds = divmod(int(elapsed_activity_val), 60)
    activity_info = f"{elapsed_minutes:02d}:{elapsed_seconds:02d} Last Activity"

    hours, remainder = divmod(max(0, total_work_val), 3600)
    minutes, _ = divmod(remainder, 60)
    total_work_time_str = f"{int(hours):02d}:{int(minutes):02d} Total Work Today"
    
    activity_status = "Active" if state.get("is_active", True) else "Idle"

    output_str = (
        f"\n\n{activity_status}\n"
        f"{activity_info}\n"
        f"{total_work_time_str}\n"
        f"{display_time_str} Time Left"
    )
    print(output_str, end='')

if __name__ == "__main__":
    main() 