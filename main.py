import argparse
import logging
import time
import json
import os
import queue
import subprocess
import threading
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
import status
from status import SECONDS_PER_MINUTE, today_str
import app_blocking
import blocklist
import firewall
from brightness_control import set_brightness_by_fraction, start_external_display_detection
from mouse_sensitivity_control import set_sensitivity_by_fraction, read_original_sensitivity, restore_sensitivity

# Why-it-acted trail. A daemon that dims the screen and powers the machine off
# must say why, on the record. Goes to stderr → journald (the systemd services'
# log; see CLAUDE.md `journalctl --user -u breaktimer-core`). Consequential
# actions log here; the control modules log their own overrides under
# breaktimer.{brightness,mouse}.
log = logging.getLogger("breaktimer.core")

_HISTORY_DAYS = 400  # covers 12-month sparkline + buffer

# Phone pings arrive every 2 s while the mobile page is foregrounded.  A ping
# older than this is treated as gone — the phone was backgrounded or the page
# closed.  Generous enough to absorb a missed poll or two, tight enough that
# leaving the phone open on a desk overnight never counts as work.
PHONE_PING_MAX_AGE_SECONDS = 10


def _prune_daily_work_totals(totals):
    """Drop entries older than _HISTORY_DAYS to keep the dict bounded."""
    cutoff = (date.today() - timedelta(days=_HISTORY_DAYS)).isoformat()
    return {d: v for d, v in totals.items() if d >= cutoff}

_XDG_STATE_HOME = os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
STATE_DIR = os.path.join(_XDG_STATE_HOME, "breaktimer")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
SAVE_INTERVAL_SECONDS = 10

# (threshold_seconds, urgency, message) — fired once per descent through each level,
# reset when the timer refills back above the threshold.
_NOTIFY_THRESHOLDS = [
    (600, "normal",   "10 minutes remaining"),
    (300, "normal",   "5 minutes remaining — wrap up soon"),
    (120, "critical", "2 minutes remaining — save your work now"),
]


def _fmt_hours(seconds):
    return f"{seconds / 3600:g}h"


def _notify(body, urgency="normal"):
    """Fire a desktop notification; silently no-ops if notify-send is absent."""
    try:
        subprocess.run(
            ["notify-send", f"--urgency={urgency}", "--app-name=breaktimer", body],
            check=False, timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


@dataclass
class TimerState:
    remaining_time: float
    daily_work_totals: dict = field(default_factory=dict)
    last_saved_time: float = None
    is_active: bool = True

    def to_dict(self):
        """The durable subset, for JSON persistence."""
        return {
            "remaining_time": self.remaining_time,
            "daily_work_totals": _prune_daily_work_totals(self.daily_work_totals),
            "last_saved_time": self.last_saved_time,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            remaining_time=data.get("remaining_time", float("inf")),
            daily_work_totals=_prune_daily_work_totals(data.get("daily_work_totals", {})),
            last_saved_time=data.get("last_saved_time")
        )


class ActivityMonitor:
    def __init__(self):
        self.last_activity_time = time.monotonic()
        self.is_running = False
        self.libinput_process = None
        self.monitor_thread = None
        self.lock = threading.Lock()
        self._healthy = False
        self._stop_event = threading.Event()

    def start(self):
        self.is_running = True
        self.monitor_thread = threading.Thread(target=self._monitor_thread, daemon=True)
        self.monitor_thread.start()

    def stop(self):
        self.is_running = False
        self._stop_event.set()
        if self.libinput_process:
            self.libinput_process.terminate()
            self.libinput_process.wait()

    def is_healthy(self):
        """True while libinput is running and feeding events."""
        return self._healthy

    def get_last_activity_time(self):
        with self.lock:
            return self.last_activity_time

    def set_last_activity_time(self, timestamp):
        with self.lock:
            self.last_activity_time = timestamp

    def _monitor_thread(self):
        backoff = 1
        while self.is_running:
            try:
                self.libinput_process = subprocess.Popen(
                    ['libinput', 'debug-events'],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                )
                self._healthy = True
                backoff = 1

                for line in self.libinput_process.stdout:
                    if not self.is_running:
                        break
                    if line.strip():
                        with self.lock:
                            self.last_activity_time = time.monotonic()

                self.libinput_process.wait()

            except Exception as e:
                log.warning("activity monitor failed: %s", e)
            finally:
                self._healthy = False
                proc, self.libinput_process = self.libinput_process, None
                if proc:
                    try:
                        proc.terminate()
                        proc.wait()
                    except Exception:
                        pass

            if self.is_running:
                self._stop_event.wait(backoff)
                backoff = min(backoff * 2, 60)


def save_state_to_file(state):
    """Saves state atomically (write temp, then rename) — this app powers the
    machine off, so a save interrupted mid-write must not corrupt the file."""
    state.last_saved_time = time.time()
    state_to_save = state.to_dict()

    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    tmp_path = STATE_FILE + ".tmp"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(state_to_save, f, indent=4)
    os.replace(tmp_path, STATE_FILE)

def load_state_from_file():
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, 'r') as f:
            data = json.load(f)
        return TimerState.from_dict(data)
    except (json.JSONDecodeError, ValueError, KeyError) as e:
        log.warning("could not read state file (%s), starting fresh", e)
        return None

