#!/bin/bash

# Pomodoro Timer Auto-Launcher for COSMIC Desktop
# This script automatically starts the Pomodoro timer in a terminal window

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="$SCRIPT_DIR/autostart.log"
# Wait for the desktop environment to fully load
sleep 10

# Change to the script directory
cd "$SCRIPT_DIR" || {
    echo "$(date): Failed to change to breaktimer directory" >> "$LOG_FILE"
    exit 1
}

# Function to launch with cosmic-term
launch_cosmic_term() {
    cosmic-term \
        --title="Pomodoro Timer" \
        --working-directory="$SCRIPT_DIR" \
        -- bash -c "
            python3 pomodoro.py || {
                echo 'Pomodoro script failed. Press Enter to close.';
                read;
            }
        " >> "$LOG_FILE" 2>&1
}

# Function to launch with gnome-terminal as fallback
launch_gnome_terminal() {
    # Get screen dimensions for positioning
    if command -v xdpyinfo >/dev/null 2>&1; then
        SCREEN_WIDTH=$(xdpyinfo | awk '/dimensions/{print $2}' | cut -d'x' -f1)
        WINDOW_WIDTH=500
        X_POS=$((SCREEN_WIDTH - WINDOW_WIDTH - 50))
        GEOMETRY_ARG="--geometry=${WINDOW_WIDTH}x20+${X_POS}+50"
    else
        GEOMETRY_ARG=""
    fi

    gnome-terminal \
        $GEOMETRY_ARG \
        --title="Pomodoro Timer" \
        --working-directory="$SCRIPT_DIR" \
        -- bash -c "
            python3 pomodoro.py || {
                echo 'Pomodoro script failed. Press Enter to close.';
                read;
            }
        " >> "$LOG_FILE" 2>&1
}

# Try cosmic-term first, then fallback to gnome-terminal
if command -v cosmic-term >/dev/null 2>&1; then
    echo "$(date): Attempting to launch with cosmic-term" >> "$LOG_FILE"
    launch_cosmic_term
    if [ $? -ne 0 ]; then
        echo "$(date): cosmic-term failed, trying gnome-terminal" >> "$LOG_FILE"
        launch_gnome_terminal
    fi
else
    echo "$(date): cosmic-term not found, using gnome-terminal" >> "$LOG_FILE"
    launch_gnome_terminal
fi

echo "$(date): Pomodoro Timer launch completed" >> "$LOG_FILE"
