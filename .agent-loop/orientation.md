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
  text (time remaining, work history) on hover or when time runs low, and shows the
  60s shutdown-grace countdown.
- **Left-click** the bar to extend by 10 minutes (cancels any active grace window);
  hovering shows a `click: +10 min` hint, and each click flashes a green running
  total (`+10 min`, `+20 min`, …) as instant confirmation.
- Persists state to `state.json` so it survives restarts.
- Managed by two systemd user services that auto-start and restart on crash.

## Architecture

Two independent processes bridged by a live status file:

- `main.py` — the headless timer core (`TimerLoop`, `ActivityMonitor`, `TimerState`).
  No UI of its own; publishes a JSON snapshot every tick for display surfaces. Also
  reads `$XDG_RUNTIME_DIR/breaktimer-command.json` each tick for control commands
  (currently: `extend`).
- `ambient.py` — the always-on strip. GTK3 + GtkLayerShell (system packages
  `gir1.2-gtklayershell-0.1`, `libgtk-layer-shell0`); reads the snapshot at 1 Hz,
  goes grey if the core stops publishing. One bar per monitor; responds to
  monitor-added/removed. Either process can restart without the other.
- `status.py` — the bridge: snapshot read/write in `$XDG_RUNTIME_DIR/breaktimer-status.json`,
  command channel in `$XDG_RUNTIME_DIR/breaktimer-command.json` (both tmpfs — no disk
  churn, gone at logout), per-process singleton locks, and the shared colour palette
  and time formatting.
- `breaktimer` — CLI tool: `status`, `extend [N]`, `restart`.
- `brightness_control.py` — wraps `brightnessctl`/sysfs/ddcutil to set screen brightness.
- `mouse_sensitivity_control.py` — rewrites COSMIC input config files to scale pointer speed.

Runtime logs: `journalctl --user -u breaktimer-{core,ambient}.service`.

## Tests

`test_main.py` covers the shutdown-power core (persistence, depletion/replenishment
arithmetic, shutdown grace window, status publishing, extend command); `test_status.py`
covers the status bridge and command channel. Run before and after any change to the core:

```bash
python3 -m unittest -q
```

No packaging. No CI (the test suite doubles as the loop's health probe — `health_cmd`
in `projects.yaml`).

## Running / control

```bash
cd ~/Projects/breaktimer
./breaktimer status       # show remaining time + today's history
./breaktimer extend [N]   # add N minutes (default 10), cancel grace
./breaktimer restart      # restart both systemd user services
```

Systemd services manage the processes: `breaktimer-core.service` and
`breaktimer-ambient.service` (both `WantedBy=graphical-session.target`, `Restart=always`).

## Quality gaps

- Hover-expansion is wired but was verified by code path, not by a real pointer.
- Click-to-extend on the bar has not been tested against the real compositor (the
  event mask is set but pointer events on a 6px layer-shell window may need verification).
- `pomodoro_state.json` and `state.sync-conflict-*.json` on disk are stale Syncthing
  artifacts — gitignored, owner's data, leave them.
