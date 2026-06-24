import datetime
import logging
import math
import os
import subprocess
import glob
import threading
import time

import status

log = logging.getLogger("breaktimer.brightness")

# Circadian curve: cosine bell peaking at 13:00 (1 pm), troughing at 01:00 (1 am).
# The floor keeps screens usable at night even with a full bar.
_CIRCADIAN_PEAK_HOUR = 13
_CIRCADIAN_FLOOR = 0.15


def circadian_fraction(hour: float) -> float:
    """Return a [FLOOR, 1.0] multiplier for the time of day.

    Peaks at 13:00 (fully bright), troughs at 01:00 (floor).
    `hour` is a float in [0, 24).
    """
    angle = math.pi * (hour - _CIRCADIAN_PEAK_HOUR) / 12
    return _CIRCADIAN_FLOOR + (1 - _CIRCADIAN_FLOOR) * (1 + math.cos(angle)) / 2

class _ExternalDisplays:
    """The set of DDC/CI-capable monitors, detected once off-thread and cached.

    ddcutil detect is slow (seconds), so detection runs in a background thread
    while readers get [] until it completes. The cache and its lock are the
    object's own fields — the cross-thread state is encapsulated behind detect()
    and get(), not a bare module global mutated through `global`.
    """

    def __init__(self):
        self._cache = None
        self._lock = threading.Lock()

    def detect(self):
        """Kick off ddcutil detect in a background thread so callers never block."""
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        displays = []
        try:
            result = subprocess.run(['ddcutil', 'detect', '--brief'],
                                    capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                for line in result.stdout.split('\n'):
                    if line.strip().startswith('Display'):
                        parts = line.strip().split()
                        if len(parts) >= 2:
                            try:
                                displays.append(int(parts[1]))
                            except ValueError:
                                pass
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        with self._lock:
            self._cache = displays

    def get(self):
        """Cached display numbers ([] until detection completes)."""
        with self._lock:
            return [] if self._cache is None else list(self._cache)


_external_displays = _ExternalDisplays()

# Last percentage actually applied, so the why-it-acted log records each real
# change once instead of re-stating the same level every 10s tick.
_last_applied = None


def start_external_display_detection():
    _external_displays.detect()


def set_brightness(level):
    """Set screen brightness level (0-100)."""
    try:
        subprocess.run(['brightnessctl', 'set', f'{level}%'],
                      capture_output=True, timeout=2, check=True)
        return
    except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
        pass

    try:
        backlight_paths = glob.glob('/sys/class/backlight/*/brightness')
        max_brightness_paths = glob.glob('/sys/class/backlight/*/max_brightness')
        if backlight_paths and max_brightness_paths:
            with open(max_brightness_paths[0], 'r') as f:
                max_brightness = int(f.read().strip())
            brightness_value = int(max_brightness * level / 100)
            with open(backlight_paths[0], 'w') as f:
                f.write(str(brightness_value))
    except (IOError, PermissionError, ValueError):
        pass

def get_external_displays():
    """Return cached list of DDC/CI-capable displays ([] if detection not yet complete)."""
    return _external_displays.get()

def set_external_brightness(display_num, level):
    """Set brightness of an external monitor via ddcutil (0-100)."""
    try:
        subprocess.run(['ddcutil', 'setvcp', '10', str(int(level)), '--display', str(display_num)],
                      capture_output=True, timeout=5, check=True)
    except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
        pass

def pause_until():
    """Epoch seconds until which brightness control is paused (0.0 if not paused)."""
    try:
        with open(status.brightness_pause_path()) as f:
            return float(f.read().strip())
    except (OSError, ValueError):
        return 0.0


def is_paused():
    return time.time() < pause_until()


def pause(seconds, level=100):
    """Suspend brightness adjustments for `seconds`, parking displays at `level`%."""
    status.atomic_write(status.brightness_pause_path(), str(time.time() + seconds))
    _apply_to_all_displays(level)


def unpause():
    """Resume brightness adjustments (the core re-applies within its next tick)."""
    try:
        os.unlink(status.brightness_pause_path())
    except OSError:
        pass


def _apply_to_all_displays(percentage):
    set_brightness(percentage)
    for display_num in get_external_displays():
        set_external_brightness(display_num, percentage)


def set_brightness_by_fraction(fraction):
    """Set all displays' brightness, composing depletion and time-of-day.

    Final brightness = depletion_fraction × circadian_fraction, so a full bar
    at 1 am still dims the screen to the circadian floor rather than blasting
    cold-bright light. No-op while paused (see pause()/unpause()).
    """
    global _last_applied
    if is_paused():
        return
    now = datetime.datetime.now()
    hour = now.hour + now.minute / 60
    circadian = circadian_fraction(hour)
    percentage = max(0, min(100, int(fraction * circadian * 100)))
    if percentage != _last_applied:
        log.info("brightness -> %d%% (bar %d%%, time-of-day %d%%)",
                 percentage, int(fraction * 100), int(circadian * 100))
        _last_applied = percentage
    _apply_to_all_displays(percentage)
