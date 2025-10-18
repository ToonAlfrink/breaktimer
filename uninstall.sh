#!/bin/bash

# This script uninstalls the breaktimer application by removing the
# system-level configuration files.

# Exit immediately if a command exits with a non-zero status.
set -e

# Get the current user.
CURRENT_USER=$(whoami)

# --- Disable and Stop the Service ---
echo "Disabling and stopping the systemd service..."
systemctl --user disable --now breaktimer-terminal.service || echo "Service not found, skipping."
echo "Service disabled and stopped successfully."

# --- Systemd Service File ---
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
SERVICE_FILE="$SYSTEMD_USER_DIR/breaktimer-terminal.service"

echo "Removing systemd service file..."
rm -f "$SERVICE_FILE"
echo "Systemd service file removed successfully."

# --- Sudoers File ---
SUDOERS_FILE="/etc/sudoers.d/99-breaktimer-nopasswd"

echo "Removing sudoers file..."
sudo rm -f "$SUDOERS_FILE"
echo "Sudoers file removed successfully."

# --- i2c setup ---
echo "Reverting i2c configuration..."
sudo gpasswd -d "$CURRENT_USER" i2c || echo "User not in i2c group, skipping."
sudo rm -f /etc/udev/rules.d/45-i2c.rules
sudo sed -i '/i2c-dev/d' /etc/modules
sudo udevadm control --reload-rules
sudo udevadm trigger
echo "i2c configuration reverted successfully."


echo "Uninstallation complete!"