def compute_offline_duration_seconds(state):
    if not state.last_saved_time:
        return 0.0
    return max(0.0, time.time() - state.last_saved_time)

def execute_shutdown():
    # Call the logind D-Bus API directly (busctl) — most reliable from a user
    # service since it bypasses systemctl's interactive-session checks while
    # still going through polkit.  Fall through to classic tools on failure.
    shutdown_commands = [
        ['/usr/bin/busctl', 'call',
         'org.freedesktop.login1', '/org/freedesktop/login1',
         'org.freedesktop.login1.Manager', 'PowerOff', 'b', 'false'],
        ['/usr/bin/systemctl', 'poweroff'],
        ['/sbin/shutdown', '-h', 'now'],
    ]

    for cmd in shutdown_commands:
        try:
            subprocess.run(cmd, check=True, timeout=10)
            log.critical("powered off via %s", cmd[0])
            return
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            continue

    log.error("all shutdown commands failed — timer expired but machine is still on")


def run_effect_inline(effect):
    """Default dispatcher: run an effect synchronously, on the caller's thread.
    Used in tests and anywhere determinism matters more than isolation."""
    effect()


class EffectsWorker:
    """Runs slow external side effects (brightness, pointer speed, desktop
    notifications) on a dedicated thread, so a blocking subprocess — ddcutil
    stalling on a misbehaving monitor, notify-send waiting on a busy bus — can
    never delay the 1 Hz heartbeat, status publishing, or the shutdown-grace
    countdown. Effects are fire-and-forget and best-effort: a full queue drops
    the oldest pending job, since the next tick re-submits fresh state anyway."""

    def __init__(self, maxsize=32):
        self._queue = queue.Queue(maxsize)
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def submit(self, effect):
        try:
            self._queue.put_nowait(effect)
        except queue.Full:
            try:
                self._queue.get_nowait()  # drop the stalest job to make room
                self._queue.put_nowait(effect)
            except queue.Empty:
                pass

    def _run(self):
        while True:
            effect = self._queue.get()
            try:
                effect()
            except Exception as e:
                log.warning("effect failed: %s", e)


