#!/bin/bash
# Setup script for aggressive CPU throttling during break mode

set -e

echo "=== CPU Throttling Setup ==="
echo ""

# Get CPU frequency info
MIN_FREQ=$(cat /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_min_freq)
MAX_FREQ=$(cat /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq)

echo "CPU Frequency Range:"
echo "  Min: ${MIN_FREQ} kHz ($((MIN_FREQ/1000)) MHz)"
echo "  Max: ${MAX_FREQ} kHz ($((MAX_FREQ/1000)) MHz)"
echo ""

# Update sudoers to allow Intel P-state control
echo "1. Adding Intel P-state control permissions..."
cat <<'EOF' | sudo tee /etc/sudoers.d/pomodoro-cpu
user ALL=(ALL) NOPASSWD: /usr/bin/tee /sys/devices/system/cpu/intel_pstate/max_perf_pct
user ALL=(ALL) NOPASSWD: /usr/bin/tee /sys/devices/system/cpu/intel_pstate/no_turbo
EOF

sudo chmod 0440 /etc/sudoers.d/pomodoro-cpu

echo ""
echo "2. Testing Intel P-state throttling..."

# Save original values
ORIG_MAX_PERF=$(cat /sys/devices/system/cpu/intel_pstate/max_perf_pct)
ORIG_NO_TURBO=$(cat /sys/devices/system/cpu/intel_pstate/no_turbo)

# Test throttling
echo "   Disabling turbo and setting max performance to 0%..."
echo "1" | sudo -n tee /sys/devices/system/cpu/intel_pstate/no_turbo > /dev/null
echo "0" | sudo -n tee /sys/devices/system/cpu/intel_pstate/max_perf_pct > /dev/null

sleep 2
CURRENT_FREQ=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq)
echo "   Current frequency (throttled): ${CURRENT_FREQ} kHz ($((CURRENT_FREQ/1000)) MHz)"

# Restore to original
echo ""
echo "3. Restoring to original settings..."
echo "$ORIG_NO_TURBO" | sudo -n tee /sys/devices/system/cpu/intel_pstate/no_turbo > /dev/null
echo "$ORIG_MAX_PERF" | sudo -n tee /sys/devices/system/cpu/intel_pstate/max_perf_pct > /dev/null

sleep 1
CURRENT_FREQ=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq)
echo "   Current frequency (restored): ${CURRENT_FREQ} kHz ($((CURRENT_FREQ/1000)) MHz)"

echo ""
echo "=== Setup Complete ==="
echo ""
echo "During break mode, CPU will be:"
echo "  - Turbo boost: disabled"
echo "  - Max performance: 0% (minimum Intel P-state allows)"
echo ""

