#!/bin/bash
# Setup script for external monitor brightness control via ddcutil

set -e

echo "=== External Monitor Brightness Setup ==="
echo ""

# Fix any interrupted dpkg operations first
echo "0. Fixing any interrupted package installations..."
sudo dpkg --configure -a

# Install ddcutil
echo ""
echo "1. Installing ddcutil..."
sudo apt update
sudo apt install -y ddcutil

# Load i2c-dev kernel module
echo ""
echo "2. Loading i2c-dev kernel module..."
sudo modprobe i2c-dev

# Make i2c-dev load on boot
echo ""
echo "3. Configuring i2c-dev to load on boot..."
if ! grep -q "i2c-dev" /etc/modules 2>/dev/null; then
    echo "i2c-dev" | sudo tee -a /etc/modules
fi

# Add user to i2c group (create if it doesn't exist)
echo ""
echo "4. Setting up i2c group permissions..."
if ! getent group i2c >/dev/null; then
    sudo groupadd i2c
fi
sudo usermod -a -G i2c $USER

# Create udev rule for i2c devices
echo ""
echo "5. Creating udev rule for i2c devices..."
echo 'KERNEL=="i2c-[0-9]*", GROUP="i2c", MODE="0660"' | sudo tee /etc/udev/rules.d/45-i2c.rules

# Reload udev rules
echo ""
echo "6. Reloading udev rules..."
sudo udevadm control --reload-rules
sudo udevadm trigger

# Apply permissions to existing i2c devices
echo ""
echo "7. Applying permissions to i2c devices..."
for i2c_dev in /dev/i2c-*; do
    if [ -e "$i2c_dev" ]; then
        sudo chgrp i2c "$i2c_dev"
        sudo chmod 660 "$i2c_dev"
    fi
done

# Test ddcutil
echo ""
echo "8. Testing ddcutil (detecting monitors)..."
echo "   This may take a few seconds..."
ddcutil detect || echo "   Note: No external monitors detected or DDC/CI not supported"

echo ""
echo "=== Setup Complete ==="
echo ""
echo "IMPORTANT: You need to log out and log back in (or reboot) for group"
echo "           changes to take effect."
echo ""
echo "After logging back in, test with:"
echo "  ddcutil detect           # List monitors"
echo "  ddcutil getvcp 10        # Get brightness of first monitor"
echo "  ddcutil setvcp 10 50     # Set brightness to 50%"
echo ""