class TimerLoop:
    """The headless timer core: ticks once a second, adjusts the mana bar,
    publishes the live snapshot, and powers the machine off at zero."""

    ACTIVITY_THRESHOLD_SECONDS = SECONDS_PER_MINUTE
    ADJUSTMENT_INTERVAL_SECONDS = 10
    GRACE_SECONDS = 60
    # A single tick must never meter more than this much time. The loop ticks at
    # 1 Hz; anything larger is a scheduler stall, not real work. Without this a
    # one-off long tick could drain the whole bar at once and trip the shutdown
    # grace. (Monotonic time already shields us from NTP steps and suspend; this
    # is the backstop for everything else, and it always errs toward the user.)
    MAX_TICK_SECONDS = SECONDS_PER_MINUTE

    def __init__(self, state, offline_duration_seconds, activity_monitor, mana_max_seconds,
                 mana_replenish_seconds, daily_budget_seconds, daily_limit_seconds,
                 dispatch=run_effect_inline):
        self.state = state
        self.activity_monitor = activity_monitor
        # How blocking side effects leave the timer thread. Defaults to inline
        # (synchronous, deterministic); production injects EffectsWorker.submit
        # so a slow subprocess can't stall the heartbeat.
        self._dispatch = dispatch
        self.mana_max_seconds = mana_max_seconds
        self.mana_replenish_seconds = mana_replenish_seconds
        self.daily_budget_seconds = daily_budget_seconds
        self.daily_limit_seconds = daily_limit_seconds
        # All in-process timing is monotonic so NTP steps / suspend can't warp it.
        self.last_loop_time = time.monotonic() - offline_duration_seconds
        self.last_save_time = time.monotonic()
        self.last_adjustment_time = time.monotonic()
        self.grace_start = None
        self._status_write_warned = False
        # Pre-populate with thresholds already crossed so a restart below a level
        # doesn't re-fire notifications the user already saw in the previous process.
        self._notified = {
            threshold
            for threshold, _, _ in _NOTIFY_THRESHOLDS
            if self.state.remaining_time <= threshold
        }
        today_total = self.state.daily_work_totals.get(today_str(), 0)
        for name, threshold in (("budget", daily_budget_seconds),
                                ("limit", daily_limit_seconds)):
            if today_total >= threshold:
                self._notified.add(f"daily-{name}-{today_str()}")
    
    def _emit(self, body, urgency="normal"):
        """Fire a desktop notification off the timer thread."""
        self._dispatch(lambda: _notify(body, urgency=urgency))

    def _check_phone_activity(self):
        """If the mobile page sent a recent ping, mark activity as if keyboard fired.

        Symmetric to libinput: the ping feeds set_last_activity_time so the
        timer sees no difference between laptop input and phone browsing.
        A ping older than PHONE_PING_MAX_AGE_SECONDS is ignored — the page was
        backgrounded or the connection dropped; phone use has stopped."""
        last_ping = status.read_phone_ping()
        if last_ping is not None and time.time() - last_ping < PHONE_PING_MAX_AGE_SECONDS:
            self.activity_monitor.set_last_activity_time(time.monotonic())

    def _update_activity_status(self, current_loop_time, time_since_last_loop):
        if not self.activity_monitor.is_healthy():
            # Can't see input — conservatively treat as active so the bar drains.
            self.state.is_active = True
            return

        last_activity_time = self.activity_monitor.get_last_activity_time()
        elapsed = current_loop_time - last_activity_time

        if time_since_last_loop > self.ACTIVITY_THRESHOLD_SECONDS:
            self.activity_monitor.set_last_activity_time(current_loop_time - time_since_last_loop)
            self.state.is_active = False
        else:
            self.state.is_active = elapsed <= self.ACTIVITY_THRESHOLD_SECONDS

    def _check_monitor_health(self):
        """Notify once when libinput goes down or comes back up."""
        if not self.activity_monitor.is_healthy():
            if "monitor-down" not in self._notified:
                self._notified.add("monitor-down")
                log.warning("activity monitor offline — draining at full rate")
                self._emit("Activity monitor offline — draining at full rate", urgency="critical")
        else:
            if "monitor-down" in self._notified:
                self._notified.discard("monitor-down")
                log.info("activity monitor restored")
                self._emit("Activity monitor restored", urgency="normal")
    
    def _refill_multiplier(self):
        """Refill fatigue: 1.0 until the daily budget, decaying linearly to 0.0
        at the daily limit. Past the limit breaks no longer refill, so the bar
        is finite and the day ends when it drains."""
        today_total = self.state.daily_work_totals.get(today_str(), 0)
        if today_total <= self.daily_budget_seconds:
            return 1.0
        span = self.daily_limit_seconds - self.daily_budget_seconds
        return max(0.0, 1.0 - (today_total - self.daily_budget_seconds) / span)

    def _adjust_timer(self, time_since_last_loop):
        today = today_str()
        if self.state.is_active:
            self.state.remaining_time -= time_since_last_loop
            current_day_total = self.state.daily_work_totals.get(today, 0)
            self.state.daily_work_totals[today] = current_day_total + time_since_last_loop
        else:
            rate = (self.mana_max_seconds / self.mana_replenish_seconds) * self._refill_multiplier()
            self.state.remaining_time += time_since_last_loop * rate
            self.state.remaining_time = min(self.state.remaining_time, self.mana_max_seconds)
    
    def _check_shutdown(self):
        if self.state.remaining_time <= 0:
            self.state.remaining_time = 0.0
            now = time.monotonic()
            if self.grace_start is None:
                self.grace_start = now
                cancellable = self._refill_multiplier() > 0
                log.warning(
                    "shutdown grace started: bar empty, %ds to power off (%s)",
                    self.GRACE_SECONDS,
                    "cancellable — go idle to refill" if cancellable
                    else "uncancellable — daily limit reached",
                )
            if now - self.grace_start >= self.GRACE_SECONDS:
                log.critical(
                    "powering off: bar empty through %ds grace; worked %s today, refill at %.0f%%",
                    self.GRACE_SECONDS,
                    _fmt_hours(self.state.daily_work_totals.get(today_str(), 0)),
                    self._refill_multiplier() * 100,
                )
                save_state_to_file(self.state)
                execute_shutdown()
                return True
        else:
            # Timer refilled (user went idle) — cancel any active grace window
            if self.grace_start is not None:
                log.info(
                    "shutdown grace cancelled: bar refilled to %s (went idle)",
                    status.format_time(self.state.remaining_time),
                )
            self.grace_start = None
        return False
    
    def _apply_blocking(self):
        """Dispatch domain/app/firewall blocking every tick (1 Hz) to close the tamper window."""
        is_active = self.state.is_active
        strict = self._refill_multiplier() <= 0
        self._dispatch(lambda: blocklist.apply(is_active=is_active, strict=strict))
        self._dispatch(lambda: app_blocking.apply(is_active=is_active, strict=strict))
        self._dispatch(lambda: firewall.apply(is_active=is_active, strict=strict))

    def _apply_hardware_adjustments(self, remaining_fraction, current_loop_time):
        """Dispatch slow hardware side effects (brightness, sensitivity) every 10 s."""
        if current_loop_time - self.last_adjustment_time >= self.ADJUSTMENT_INTERVAL_SECONDS:
            self._dispatch(lambda: set_brightness_by_fraction(remaining_fraction))
            self._dispatch(lambda: set_sensitivity_by_fraction(remaining_fraction))
            self.last_adjustment_time = current_loop_time
    
    def _grace_remaining(self):
        """Seconds left in the shutdown grace window, or None if not in it."""
        if self.grace_start is None:
            return None
        return max(0.0, self.GRACE_SECONDS - (time.monotonic() - self.grace_start))

    def _check_notifications(self):
        remaining = self.state.remaining_time
        grace = self._grace_remaining()

        if grace is not None:
            if "grace" not in self._notified:
                self._notified.add("grace")
                if self._refill_multiplier() > 0:
                    msg = f"Shutting down in {int(grace) + 1}s — go idle to cancel"
                else:
                    msg = f"Day limit reached — shutting down in {int(grace) + 1}s"
                self._emit(msg, urgency="critical")
        else:
            self._notified.discard("grace")

        for threshold, urgency, msg in _NOTIFY_THRESHOLDS:
            if remaining <= threshold:
                if threshold not in self._notified:
                    self._notified.add(threshold)
                    self._emit(msg, urgency=urgency)
            else:
                self._notified.discard(threshold)

        # Daily fatigue crossings fire once per day; totals only rise within a day.
        today = today_str()
        today_total = self.state.daily_work_totals.get(today, 0)
        for name, threshold, urgency, msg in (
            ("budget", self.daily_budget_seconds, "normal",
             f"{_fmt_hours(self.daily_budget_seconds)} worked today — breaks now refill slower"),
            ("limit", self.daily_limit_seconds, "critical",
             f"{_fmt_hours(self.daily_limit_seconds)} worked today — no refill left, "
             "shutdown when the bar empties"),
        ):
            key = f"daily-{name}-{today}"
            if today_total >= threshold and key not in self._notified:
                self._notified.add(key)
                log.info("daily %s crossed: worked %s today", name, _fmt_hours(today_total))
                self._emit(msg, urgency=urgency)

    def _write_status(self):
        """Publish the live snapshot for ambient surfaces (see status.Snapshot)."""
        snapshot = status.Snapshot(
            remaining_seconds=self.state.remaining_time,
            max_seconds=self.mana_max_seconds,
            is_active=self.state.is_active,
            grace_remaining=self._grace_remaining(),
            refill_rate=self._refill_multiplier(),
            history=status.format_history_line(self.state.daily_work_totals),
        )
        try:
            snapshot.publish()
        except OSError as e:
            if not self._status_write_warned:
                log.warning("cannot publish live status: %s", e)
                self._status_write_warned = True

    def tick(self):
        """Advance the timer by one step: meter time, drive side effects, persist.
        Returns True when the shutdown grace has elapsed and the machine should
        power off. Free of cadence — run() owns the clock — so it's the single
        seam tests drive one step at a time."""
        current_loop_time = time.monotonic()
        time_since_last_loop = current_loop_time - self.last_loop_time

        self._check_phone_activity()
        self._update_activity_status(current_loop_time, time_since_last_loop)
        self._check_monitor_health()
        # Meter at most one bounded step, even if the raw gap was longer (the
        # raw gap above still drives activity detection, so a long gap reads
        # as idle rather than draining).
        self._adjust_timer(min(time_since_last_loop, self.MAX_TICK_SECONDS))
        if self._check_shutdown():
            return True

        remaining_fraction = self.state.remaining_time / self.mana_max_seconds
        self._apply_blocking()
        self._apply_hardware_adjustments(remaining_fraction, current_loop_time)
        self._check_notifications()
        self._write_status()

        if current_loop_time - self.last_save_time >= SAVE_INTERVAL_SECONDS:
            save_state_to_file(self.state)
            self.last_save_time = current_loop_time

        self.last_loop_time = current_loop_time
        return False

    def run(self):
        while True:
            if self.tick():
                sys.exit(0)
            time.sleep(1)


