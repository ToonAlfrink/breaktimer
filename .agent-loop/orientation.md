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
- `brightness_control.py` — wraps `brightnessctl`/sysfs/ddcutil to set screen brightness.
- `mouse_sensitivity_control.py` — rewrites COSMIC input config files to scale pointer speed.

`test_main.py` is the smoke suite over the shutdown-power core (persistence,
depletion/replenishment arithmetic, shutdown grace window). Run it before and
after any change to `main.py`:

```bash
python3 -m unittest -q
```

No packaging. No CI (the test suite doubles as the loop's health probe).

## Running

```bash
cd ~/Projects/breaktimer
python3 main.py --deplete-minutes 60 --replenish-minutes 20
```

## Health check

`health_cmd` in `projects.yaml` runs the test suite (`python3 -m unittest -q`) —
a failing suite shows up in every dispatch turn's health snapshot.

## Quality gaps

- The `start` script uses `sleep 10` (brittle autostart) and hardcodes geometry guessing.
- `pomodoro_state.json` and `state.sync-conflict-*.json` on disk are stale Syncthing
  artifacts — gitignored, owner's data, leave them.
