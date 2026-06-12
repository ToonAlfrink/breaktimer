# breaktimer

A Python app that enforces work-session limits by tracking keyboard/mouse activity
via `libinput`, adjusting screen brightness and mouse sensitivity as time depletes, and
shutting the system down when the "mana bar" reaches zero. The bar lives on an
always-on ambient strip at the top edge of the screen (Wayland layer-shell), not in
a terminal window. The limit is unconditional — there is no extend command, no
command channel, no click escape (owner: extending "goes against the spirit of
this tool"); `test_main.py:TestUnconditionalLimit` pins that invariant.

## What it does

- Counts down from a configurable cap (default 60 min) while the user is active.
- Refills passively when idle (activity gap > 1 min), at 3× by default.
- **Refill fatigue — the day has gravity.** Past a daily work budget (default 8h)
  idle refill decays linearly, reaching zero at the daily limit (default 10h).
  Past the limit the bar is finite: when it drains, the grace window can no longer
  be cancelled by going idle and the machine powers off for the day. Days under
  the budget are completely unaffected. (Totals reset at midnight, so next-day
  boot refills normally.)
- Renders a thin full-width mana strip on the top screen edge (panel/dock are at the
  bottom) that changes colour (blue → cyan → yellow → red); it expands with detail
  text (time remaining, work history) on hover, when time runs low, or for the whole
  final stretch once daily refill is gone, and shows the 60s shutdown-grace countdown.
  History text shifts white → amber → red as the day's fatigue sets in.
- Desktop notifications at session thresholds (10/5/2 min) and once per day at the
  budget and limit crossings.
- Persists state to `state.json` so it survives restarts.
- Managed by two systemd user services that auto-start and restart on crash.

## Architecture

Two independent processes bridged by a live status file:

- `main.py` — the headless timer core (`TimerLoop`, `ActivityMonitor`, `TimerState`).
  No UI of its own; publishes a JSON snapshot every tick (including `refill_rate`,
  the current fatigue multiplier) for display surfaces. Daily budget/limit are
  `--daily-budget-minutes` / `--daily-limit-minutes` flags.
- `ambient.py` — the always-on strip. GTK3 + GtkLayerShell (system packages
  `gir1.2-gtklayershell-0.1`, `libgtk-layer-shell0`); reads the snapshot at 1 Hz,
  goes grey if the core stops publishing. One bar per monitor; responds to
  monitor-added/removed. Either process can restart without the other.
- `status.py` — the bridge: snapshot read/write in `$XDG_RUNTIME_DIR/breaktimer-status.json`
  (tmpfs — no disk churn, gone at logout), per-process singleton locks, and the
  shared colour palette and time formatting.
- `breaktimer` — CLI tool: `status`, `brightness off|on`, `restart`.
- `brightness_control.py` — wraps `brightnessctl`/sysfs/ddcutil to set screen brightness.
- `mouse_sensitivity_control.py` — rewrites COSMIC input config files to scale pointer speed.

Runtime logs: `journalctl --user -u breaktimer-{core,ambient}.service`.

## Tests

`test_main.py` covers the shutdown-power core (persistence, depletion/replenishment
arithmetic, refill fatigue, shutdown grace window, notifications, status publishing,
the unconditional-limit invariant); `test_status.py` covers the status bridge;
`test_ambient.py` covers headless bar logic and the service files. Run before and
after any change to the core:

```bash
python3 -m unittest -q
```

No packaging. No CI (the test suite doubles as the loop's health probe — `health_cmd`
in `projects.yaml`).

## Running / control

```bash
cd ~/Projects/breaktimer
./breaktimer status       # remaining time, today's history, fatigue state
./breaktimer restart      # restart both systemd user services
```

Systemd services manage the processes: `breaktimer-core.service` and
`breaktimer-ambient.service` (both `WantedBy=graphical-session.target`, `Restart=always`).

## Quality gaps

- Hover-expansion is wired but was verified by code path, not by a real pointer.
- The post-limit shutdown path (refill gone → bar drains → uncancellable grace →
  poweroff → re-shuts on every login until midnight) is fully pinned by tests
  (`TestRestartAfterShutdown`, `TestExecuteShutdown`, `TestRefillFatigue.test_grace_not_cancellable_past_limit`)
  but has not yet fired on the real machine. The owner's escape while in the 60s grace
  window is `systemctl --user stop breaktimer-core.service`.
- `pomodoro_state.json` and `state.sync-conflict-*.json` on disk are stale Syncthing
  artifacts — gitignored, owner's data, leave them.