def parse_arguments():
    parser = argparse.ArgumentParser(description="A countdown timer with activity tracking.")
    parser.add_argument(
        "--start-minutes",
        type=float,
        default=None,
        help="Override starting time in minutes (default: starts at --deplete-minutes cap)."
    )
    parser.add_argument(
        "--deplete-minutes",
        type=float,
        default=60,
        help="Bar cap and minutes to deplete from full (X)."
    )
    parser.add_argument(
        "--replenish-minutes",
        type=float,
        default=20,
        help="Minutes to replenish from empty to full (Y)."
    )
    parser.add_argument(
        "--daily-budget-minutes",
        type=float,
        default=480,
        help="Daily work total past which idle refill starts slowing (default 8h)."
    )
    parser.add_argument(
        "--daily-limit-minutes",
        type=float,
        default=600,
        help="Daily work total at which idle refill stops entirely (default 10h)."
    )
    args = parser.parse_args()
    if args.deplete_minutes <= 0 or args.replenish_minutes <= 0:
        parser.error("--deplete-minutes and --replenish-minutes must be positive")
    if not 0 < args.daily_budget_minutes < args.daily_limit_minutes:
        parser.error("--daily-budget-minutes must be positive and below --daily-limit-minutes")
    return args

def initialize_state(args, mana_max_seconds):
    """Load saved state (or start full), clamping remaining time to the bar cap."""
    state = load_state_from_file() or TimerState(remaining_time=mana_max_seconds)
    if args.start_minutes is not None:
        state.remaining_time = args.start_minutes * SECONDS_PER_MINUTE
    state.remaining_time = min(state.remaining_time, mana_max_seconds)
    return state

