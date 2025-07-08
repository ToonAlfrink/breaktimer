import argparse
import time
import json
import os
import subprocess
import threading
import glob
from datetime import datetime

# Global variable to store the timestamp of the last detected activity
last_activity_time = time.time()
activity_detection_running = False
libinput_process = None

STATE_FILE = "pomodoro_state.json"
SAVE_INTERVAL_SECONDS = 10  # Save state every 10 seconds

def get_input_devices():
    """Get list of input devices that can be monitored."""
    devices = []
    try:
        # Look for input devices
        for device in glob.glob('/dev/input/event*'):
            try:
                # Try to get device info
                result = subprocess.run(['udevadm', 'info', '--name=' + device], 
                                      capture_output=True, text=True, timeout=1)
                if result.returncode == 0:
                    # Check if it's a keyboard or mouse
                    if any(keyword in result.stdout.lower() for keyword in ['keyboard', 'mouse', 'pointer']):
                        devices.append(device)
            except (subprocess.TimeoutExpired, subprocess.SubprocessError):
                continue
    except Exception:
        pass
    
    # Fallback: just try common input devices
    if not devices:
        common_devices = ['/dev/input/event0', '/dev/input/event1', '/dev/input/event2']
        for device in common_devices:
            if os.path.exists(device):
                devices.append(device)
    
    return devices

def monitor_input_activity():
    """Monitor input activity using file modification times."""
    global last_activity_time
    
    # Get input devices
    devices = get_input_devices()
    if not devices:
        return False
    
    try:
        # Check if any input device has been modified recently
        current_time = time.time()
        for device in devices:
            try:
                mtime = os.path.getmtime(device)
                if current_time - mtime < 2:  # If device was accessed in last 2 seconds
                    last_activity_time = current_time
                    return True
            except (OSError, IOError):
                continue
    except Exception:
        pass
    
    return False

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

def get_system_idle_time():
    """Get system idle time using various methods."""
    global last_activity_time
    
    # Calculate idle time based on last_activity_time
    current_time = time.time()
    idle_time = current_time - last_activity_time
    return idle_time

def update_last_activity_time():
    """Update the last activity time based on system idle detection."""
    global last_activity_time
    idle_time = get_system_idle_time()
    # No debug output needed here

def activity_monitor_thread():
    """Background thread to continuously monitor system activity."""
    global activity_detection_running
    while activity_detection_running:
        update_last_activity_time()
        time.sleep(1)  # Check every second

def save_state_to_file(state):
    """Saves a cleaned version of the current state to a JSON file directly."""
    state_to_save = state.copy()
    # Remove ephemeral or deprecated keys before saving
    state_to_save.pop('is_active', None)
    state_to_save.pop('elapsed_since_last_activity', None)
    state_to_save.pop('total_work_today_seconds', None) # Deprecated, remove if present

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

def parse_arguments():
    """Parses command-line arguments for the Pomodoro timer."""
    parser = argparse.ArgumentParser(description="A Pomodoro timer with activity tracking.")
    parser.add_argument(
        "--work-time",
        type=int,
        default=30,
        help="Duration of a work session in minutes (default: 30)."
    )
    parser.add_argument(
        "--break-time",
        type=int,
        default=30,
        help="Duration of a break in minutes (default: 30)."
    )
    parser.add_argument(
        "--start-mode",
        type=str,
        default="work",
        choices=["work", "break"],
        help="Starting mode of the timer (default: work)."
    )
    parser.add_argument(
        "--start-minutes",
        type=float,
        default=None, # Default to None, so we can use full work/break time if not specified
        help="Starting remaining time in minutes. Overrides the default full duration for the chosen start-mode."
    )
    return parser.parse_args()

