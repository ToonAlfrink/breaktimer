import subprocess
import glob

def set_brightness(level):
    """Set screen brightness level (0-100)."""
    if level is None:
        return False
    
    try:
        subprocess.run(['brightnessctl', 'set', f'{level}%'], 
                      capture_output=True, timeout=2, check=True)
        return True
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
            return True
    except (IOError, PermissionError, ValueError):
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

def set_external_brightness(display_num, level):
    """Set brightness of an external monitor via ddcutil (0-100)."""
    if level is None:
        return False
    
    try:
        subprocess.run(['ddcutil', 'setvcp', '10', str(int(level)), '--display', str(display_num)], 
                      capture_output=True, timeout=5, check=True)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
        pass
    
    return False

def set_brightness_by_fraction(fraction):
    """Set all displays' brightness to the remaining-time fraction (0.0 to 1.0)."""
    percentage = max(0, min(100, int(fraction * 100)))
    
    set_brightness(percentage)
    
    external_displays = get_external_displays()
    for display_num in external_displays:
        set_external_brightness(display_num, percentage)
    
    return percentage
