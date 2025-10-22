import argparse
import time
import json
import os
import subprocess
import threading
import glob
import sys
import termios
import tty
from datetime import datetime

# Hardcoded brightness values for work mode
WORK_MODE_LAPTOP_BRIGHTNESS = 19  # Laptop brightness level for work mode
WORK_MODE_EXTERNAL_BRIGHTNESS = 100  # External monitor brightness for work mode

# Lock to protect shared state across threads
state_lock = threading.Lock()
# Flag to pause timer updates and periodic output while prompting
prompt_active = False

# Global rate needed to translate break-time remaining adjustments to work seconds
GLOBAL_BREAK_ACTIVE_RATE = 1.0

# Global variable to store the timestamp of the last detected activity
last_activity_time = time.time()
activity_detection_running = False
libinput_process = None

STATE_FILE = "pomodoro_state.json"
SAVE_INTERVAL_SECONDS = 10  # Save state every 10 seconds
SETTINGS_ENFORCEMENT_INTERVAL_SECONDS = 10  # Apply mode settings every 10 seconds

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

def user_input_handler(state):
    """Background thread to handle immediate '+'/'-' and prompt for minutes.

    On '+' or '-' keypress (no Enter needed), pause timer/output, prompt for a
    number of minutes, apply adjustment, then resume.
    """
    global prompt_active

    stdin_fd = sys.stdin.fileno()
    try:
        old_settings = termios.tcgetattr(stdin_fd)
    except termios.error:
        old_settings = None

    try:
        # Enter cbreak mode to capture single keypresses immediately
        if old_settings is not None:
            tty.setcbreak(stdin_fd)

        while True:
            try:
                ch = sys.stdin.read(1)
            except (KeyboardInterrupt, EOFError):
                break

            if ch not in ('+', '-'):
                continue

            # Pause timer/output immediately
            prompt_active = True
            sign = 1.0 if ch == '+' else -1.0

            # Build simple input line for minutes (digits and '.')
            print("\n\nEnter number of minutes to adjust: ", end='', flush=True)
            buffer = []
            while True:
                try:
                    c = sys.stdin.read(1)
                except (KeyboardInterrupt, EOFError):
                    c = '\n'

                if c in ('\n', '\r'):
                    break
                # Handle backspace (127) and Ctrl+H (8)
                if c and ord(c) in (127, 8):
                    if buffer:
                        buffer.pop()
                        # Erase one character visually
                        print('\b \b', end='', flush=True)
                    continue
                # Accept digits and dot
                if c.isdigit() or c == '.':
                    buffer.append(c)
                    print(c, end='', flush=True)
                # Ignore everything else

            amount_str = ''.join(buffer).strip()
            print("")  # newline after prompt

            if not amount_str:
                print("Adjustment canceled.")
                prompt_active = False
                continue

            try:
                amount_minutes = float(amount_str)
            except ValueError:
                print("Invalid number. Adjustment canceled.")
                prompt_active = False
                continue

            delta_seconds = sign * amount_minutes * 60.0
            with state_lock:
                previous_seconds = float(state.get("remaining_time", 0.0))
                state["remaining_time"] = previous_seconds + delta_seconds
                # Update today's work total based on manual adjustment
                today_str = datetime.now().strftime('%Y-%m-%d')
                current_total = state["daily_work_totals"].get(today_str, 0.0)
                add_work_seconds = 0.0
                if state.get("current_mode") == "work" and delta_seconds < 0:
                    # Reducing work remaining counts as completed work
                    add_work_seconds = -delta_seconds
                elif state.get("current_mode") == "break" and delta_seconds > 0 and GLOBAL_BREAK_ACTIVE_RATE > 0:
                    # Adding to break remaining implies active-break effort
                    add_work_seconds = delta_seconds / GLOBAL_BREAK_ACTIVE_RATE
                if add_work_seconds > 0:
                    state["daily_work_totals"][today_str] = current_total + add_work_seconds

            if delta_seconds >= 0:
                print(f"Added {amount_minutes:g} minutes to the timer.")
            else:
                print(f"Subtracted {abs(amount_minutes):g} minutes from the timer.")

            # Resume timer/output
            prompt_active = False
    finally:
        # Restore terminal settings
        if old_settings is not None:
            try:
                termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_settings)
            except termios.error:
                pass

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

