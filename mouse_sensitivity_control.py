import os
import re

CONFIG_FILE = os.path.expanduser("~/.config/cosmic/com.system76.CosmicComp/v1/input_default")
_original_sensitivity = None

def get_current_sensitivity():
    """Get current speed value from Pop OS config file."""
    if not os.path.exists(CONFIG_FILE):
        raise RuntimeError(f"Config file not found: {CONFIG_FILE}")
    
    with open(CONFIG_FILE, 'r') as f:
        content = f.read()
    
    match = re.search(r'speed:\s*(-?[\d.]+)', content)
    if not match:
        raise RuntimeError("Could not find speed value in config file")
    
    return round(float(match.group(1)), 2)

def set_sensitivity(value):
    """Set speed value in Pop OS config file (range -1.0 to 1.0)."""
    value = round(max(-1.0, min(1.0, value)), 2)
    
    if not os.path.exists(CONFIG_FILE):
        raise RuntimeError(f"Config file not found: {CONFIG_FILE}")
    
    with open(CONFIG_FILE, 'r') as f:
        content = f.read()
    
    content = re.sub(r'speed:\s*-?[\d.]+', f'speed: {value}', content)
    
    with open(CONFIG_FILE, 'w') as f:
        f.write(content)

def save_original_sensitivity():
    """Store original sensitivity value from config file."""
    global _original_sensitivity
    _original_sensitivity = get_current_sensitivity()

def restore_original_sensitivity():
    """Restore saved sensitivity value."""
    if _original_sensitivity is not None:
        set_sensitivity(_original_sensitivity)

def set_sensitivity_by_fraction(fraction, max_time_seconds):
    """Set sensitivity based on remaining time fraction.
    
    Args:
        fraction: Remaining time as a fraction of max_time_seconds (0.0 to 1.0)
        max_time_seconds: Maximum timer value in seconds
    
    Returns:
        The sensitivity value that was set (-1.0 to 1.0)
    """
    sensitivity = -1.0 + (fraction * 2.0)
    sensitivity = max(-1.0, min(1.0, sensitivity))
    set_sensitivity(sensitivity)
    return sensitivity
