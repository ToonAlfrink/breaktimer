import argparse
import time
from pynput import mouse, keyboard
import json
import os
from datetime import datetime

# Global variable to store the timestamp of the last detected activity
last_activity_time = time.time()

STATE_FILE = "pomodoro_state.json"
SAVE_INTERVAL_SECONDS = 10  # Save state every 10 seconds

def update_last_activity_time(*args):
    """Callback function to update the last_activity_time on any input event."""
    global last_activity_time
    last_activity_time = time.time()

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
    global last_activity_time
    args = parse_arguments()

    print("Pomodoro Timer Started")
    print(f"Work time: {args.work_time} minutes")
    print(f"Break time: {args.break_time} minutes")

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
        else:
            if state["current_mode"] == "work":
                state["remaining_time"] = float(work_time_seconds)
            else: # state["current_mode"] == "break"
                state["remaining_time"] = float(break_time_seconds)
    
    state["is_active"] = True 
    state["elapsed_since_last_activity"] = 0.0

    last_activity_time = time.time() 
    
    work_idle_count_up_rate = (args.work_time * 1.0) / args.break_time
    break_active_count_up_rate = (args.break_time * 1.0) / args.work_time

    mouse_listener = mouse.Listener(
        on_move=update_last_activity_time,
        on_click=update_last_activity_time,
        on_scroll=update_last_activity_time
    )
    keyboard_listener = keyboard.Listener(
        on_press=update_last_activity_time
    )

    mouse_listener.start()
    keyboard_listener.start()

    last_save_time = time.time()

    try:
        while True:
            current_loop_time = time.time()
            today_str = datetime.now().strftime('%Y-%m-%d') # Get current date string
            state["elapsed_since_last_activity"] = current_loop_time - last_activity_time
            IDLE_THRESHOLD_SECONDS = 30
            state["is_active"] = state["elapsed_since_last_activity"] <= IDLE_THRESHOLD_SECONDS
            
            increment_today_work = False
            if state["current_mode"] == "work":
                if state["is_active"]:
                    state["remaining_time"] -= 1
                    increment_today_work = True
                else:
                    state["remaining_time"] += work_idle_count_up_rate
                    state["remaining_time"] = min(state["remaining_time"], max_idle_cap)
            elif state["current_mode"] == "break":
                if state["is_active"]:
                    state["remaining_time"] += break_active_count_up_rate
                    increment_today_work = True # Active breaks still count towards daily total
                else:
                    state["remaining_time"] -= 1
            
            if increment_today_work:
                current_day_total = state["daily_work_totals"].get(today_str, 0)
                state["daily_work_totals"][today_str] = current_day_total + 1

            if state["remaining_time"] <= 0:
                print(" " * 120, end='\\r') 
                if state["current_mode"] == "work":
                    state["current_mode"] = "break"
                    state["remaining_time"] = float(break_time_seconds)
                    print(f"\\nStarting break ({args.break_time} minutes)...")
                elif state["current_mode"] == "break": 
                    state["current_mode"] = "work"
                    state["remaining_time"] = float(work_time_seconds)
                    print(f"\\nStarting work ({args.work_time} minutes)...")
            
            output(state, args)

            if current_loop_time - last_save_time >= SAVE_INTERVAL_SECONDS:
                save_state_to_file(state)
                last_save_time = current_loop_time

            time.sleep(1)

    except KeyboardInterrupt:
        print(" " * 120, end='\\r') 
        print("\\nPomodoro Timer stopped.")
        save_state_to_file(state) 
    finally:
        mouse_listener.stop()
        keyboard_listener.stop()
        print("Input listeners stopped.")

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