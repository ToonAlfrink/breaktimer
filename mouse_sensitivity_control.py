import logging
import os
import re

import status

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
    try:
        with open(path) as f:
            content = f.read()
    except OSError:
        return

    if "speed:" not in content:
        return

    content = re.sub(r"speed:\s*-?[\d.]+", f"speed: {value}", content)

    status.atomic_write(path, content)


class MouseController:
    """Manages COSMIC pointer-speed overrides as instance state.

    Tracks the last value written so the why-it-acted log fires once per real
    change rather than repeating the same level every 10 s tick.
    """

    def __init__(self, config_files=CONFIG_FILES):
        self._config_files = config_files
        self._last_value = None

    def set(self, value: float) -> None:
        """Set speed value in all COSMIC input configs (range -1.0 to 1.0)."""
        value = round(max(-1.0, min(1.0, value)), 2)
        if value != self._last_value:
            log.info("pointer speed -> %s", value)
            self._last_value = value
        for path in self._config_files:
            _write_speed_to_file(path, value)

    def set_by_fraction(self, fraction: float) -> None:
        """Set sensitivity based on remaining time fraction (0.0 to 1.0)."""
        self.set(-1.0 + fraction * 2.0)

    def read_originals(self) -> dict:
        """Snapshot the user's current speed per config file, for later restore.

        Returns a {path: value} dict — explicit value passed by main(), not hidden
        module-level state mutated across a process lifetime.
        """
        return {
            path: value
            for path in self._config_files
            if (value := _read_speed_from_file(path)) is not None
        }

    def restore(self, originals: dict) -> None:
        """Restore pointer speeds from a snapshot returned by read_originals()."""
        if originals:
            log.info("pointer speed restored: %s", originals)
        for path, value in originals.items():
            _write_speed_to_file(path, value)
