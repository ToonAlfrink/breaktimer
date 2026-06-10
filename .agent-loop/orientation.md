# breaktimer — orientation for agents

A Python app that enforces work-session limits by tracking keyboard/mouse activity
via `libinput`, adjusting screen brightness and mouse sensitivity as time depletes, and
shutting the system down when the "mana bar" reaches zero. The bar lives on an
always-on ambient strip at the top edge of the screen (Wayland layer-shell), not in
a terminal window.

## What it does

- Counts down from a configurable cap (default 60 min) while the user is active.
- Refills passively when idle (activity gap > 1 min).
- Renders a thin full-width mana strip on the top screen edge (panel/dock are at the
  bottom) that changes colour (blue → cyan → yellow → red); it expands with detail
  text (time, % left, work history) on hover or when time runs low, and shows the
  60s shutdown-grace countdown.
- Persists state to `state.json` so it survives restarts.
- Auto-starts via `~/.config/autostart/breaktimer.desktop` → the `start` script.

## Architecture

Two independent processes bridged by a live status file:

- `main.py` — the timer core (`TimerLoop`, `ActivityMonitor`, `TimerState`). Headless
  under autostart; still renders the full-terminal ANSI bar when run in a tty.
  Publishes a JSON snapshot every tick.
- `ambient.py` — the always-on strip. GTK3 + GtkLayerShell (system packages
  `gir1.2-gtklayershell-0.1`, `libgtk-layer-shell0`); reads the snapshot at 1 Hz,
  goes grey if the core stops publishing. Either process can restart without the other.
- `status.py` — the bridge: snapshot read/write in `$XDG_RUNTIME_DIR/breaktimer-status.json`
  (tmpfs — no disk churn, gone at logout), per-process singleton locks
  (`breaktimer-{core,ambient}.lock`), and the shared colour palette / time formatting.
- `brightness_control.py` — wraps `brightnessctl`/sysfs/ddcutil to set screen brightness.
- `mouse_sensitivity_control.py` — rewrites COSMIC input config files to scale pointer speed.

Runtime logs: `~/.local/state/breaktimer/{core,ambient}.log`.

## Tests

`test_main.py` covers the shutdown-power core (persistence, depletion/replenishment
arithmetic, shutdown grace window, status publishing); `test_status.py` covers the
status bridge. Run before and after any change to the core:

```bash
python3 -m unittest -q
```

No packaging. No CI (the test suite doubles as the loop's health probe — `health_cmd`
in `projects.yaml`).

## Running

```bash
cd ~/Projects/breaktimer
./start                  # headless core + ambient bar (idempotent via locks)
python3 main.py          # terminal mode, full ANSI bar
```

## Quality gaps

- The ambient bar appears on the compositor-chosen output only (no multi-monitor story).
- Hover-expansion is wired but was verified by code path, not by a real pointer.
- `pomodoro_state.json` and `state.sync-conflict-*.json` on disk are stale Syncthing
  artifacts — gitignored, owner's data, leave them.
