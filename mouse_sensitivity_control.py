import os
import re

CONFIG_DEFAULT_FILE = os.path.expanduser(
    "~/.config/cosmic/com.system76.CosmicComp/v1/input_default"
)
CONFIG_TOUCHPAD_FILE = os.path.expanduser(
    "~/.config/cosmic/com.system76.CosmicComp/v1/input_touchpad"
)

CONFIG_FILES = (CONFIG_DEFAULT_FILE, CONFIG_TOUCHPAD_FILE)

_original_sensitivity = {}


def _read_speed_from_file(path):
    """Return current speed value from a given COSMIC input config file, or None."""
    if not os.path.exists(path):
        return None

    with open(path, "r") as f:
        content = f.read()

    match = re.search(r"speed:\s*(-?[\d.]+)", content)
    if not match:
        return None

    return round(float(match.group(1)), 2)


def _write_speed_to_file(path, value):
    """Write speed value into a given COSMIC input config file if present."""
    if not os.path.exists(path):
        return

    with open(path, "r") as f:
        content = f.read()

    if "speed:" not in content:
        return

    content = re.sub(r"speed:\s*-?[\d.]+", f"speed: {value}", content)

    with open(path, "w") as f:
        f.write(content)


def set_sensitivity(value):
    """Set speed value in all COSMIC input configs (range -1.0 to 1.0)."""
    value = round(max(-1.0, min(1.0, value)), 2)
    for path in CONFIG_FILES:
        _write_speed_to_file(path, value)


def save_original_sensitivity():
    """Store original sensitivity values for all known input config files."""
    global _original_sensitivity
    _original_sensitivity = {}
    for path in CONFIG_FILES:
        value = _read_speed_from_file(path)
        if value is not None:
            _original_sensitivity[path] = value


def restore_original_sensitivity():
    """Restore saved sensitivity values for all known input config files."""
    for path, value in _original_sensitivity.items():
        _write_speed_to_file(path, value)

def set_sensitivity_by_fraction(fraction):
    """Set sensitivity based on remaining time fraction (0.0 to 1.0)."""
    set_sensitivity(-1.0 + fraction * 2.0)