def get_brightness():
    """Get current screen brightness level."""
    try:
        # Try brightnessctl first
        result = subprocess.run(['brightnessctl', 'get'], 
                              capture_output=True, text=True, timeout=2)
        if result.returncode == 0:
            return int(result.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        pass
    
    try:
        # Try reading from sysfs
        backlight_paths = glob.glob('/sys/class/backlight/*/brightness')
        if backlight_paths:
            with open(backlight_paths[0], 'r') as f:
                return int(f.read().strip())
    except (IOError, ValueError):
        pass
    
    return None

def set_brightness(level):
    """Set screen brightness level."""
    if level is None:
        return False
    
    try:
        # Try brightnessctl first
        subprocess.run(['brightnessctl', 'set', str(level)], 
                      capture_output=True, timeout=2, check=True)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
        pass
    
    try:
        # Try writing to sysfs
        backlight_paths = glob.glob('/sys/class/backlight/*/brightness')
        if backlight_paths:
            with open(backlight_paths[0], 'w') as f:
                f.write(str(level))
            return True
    except (IOError, PermissionError):
        pass
    
    return False

def get_external_displays():
    """Get list of external displays that support DDC/CI."""
    displays = []
    try:
        result = subprocess.run(['ddcutil', 'detect', '--brief'], 
                              capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            for line in result.stdout.split('\n'):
                if line.strip().startswith('Display'):
                    # Parse display number from "Display 1" or "Display 2"
                    parts = line.strip().split()
                    if len(parts) >= 2:
                        try:
                            display_num = int(parts[1])
                            displays.append(display_num)
                        except ValueError:
                            pass
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    
    return displays

def get_external_brightness(display_num):
    """Get brightness of an external monitor via ddcutil."""
    try:
        result = subprocess.run(['ddcutil', 'getvcp', '10', '--display', str(display_num), '--brief'], 
                              capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            # Output format: "VCP 10 C 50 100" means current=50, max=100
            parts = result.stdout.strip().split()
            if len(parts) >= 4:
                return int(parts[3])
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        pass
    
    return None

def set_external_brightness(display_num, level):
    """Set brightness of an external monitor via ddcutil."""
    if level is None:
        return False
    
    try:
        subprocess.run(['ddcutil', 'setvcp', '10', str(level), '--display', str(display_num)], 
                      capture_output=True, timeout=5, check=True)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
        pass
    
    return False

def get_intel_pstate_max_perf():
    """Get Intel P-state max performance percentage."""
    try:
        pstate_path = '/sys/devices/system/cpu/intel_pstate/max_perf_pct'
        if os.path.exists(pstate_path):
            with open(pstate_path, 'r') as f:
                return int(f.read().strip())
    except (IOError, PermissionError, ValueError):
        pass
    
    return None

def set_intel_pstate_max_perf(percent):
    """Set Intel P-state max performance percentage."""
    if percent is None:
        return False
    
    try:
        pstate_path = '/sys/devices/system/cpu/intel_pstate/max_perf_pct'
        if os.path.exists(pstate_path):
            subprocess.run(['sudo', '-n', 'tee', pstate_path], 
                         input=str(percent).encode(), 
                         capture_output=True, timeout=2, check=True)
            return True
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
        pass
    
    return False

def get_intel_pstate_no_turbo():
    """Get Intel P-state turbo status."""
    try:
        turbo_path = '/sys/devices/system/cpu/intel_pstate/no_turbo'
        if os.path.exists(turbo_path):
            with open(turbo_path, 'r') as f:
                return int(f.read().strip())
    except (IOError, PermissionError, ValueError):
        pass
    
    return None

def set_intel_pstate_no_turbo(disabled):
    """Set Intel P-state turbo status (1=disabled, 0=enabled)."""
    if disabled is None:
        return False
    
    try:
        turbo_path = '/sys/devices/system/cpu/intel_pstate/no_turbo'
        if os.path.exists(turbo_path):
            subprocess.run(['sudo', '-n', 'tee', turbo_path], 
                         input=str(disabled).encode(), 
                         capture_output=True, timeout=2, check=True)
            return True
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
        pass
    
    return False

def get_cpu_governor():
    """Get current CPU frequency governor."""
    try:
        cpu_path = '/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor'
        if os.path.exists(cpu_path):
            with open(cpu_path, 'r') as f:
                return f.read().strip()
    except (IOError, PermissionError):
        pass
    
    return None

def set_cpu_governor(governor):
    """Set CPU frequency governor for all CPUs."""
    if governor is None:
        return False
    
    try:
        # Try using cpupower
        subprocess.run(['sudo', '-n', 'cpupower', 'frequency-set', '-g', governor], 
                      capture_output=True, timeout=5, check=True)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
        pass
    
    try:
        # Try writing to sysfs for each CPU
        cpu_paths = glob.glob('/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor')
        if cpu_paths:
            for cpu_path in cpu_paths:
                try:
                    subprocess.run(['sudo', '-n', 'tee', cpu_path], 
                                 input=governor.encode(), 
                                 capture_output=True, timeout=2, check=True)
                except:
                    pass
            return True
    except Exception:
        pass
    
    return False

def get_cpu_max_freq():
    """Get current CPU maximum frequency limit."""
    try:
        cpu_path = '/sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq'
        if os.path.exists(cpu_path):
            with open(cpu_path, 'r') as f:
                return int(f.read().strip())
    except (IOError, PermissionError, ValueError):
        pass
    
    return None

def get_cpu_min_freq():
    """Get CPU minimum frequency."""
    try:
        cpu_path = '/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_min_freq'
        if os.path.exists(cpu_path):
            with open(cpu_path, 'r') as f:
                return int(f.read().strip())
    except (IOError, PermissionError, ValueError):
        pass
    
    return None

def set_cpu_max_freq(freq_khz):
    """Set CPU maximum frequency limit for all CPUs."""
    if freq_khz is None:
        return False
    
    try:
        # Write to sysfs for each CPU
        cpu_paths = glob.glob('/sys/devices/system/cpu/cpu*/cpufreq/scaling_max_freq')
        if cpu_paths:
            for cpu_path in cpu_paths:
                try:
                    subprocess.run(['sudo', '-n', 'tee', cpu_path], 
                                 input=str(freq_khz).encode(), 
                                 capture_output=True, timeout=2, check=True)
                except:
                    pass
            return True
    except Exception:
        pass
    
    return False

def apply_mode_settings(mode, verbose=True):
    """Apply system settings for the given mode (work or break).
    
    Args:
        mode: Either "work" or "break"
        verbose: Whether to print status messages (default True)
    """
    if mode == "break":
        # Lower laptop screen brightness
        max_brightness_paths = glob.glob('/sys/class/backlight/*/max_brightness')
        if max_brightness_paths:
            try:
                with open(max_brightness_paths[0], 'r') as f:
                    max_brightness = int(f.read().strip())
                new_brightness = max(int(max_brightness * 0.05), 1)  # 5% of max or 1, whichever is higher
            except:
                new_brightness = 1
        else:
            new_brightness = 1
        
        if set_brightness(new_brightness):
            if verbose:
                print(f"  Laptop brightness lowered to {new_brightness}")
        else:
            if verbose:
                print(f"  Warning: Could not lower laptop brightness")
        
        # Lower external monitor brightness
        external_displays = get_external_displays()
        for display_num in external_displays:
            # Set to minimum brightness (0) for break mode
            if set_external_brightness(display_num, 0):
                if verbose:
                    print(f"  External display {display_num} brightness lowered to 0")
            else:
                if verbose:
                    print(f"  Warning: Could not lower brightness for display {display_num}")
        
        # Throttle CPU using Intel P-state
        # Disable turbo boost
        if set_intel_pstate_no_turbo(1):
            if verbose:
                print(f"  CPU turbo disabled")
        else:
            if verbose:
                print(f"  Warning: Could not disable CPU turbo")
        
        # Set CPU to 0% max performance
        if set_intel_pstate_max_perf(0):
            if verbose:
                print(f"  CPU max performance set to 0%")
        else:
            if verbose:
                print(f"  Warning: Could not set CPU max performance")
    
    elif mode == "work":
        # Restore laptop brightness
        if set_brightness(WORK_MODE_LAPTOP_BRIGHTNESS):
            if verbose:
                print(f"  Laptop brightness restored to {WORK_MODE_LAPTOP_BRIGHTNESS}")
        else:
            if verbose:
                print(f"  Warning: Could not restore laptop brightness")
        
        # Restore external monitor brightness
        external_displays = get_external_displays()
        for display_num in external_displays:
            if set_external_brightness(display_num, WORK_MODE_EXTERNAL_BRIGHTNESS):
                if verbose:
                    print(f"  External display {display_num} brightness restored to {WORK_MODE_EXTERNAL_BRIGHTNESS}")
            else:
                if verbose:
                    print(f"  Warning: Could not restore brightness for display {display_num}")
        
        # Restore CPU to work mode settings
        # Enable turbo boost
        if set_intel_pstate_no_turbo(0):
            if verbose:
                print(f"  CPU turbo enabled")
        else:
            if verbose:
                print(f"  Warning: Could not enable CPU turbo")
        
        # Set CPU to 100% max performance
        if set_intel_pstate_max_perf(100):
            if verbose:
                print(f"  CPU max performance set to 100%")
        else:
            if verbose:
                print(f"  Warning: Could not set CPU max performance")

def apply_settings_async(mode):
    """Apply settings in a background thread (non-blocking)."""
    apply_mode_settings(mode, verbose=False)

def enter_break_mode():
    """Apply system changes when entering break mode."""
    apply_mode_settings("break", verbose=True)

def exit_break_mode():
    """Restore system settings to work mode."""
    apply_mode_settings("work", verbose=True)

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
        default=None,
        choices=["work", "break"],
        help="Starting mode of the timer (default: work if no saved state, otherwise uses saved state)."
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
    if args.start_mode is not None:
        print(f"Start mode: {args.start_mode}")
    else:
        print("Start mode: resuming from saved state (or work if no saved state)")
    if args.start_minutes is not None:
        print(f"Start minutes: {args.start_minutes}")
    print("Using system-wide activity detection (works across all screens/workspaces)")

    work_time_seconds = args.work_time * 60
    break_time_seconds = args.break_time * 60
    max_idle_cap = work_time_seconds * 2 # Maximum time allowed when counting up due to idle

    state = load_state_from_file() # Attempt to load state
    if not state: # If loading failed or no state file
        print("No valid saved state found, starting fresh.")
        # Use provided start_mode or default to "work"
        start_mode = args.start_mode if args.start_mode is not None else "work"
        state = {
            "current_mode": start_mode,
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
        
        # Override start-mode if it was explicitly provided and differs from saved state
        if args.start_mode is not None and state["current_mode"] != args.start_mode:
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
    
    # Apply settings for the starting mode
    if state["current_mode"] == "break":
        print("\nApplying break mode settings...")
        enter_break_mode()
    else:
        print("\nApplying work mode settings...")
        exit_break_mode() 
    
    work_idle_count_up_rate = (args.work_time * 1.0) / args.break_time
    break_active_count_up_rate = (args.break_time * 1.0) / args.work_time
    # Expose break rate globally for input handler computations
    global GLOBAL_BREAK_ACTIVE_RATE
    GLOBAL_BREAK_ACTIVE_RATE = break_active_count_up_rate

    # Start background activity monitoring threads
    activity_detection_running = True
    
    # Start libinput monitor thread
    libinput_thread = threading.Thread(target=libinput_monitor_thread, daemon=True)
    libinput_thread.start()
    
    # Start activity monitor thread
    activity_thread = threading.Thread(target=activity_monitor_thread, daemon=True)
    activity_thread.start()

    # Start user input handler thread for +/- time adjustments
    input_thread = threading.Thread(target=user_input_handler, args=(state,), daemon=True)
    input_thread.start()

    last_save_time = time.time()
    last_loop_time = time.time()
    loop_counter = 0
    
    # Activity detection threshold - used for both idle detection and sleep detection
    ACTIVITY_THRESHOLD_SECONDS = 30

    try:
        while True:
            current_loop_time = time.time()
            time_since_last_loop = current_loop_time - last_loop_time
            
            # If prompting, pause timer updates and output; keep loop time fresh
            if prompt_active:
                last_loop_time = current_loop_time
                time.sleep(0.1)
                continue
            
            # Mutate state under lock to avoid races with the input thread
            with state_lock:
                today_str = datetime.now().strftime('%Y-%m-%d') # Get current date string
                state["elapsed_since_last_activity"] = current_loop_time - last_activity_time
                state["is_active"] = state["elapsed_since_last_activity"] <= ACTIVITY_THRESHOLD_SECONDS
                
                # If we've been sleeping for more than the activity threshold, we likely woke from sleep
                # During sleep, the user was definitely idle, so force idle behavior
                if time_since_last_loop > ACTIVITY_THRESHOLD_SECONDS:
                    print(f"\nResumed after {time_since_last_loop:.1f} seconds (likely from sleep)")
                    state["is_active"] = False
                
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

                    # Calculate how much time went negative (spillover into the next mode)
                    negative_time = abs(state["remaining_time"])

                    if state["current_mode"] == "work":
                        # Switch FROM work TO break
                        spillover_seconds = negative_time
                        state["current_mode"] = "break"

                        if state["is_active"]:
                            # Active spillover into break extends break via active-break rate
                            adjusted_remaining = float(break_time_seconds) + spillover_seconds * break_active_count_up_rate
                            spillover_msg = f"Spillover: +{spillover_seconds:.1f}s to break (active)."
                        else:
                            # Idle spillover into break reduces break directly
                            adjusted_remaining = float(break_time_seconds) - spillover_seconds
                            spillover_msg = f"Spillover: -{spillover_seconds:.1f}s from break (idle)."

                        state["remaining_time"] = adjusted_remaining
                        print(f"\nStarting break ({args.break_time} minutes)...")
                        print(spillover_msg.rjust(120))
                        
                        # Apply break mode system changes
                        enter_break_mode()

                    elif state["current_mode"] == "break":
                        # Switch FROM break TO work
                        spillover_seconds = negative_time
                        state["current_mode"] = "work"

                        if state["is_active"]:
                            # Active spillover into work reduces work directly
                            adjusted_remaining = float(work_time_seconds) - spillover_seconds
                            spillover_msg = f"Spillover: -{spillover_seconds:.1f}s from work (active)."
                        else:
                            # Idle spillover into work increases work via work-idle rate
                            adjusted_remaining = float(work_time_seconds) + spillover_seconds * work_idle_count_up_rate
                            # Cap to the same max used when idling in work
                            adjusted_remaining = min(adjusted_remaining, max_idle_cap)
                            spillover_msg = f"Spillover: +{spillover_seconds:.1f}s to work (idle)."

                        state["remaining_time"] = adjusted_remaining
                        print(f"\nStarting work ({args.work_time} minutes)...")
                        print(spillover_msg.rjust(120))
                        
                        # Restore normal system settings
                        exit_break_mode()
            
            output(state, args)

            # Periodically enforce mode settings (brightness, CPU, etc.)
            if loop_counter % SETTINGS_ENFORCEMENT_INTERVAL_SECONDS == 0:
                # Spawn a thread to apply settings without blocking
                current_mode = state.get("current_mode")
                settings_thread = threading.Thread(target=apply_settings_async, args=(current_mode,), daemon=True)
                settings_thread.start()

            if current_loop_time - last_save_time >= SAVE_INTERVAL_SECONDS:
                save_state_to_file(state)
                last_save_time = current_loop_time

            last_loop_time = current_loop_time
            loop_counter += 1
            time.sleep(1)

    except KeyboardInterrupt:
        print(" " * 120, end='\r') 
        print("\nPomodoro Timer stopped.")
        # Restore settings if we're in break mode
        if state.get("current_mode") == "break":
            print("Restoring system settings...")
            exit_break_mode()
        save_state_to_file(state) 
    finally:
        activity_detection_running = False
        if libinput_process:
            libinput_process.terminate()
        print("Activity monitoring stopped.")

def output(state, args):
    """Handles all per-second console output."""
    # Snapshot state under lock to avoid races with the input thread
    with state_lock:
        display_status_text = state.get("current_mode", "Unknown").capitalize()
        remaining_time_val = state.get("remaining_time", 0)
        elapsed_activity_val = state.get("elapsed_since_last_activity", 0)
        # Get today's work total, defaulting to 0 if not found
        today_str = datetime.now().strftime('%Y-%m-%d')
        total_work_val = state["daily_work_totals"].get(today_str, 0)
    
    # Format time display - show MM:SS but allow minutes to go over 60
    remaining_seconds = int(max(0, remaining_time_val))
    remaining_minutes = remaining_seconds // 60
    remaining_secs = remaining_seconds % 60
    display_time_str = f"{remaining_minutes}:{remaining_secs:02d}"
    
    elapsed_minutes, elapsed_seconds = divmod(int(elapsed_activity_val), 60)
    activity_info = f"{elapsed_minutes:02d}:{elapsed_seconds:02d} Last Activity"

    hours, remainder = divmod(max(0,total_work_val), 3600)
    minutes, _ = divmod(remainder, 60)
    total_work_time_str = f"{int(hours):02d}:{int(minutes):02d} Total Work Today"
    
    activity_status = "Active" if state.get("is_active", True) else "Idle"
    mode_info = f"{display_status_text}"
    status_info = f"{activity_status}"

    output_str = (
        f"\n\n\n{mode_info}\n"
        f"{status_info}\n"
        f"{activity_info}\n"
        f"{total_work_time_str}\n"
        f"{display_time_str} Time Left"
    )
    print(output_str, end='')

if __name__ == "__main__":
    main() 