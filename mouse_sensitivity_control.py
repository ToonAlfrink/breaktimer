import logging
import os
import re

log = logging.getLogger("breaktimer.mouse")

CONFIG_DEFAULT_FILE = os.path.expanduser(
    "~/.config/cosmic/com.system76.CosmicComp/v1/input_default"
)
CONFIG_TOUCHPAD_FILE = os.path.expanduser(
    "~/.config/cosmic/com.system76.CosmicComp/v1/input_touchpad"
)

CONFIG_FILES = (CONFIG_DEFAULT_FILE, CONFIG_TOUCHPAD_FILE)


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

    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(content)
    os.replace(tmp, path)


# Last speed actually written, so the why-it-acted log records each real change
# once rather than re-stating the same value every 10s tick.
_last_value = None


def set_sensitivity(value):
    """Set speed value in all COSMIC input configs (range -1.0 to 1.0)."""
    global _last_value
    value = round(max(-1.0, min(1.0, value)), 2)
    if value != _last_value:
        log.info("pointer speed -> %s", value)
        _last_value = value
    for path in CONFIG_FILES:
        _write_speed_to_file(path, value)


def read_original_sensitivity():
    """Snapshot the user's current speed per config file, for later restore.

    Returns a {path: value} dict the caller owns — no module-level state — so
    the save/restore pair is an explicit value passed by main(), not hidden
    global state mutated across a process lifetime.
    """
    return {
        path: value
        for path in CONFIG_FILES
        if (value := _read_speed_from_file(path)) is not None
    }


def restore_sensitivity(originals):
    for path, value in originals.items():
        _write_speed_to_file(path, value)

def set_sensitivity_by_fraction(fraction):
    """Set sensitivity based on remaining time fraction (0.0 to 1.0)."""
    set_sensitivity(-1.0 + fraction * 2.0)