def main():
    """Main function to run the Pomodoro timer."""
    global last_activity_time, activity_detection_running
    args = parse_arguments()

    print("Pomodoro Timer Started")
    print(f"Work time: {args.work_time} minutes")
    print(f"Break time: {args.break_time} minutes")
    print(f"Start mode: {args.start_mode}")
    if args.start_minutes is not None:
        print(f"Start minutes: {args.start_minutes}")
    print("Using system-wide activity detection (works across all screens/workspaces)")

    work_time_seconds = args.work_time * 60
    break_time_seconds = args.break_time * 60
    max_idle_cap = work_time_seconds * 2 # Maximum time allowed when counting up due to idle

    state = load_state_from_file() # Attempt to load state
    if not state: # If loading failed or no state file
        print("No valid saved state found, starting fresh.")
        state = {
            "current_mode": args.start_mode,
            "remaining_time": 0.0,
            "daily_work_totals": {}
        }
        if args.start_minutes is not None:
            state["remaining_time"] = args.start_minutes * 60.0
            print(f"Starting with {args.start_minutes} minutes remaining")
        else:
            if state["current_mode"] == "work":
                state["remaining_time"] = float(work_time_seconds)
            else: # state["current_mode"] == "break"
                state["remaining_time"] = float(break_time_seconds)
    else:
        # If we loaded a state, apply command-line overrides
        original_mode = state["current_mode"]
        original_time = state["remaining_time"] / 60.0
        
        # Override start-mode if it differs from saved state
        if state["current_mode"] != args.start_mode:
            state["current_mode"] = args.start_mode
            print(f"Overriding saved mode '{original_mode}' with '{args.start_mode}'")
        
        # Override remaining time if start-minutes was specified
        if args.start_minutes is not None:
            state["remaining_time"] = args.start_minutes * 60.0
            print(f"Overriding saved time ({original_time:.1f} min) with {args.start_minutes} minutes")
        else:
            print(f"Loaded saved state: {original_mode} mode with {original_time:.1f} minutes remaining")
    
    state["is_active"] = True 
    state["elapsed_since_last_activity"] = 0.0

    last_activity_time = time.time() 
    
    work_idle_count_up_rate = (args.work_time * 1.0) / args.break_time
    break_active_count_up_rate = (args.break_time * 1.0) / args.work_time

    # Start background activity monitoring threads
    activity_detection_running = True
    
    # Start libinput monitor thread
    libinput_thread = threading.Thread(target=libinput_monitor_thread, daemon=True)
    libinput_thread.start()
    
    # Start activity monitor thread
    activity_thread = threading.Thread(target=activity_monitor_thread, daemon=True)
    activity_thread.start()

    last_save_time = time.time()
    last_loop_time = time.time()  # Track when we last ran the loop

    try:
        while True:
            current_loop_time = time.time()
            time_since_last_loop = current_loop_time - last_loop_time
            
            # If we've been sleeping for more than 5 seconds, we likely woke from sleep
            if time_since_last_loop > 5:
                print(f"\nResumed after {time_since_last_loop:.1f} seconds (likely from sleep)")
            
            today_str = datetime.now().strftime('%Y-%m-%d') # Get current date string
            state["elapsed_since_last_activity"] = current_loop_time - last_activity_time
            IDLE_THRESHOLD_SECONDS = 30
            state["is_active"] = state["elapsed_since_last_activity"] <= IDLE_THRESHOLD_SECONDS
            
            increment_today_work = False
            if state["current_mode"] == "work":
                if state["is_active"]:
                    state["remaining_time"] -= time_since_last_loop
                    increment_today_work = True
                else:
                    state["remaining_time"] += work_idle_count_up_rate * time_since_last_loop
                    state["remaining_time"] = min(state["remaining_time"], max_idle_cap)
            elif state["current_mode"] == "break":
                if state["is_active"]:
                    state["remaining_time"] += break_active_count_up_rate * time_since_last_loop
                    increment_today_work = True # Active breaks still count towards daily total
                else:
                    state["remaining_time"] -= time_since_last_loop
            
            if increment_today_work:
                current_day_total = state["daily_work_totals"].get(today_str, 0)
                state["daily_work_totals"][today_str] = current_day_total + time_since_last_loop

            if state["remaining_time"] <= 0:
                print(" " * 120, end='\r') 
                
                # Calculate how much time went negative
                negative_time = abs(state["remaining_time"])
                
                if state["current_mode"] == "work":
                    state["current_mode"] = "break"
                    state["remaining_time"] = float(break_time_seconds)
                    print(f"\nStarting break ({args.break_time} minutes)...")
                    
                    # If we went negative, add extra work time that should have been added
                    if negative_time > 0:
                        extra_work_time = negative_time * work_idle_count_up_rate
                        state["remaining_time"] += extra_work_time
                        state["remaining_time"] = min(state["remaining_time"], max_idle_cap)
                        print(f"Adjusted for {negative_time:.1f}s of extra work time during sleep")
                        
                elif state["current_mode"] == "break": 
                    state["current_mode"] = "work"
                    state["remaining_time"] = float(work_time_seconds)
                    print(f"\nStarting work ({args.work_time} minutes)...")
                    
                    # If we went negative, subtract extra break time that should have been added
                    if negative_time > 0:
                        extra_break_time = negative_time * break_active_count_up_rate
                        state["remaining_time"] -= extra_break_time
                        print(f"Adjusted for {negative_time:.1f}s of extra break time during sleep")
            
            output(state, args)

            if current_loop_time - last_save_time >= SAVE_INTERVAL_SECONDS:
                save_state_to_file(state)
                last_save_time = current_loop_time

            last_loop_time = current_loop_time
            time.sleep(1)

    except KeyboardInterrupt:
        print(" " * 120, end='\r') 
        print("\nPomodoro Timer stopped.")
        save_state_to_file(state) 
    finally:
        activity_detection_running = False
        if libinput_process:
            libinput_process.terminate()
        print("Activity monitoring stopped.")

def output(state, args):
    """Handles all per-second console output."""
    display_status_text = state.get("current_mode", "Unknown").capitalize()
    remaining_time_val = state.get("remaining_time", 0)
    elapsed_activity_val = state.get("elapsed_since_last_activity", 0)
    today_str = datetime.now().strftime('%Y-%m-%d')
    total_work_val = state["daily_work_totals"][today_str]
    

    display_time_str = time.strftime('%M:%S', time.gmtime(int(max(0, remaining_time_val))))
    
    elapsed_minutes, elapsed_seconds = divmod(int(elapsed_activity_val), 60)
    activity_info = f"Last Activity: {elapsed_minutes:02d}:{elapsed_seconds:02d}"

    hours, remainder = divmod(max(0,total_work_val), 3600)
    minutes, _ = divmod(remainder, 60)
    total_work_time_str = f"{int(hours):02d}:{int(minutes):02d}"
    
    activity_status = "Active" if state.get("is_active", True) else "Idle"

    output_str = (
        f"Mode: {display_status_text} ({activity_status})\n"
        f"Time Left: {display_time_str}\n"
        f"{activity_info}\n"
        f"Total Work Today: {total_work_time_str}"
    )
    print(output_str)

if __name__ == "__main__":
    main() 