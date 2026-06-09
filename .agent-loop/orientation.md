# breaktimer — orientation for agents

A Python terminal app that enforces work-session limits by tracking keyboard/mouse activity
via `libinput`, adjusting screen brightness and mouse sensitivity as time depletes, and
shutting the system down when the "mana bar" reaches zero.

## What it does

- Counts down from a configurable cap (default 60 min) while the user is active.
- Refills passively when idle (activity gap > 1 min).
- Displays a full-terminal ANSI mana bar that changes colour (blue → cyan → yellow → red).
- Persists state to `state.json` so it survives restarts.
- Auto-starts via the `start` bash script (designed for COSMIC/GNOME desktop autostart).

## Stack

Pure Python 3, stdlib only — no pip dependencies. Three modules:
- `main.py` — the entry point; `TimerLoop`, `ActivityMonitor`, `TimerState`.
- `brightness_control.py` — wraps `xrandr`/sysfs to set screen brightness.
- `mouse_sensitivity_control.py` — wraps `xinput` to scale pointer sensitivity.

No tests. No packaging. No CI.

## Running

```bash
cd ~/Projects/breaktimer
python3 main.py --deplete-minutes 60 --replenish-minutes 20
```

## Health check

No health server — this is a standalone CLI tool. Leave `health_cmd` empty in `projects.yaml`.

## Quality gaps

- No test suite.
- `execute_shutdown` tries three commands sequentially with no logging.
- `ActivityMonitor` swallows all exceptions silently.
- `state.json` and `pomodoro_state.json` are both present — the latter appears stale.
- The `start` script uses `sleep 10` (brittle autostart) and hardcodes geometry guessing.
