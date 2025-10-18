#!/bin/bash

# This script sets up the breaktimer application by creating the necessary
# system-level configuration files.

# Exit immediately if a command exits with a non-zero status.
set -e

# Get the directory where the script is located.
SCRIPT_DIR="$(cd \"$(dirname \"${BASH_SOURCE[0]}\")\" && pwd)"

# Get the current user.
CURRENT_USER=$(whoami)

# --- Dependency Installation ---
echo "Installing dependencies..."
sudo apt-get update
sudo apt-get install -y ddcutil brightnessctl
echo "Dependencies installed successfully."

# --- i2c setup ---
echo "Configuring i2c..."
sudo modprobe i2c-dev
if ! grep -q "i2c-dev" /etc/modules 2>/dev/null; then
    echo "i2c-dev" | sudo tee -a /etc/modules
fi
if ! getent group i2c >/dev/null; then
    sudo groupadd i2c
fi
sudo usermod -a -G i2c "$CURRENT_USER"
echo 'KERNEL=="i2c-[0-9]*", GROUP="i2c", MODE="0660"' | sudo tee /etc/udev/rules.d/45-i2c.rules
sudo udevadm control --reload-rules
sudo udevadm trigger
echo "i2c configured successfully."

# --- Sudoers File ---
SUDOERS_FILE="/etc/sudoers.d/99-breaktimer-nopasswd"
SUDOERS_CONTENT="$CURRENT_USER ALL=(ALL) NOPASSWD: ALL"

echo "Creating sudoers file at $SUDOERS_FILE..."
echo "$SUDOERS_CONTENT" | sudo tee "$SUDOERS_FILE" > /dev/null
sudo chmod 0440 "$SUDOERS_FILE"
echo "Sudoers file created successfully."

# --- Systemd Service File ---
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
SERVICE_FILE="$SYSTEMD_USER_DIR/breaktimer-terminal.service"
SERVICE_CONTENT="""[Unit]
Description=Breaktimer (terminal) at login
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Environment="DISPLAY=:0" "XAUTHORITY=%h/.Xauthority"
Type=forking
ExecStart=$SCRIPT_DIR/start_pomodoro.sh
Restart=always
RestartSec=1

[Install]
WantedBy=graphical-session.target
"""

echo "Creating systemd service file at $SERVICE_FILE..."
mkdir -p "$SYSTEMD_USER_DIR"
echo "$SERVICE_CONTENT" > "$SERVICE_FILE"
echo "Systemd service file created successfully."

# --- Enable and Start the Service ---
echo "Reloading systemd daemon and enabling the service..."
systemctl --user daemon-reload
systemctl --user enable --now breaktimer-terminal.service
echo "Service enabled and started successfully."

echo "Installation complete!"