def main():
    # journald already stamps time and unit, so keep the line lean.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    lock = status.acquire_singleton_lock("core")
    if lock is None:
        log.error("breaktimer core already running — exiting")
        sys.exit(1)

    args = parse_arguments()
    blocklist.blocklist_file          = os.path.join(STATE_DIR, "blocklist.txt")
    blocklist.blocklist_active_file   = os.path.join(STATE_DIR, "blocklist-active.txt")
    blocklist.blocklist_strict_file   = os.path.join(STATE_DIR, "blocklist-strict.txt")
    blocklist.blocklist_schedule_file = os.path.join(STATE_DIR, "blocklist-schedule.txt")
    app_blocking.app_blocklist_file          = os.path.join(STATE_DIR, "blocklist-apps.txt")
    app_blocking.app_blocklist_active_file   = os.path.join(STATE_DIR, "blocklist-apps-active.txt")
    app_blocking.app_blocklist_strict_file   = os.path.join(STATE_DIR, "blocklist-apps-strict.txt")
    app_blocking.app_blocklist_schedule_file = os.path.join(STATE_DIR, "blocklist-apps-schedule.txt")
    mana_max_seconds = args.deplete_minutes * SECONDS_PER_MINUTE
    mana_replenish_seconds = args.replenish_minutes * SECONDS_PER_MINUTE

    state = initialize_state(args, mana_max_seconds)

    activity_monitor = ActivityMonitor()
    activity_monitor.start()
    start_external_display_detection()

    effects = EffectsWorker().start()

    original_sensitivity = read_original_sensitivity()

    # Offline duration is wall-clock (it spans a process/boot gap); fold it into
    # the monotonic activity baseline so the first tick reads as idle downtime.
    offline_duration_seconds = compute_offline_duration_seconds(state)
    activity_monitor.set_last_activity_time(time.monotonic() - offline_duration_seconds)

    try:
        timer_loop = TimerLoop(
            state,
            offline_duration_seconds,
            activity_monitor,
            mana_max_seconds,
            mana_replenish_seconds,
            args.daily_budget_minutes * SECONDS_PER_MINUTE,
            args.daily_limit_minutes * SECONDS_PER_MINUTE,
            dispatch=effects.submit,
        )
        timer_loop.run()

    except KeyboardInterrupt:
        save_state_to_file(state)
    finally:
        activity_monitor.stop()
        restore_sensitivity(original_sensitivity)
        firewall.cleanup()

if __name__ == "__main__":
    main() 