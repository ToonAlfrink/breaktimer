"""Tests for ambient bar logic that does not require a live GTK/Wayland session."""
import configparser
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

import ambient
from ambient import AmbientBar, BarManager, EXPAND_SECONDS, WARN_SECONDS
from status import Snapshot


class TestWarningText(unittest.TestCase):
    def test_grace_mode_shows_countdown(self):
        s = Snapshot(grace_remaining=45.0, remaining_seconds=0)
        text = AmbientBar._warning_text(s)
        self.assertIn("SHUTTING DOWN", text)
        self.assertIn("0:45", text)
        self.assertIn("go idle", text)

    def test_under_2min_shows_save_warning(self):
        s = Snapshot(remaining_seconds=90, grace_remaining=None)
        text = AmbientBar._warning_text(s)
        self.assertIn("save your work", text)
        self.assertNotIn("wrap up", text)

    def test_between_2_and_warn_shows_wrap_up(self):
        s = Snapshot(remaining_seconds=3 * 60, grace_remaining=None)
        text = AmbientBar._warning_text(s)
        self.assertIn("wrap up soon", text)
        self.assertNotIn("save your work", text)

    def test_above_warn_threshold_returns_none(self):
        s = Snapshot(remaining_seconds=WARN_SECONDS + 1, grace_remaining=None)
        self.assertIsNone(AmbientBar._warning_text(s))

    def test_grace_takes_priority_over_remaining(self):
        # even with remaining_seconds > 0, grace_remaining drives the message
        s = Snapshot(remaining_seconds=500, grace_remaining=30.0)
        text = AmbientBar._warning_text(s)
        self.assertIn("SHUTTING DOWN", text)


def _detached_bar(hovered=False, brightness_pause_until=0.0):
    """A minimal stand-in wired to the real AmbientBar methods, no GTK needed."""
    class FakeBar:
        pass
    bar = FakeBar()
    bar.hovered = hovered
    bar.brightness_pause_until = brightness_pause_until
    bar._center_text = AmbientBar._center_text.__get__(bar, FakeBar)
    bar._warning_text = AmbientBar._warning_text
    return bar


class TestCenterText(unittest.TestCase):
    CALM = Snapshot(remaining_seconds=50 * 60, grace_remaining=None)
    WARN = Snapshot(remaining_seconds=90, grace_remaining=None)

    def test_empty_when_calm_and_unhovered(self):
        self.assertEqual(_detached_bar()._center_text(self.CALM), (None, None))

    def test_empty_when_calm_and_hovered(self):
        self.assertEqual(_detached_bar(hovered=True)._center_text(self.CALM), (None, None))

    def test_warning_shows_when_hovered(self):
        text, _ = _detached_bar(hovered=True)._center_text(self.WARN)
        self.assertIn("save your work", text)

    def test_day_limit_notice_when_refill_gone(self):
        s = Snapshot(remaining_seconds=50 * 60, grace_remaining=None, refill_rate=0.0)
        text, _ = _detached_bar()._center_text(s)
        self.assertIn("day limit reached", text)

    def test_warning_takes_priority_over_day_limit_notice(self):
        s = Snapshot(remaining_seconds=90, grace_remaining=None, refill_rate=0.0)
        text, _ = _detached_bar()._center_text(s)
        self.assertIn("save your work", text)

    def test_no_notice_while_refill_merely_slowed(self):
        s = Snapshot(remaining_seconds=50 * 60, grace_remaining=None, refill_rate=0.5)
        self.assertEqual(_detached_bar()._center_text(s), (None, None))


class TestDayFatigueDisplay(unittest.TestCase):
    def test_grace_text_honest_when_no_refill(self):
        s = Snapshot(grace_remaining=45.0, remaining_seconds=0, refill_rate=0.0)
        text = AmbientBar._warning_text(s)
        self.assertIn("DAY LIMIT", text)
        self.assertNotIn("go idle", text)

    def test_grace_text_offers_idle_escape_with_refill(self):
        s = Snapshot(grace_remaining=45.0, remaining_seconds=0, refill_rate=1.0)
        self.assertIn("go idle", AmbientBar._warning_text(s))

    def test_history_white_within_budget(self):
        self.assertEqual(AmbientBar._history_rgb(Snapshot(refill_rate=1.0)), (255, 255, 255))

    def test_history_amber_past_budget(self):
        self.assertEqual(AmbientBar._history_rgb(Snapshot(refill_rate=0.5)), (255, 190, 80))

    def test_history_red_at_limit(self):
        self.assertEqual(AmbientBar._history_rgb(Snapshot(refill_rate=0.0)), (255, 80, 80))

    def test_history_white_by_default(self):
        # refill_rate defaults to 1.0 (no fatigue) when a snapshot omits it.
        self.assertEqual(AmbientBar._history_rgb(Snapshot()), (255, 255, 255))


class TestBrightnessPauseDisplay(unittest.TestCase):
    CALM = Snapshot(remaining_seconds=50 * 60, grace_remaining=None)

    def test_pause_indicator_shows_when_paused(self):
        bar = _detached_bar(brightness_pause_until=time.time() + 3600)
        text, rgb = bar._center_text(self.CALM)
        self.assertIn("☀", text)
        self.assertIn("brightness paused", text)

    def test_pause_indicator_includes_time_remaining(self):
        bar = _detached_bar(brightness_pause_until=time.time() + 3600)
        text, _ = bar._center_text(self.CALM)
        self.assertRegex(text, r"\d+:\d+")

    def test_no_indicator_when_not_paused(self):
        bar = _detached_bar(brightness_pause_until=time.time() - 1)
        self.assertEqual(bar._center_text(self.CALM), (None, None))

    def test_no_indicator_when_never_paused(self):
        bar = _detached_bar(brightness_pause_until=0.0)
        self.assertEqual(bar._center_text(self.CALM), (None, None))

    def test_warning_takes_priority_over_pause(self):
        warn = Snapshot(remaining_seconds=90, grace_remaining=None)
        bar = _detached_bar(brightness_pause_until=time.time() + 3600)
        text, _ = bar._center_text(warn)
        self.assertIn("save your work", text)
        self.assertNotIn("brightness paused", text)

    def test_day_limit_takes_priority_over_pause(self):
        limit = Snapshot(remaining_seconds=50 * 60, grace_remaining=None, refill_rate=0.0)
        bar = _detached_bar(brightness_pause_until=time.time() + 3600)
        text, _ = bar._center_text(limit)
        self.assertIn("day limit reached", text)
        self.assertNotIn("brightness paused", text)

    def test_pause_color_is_calm_blue(self):
        bar = _detached_bar(brightness_pause_until=time.time() + 3600)
        _, rgb = bar._center_text(self.CALM)
        r, g, b = rgb
        self.assertGreater(b, r)


class TestIsCritical(unittest.TestCase):
    def _bar_with_snapshot(self, snapshot):
        """Return a minimal stand-in that delegates is_critical to the real method."""
        class FakeBar:
            pass
        bar = FakeBar()
        bar.snapshot = snapshot
        bar.is_critical = AmbientBar.is_critical.__get__(bar, FakeBar)
        return bar

    def test_no_snapshot_not_critical(self):
        bar = self._bar_with_snapshot(None)
        self.assertFalse(bar.is_critical())

    def test_in_grace_is_critical(self):
        bar = self._bar_with_snapshot(Snapshot(grace_remaining=40.0, remaining_seconds=0))
        self.assertTrue(bar.is_critical())

    def test_below_expand_threshold_is_critical(self):
        bar = self._bar_with_snapshot(Snapshot(grace_remaining=None, remaining_seconds=EXPAND_SECONDS - 1))
        self.assertTrue(bar.is_critical())

    def test_above_expand_threshold_not_critical(self):
        bar = self._bar_with_snapshot(Snapshot(grace_remaining=None, remaining_seconds=EXPAND_SECONDS + 1))
        self.assertFalse(bar.is_critical())

    def test_no_refill_left_is_critical(self):
        # past the daily limit the bar stays expanded for its final stretch
        bar = self._bar_with_snapshot(Snapshot(grace_remaining=None,
                                               remaining_seconds=EXPAND_SECONDS + 1,
                                               refill_rate=0.0))
        self.assertTrue(bar.is_critical())


class TestFillBar(unittest.TestCase):
    """Smoke-test the bar rendering on an offscreen surface (no GTK/Wayland)."""

    def _render(self, fraction, active=True, w=200, h=6):
        import cairo
        import status
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, max(1, w), h)
        cr = cairo.Context(surf)
        AmbientBar._fill_bar(cr, w, h, fraction,
                             status.color_for_fraction(fraction), active)
        surf.flush()
        return surf

    def test_renders_across_fill_levels_without_error(self):
        for f in (1.0, 0.5, 0.1, 0.0):
            for active in (True, False):
                self._render(f, active)  # must not raise

    def test_empty_bar_paints_only_the_ghost_rail(self):
        # fraction 0 still draws the dim full-width rail, never the lit gradient
        self._render(0.0)  # must not raise on zero-width fill

    def test_narrow_strip_does_not_overflow(self):
        self._render(0.5, w=4, h=6)  # leading glow must clamp to fill width


class TestBarManager(unittest.TestCase):
    def _setup(self):
        created = []

        class FakeBar:
            def __init__(self, monitor):
                self.monitor = monitor
                self.destroyed = False

            def destroy(self):
                self.destroyed = True

        def factory(monitor):
            bar = FakeBar(monitor)
            created.append(bar)
            return bar

        return BarManager(factory), created

    def test_add_creates_bar(self):
        mgr, created = self._setup()
        mgr.add("mon1")
        self.assertEqual(len(mgr._bars), 1)
        self.assertEqual(len(created), 1)

    def test_add_same_monitor_twice_is_idempotent(self):
        mgr, created = self._setup()
        mgr.add("mon1")
        mgr.add("mon1")
        self.assertEqual(len(mgr._bars), 1)
        self.assertEqual(len(created), 1)

    def test_add_multiple_monitors(self):
        mgr, created = self._setup()
        mgr.add("mon1")
        mgr.add("mon2")
        self.assertEqual(len(mgr._bars), 2)
        self.assertEqual(len(created), 2)

    def test_remove_destroys_bar_and_decrements_count(self):
        mgr, created = self._setup()
        mgr.add("mon1")
        mgr.remove("mon1")
        self.assertTrue(created[0].destroyed)
        self.assertEqual(len(mgr._bars), 0)

    def test_remove_nonexistent_is_noop(self):
        mgr, _ = self._setup()
        mgr.remove("never-added")  # must not raise
        self.assertEqual(len(mgr._bars), 0)

    def test_remove_all_monitors_does_not_quit(self):
        # Regression: removing the last monitor used to call Gtk.main_quit(),
        # killing the process. A reconnected monitor would get no bar without a
        # full restart. BarManager must survive zero-bar state.
        mgr, _ = self._setup()
        mgr.add("mon1")
        mgr.remove("mon1")
        self.assertEqual(len(mgr._bars), 0)
        # If we get here, Gtk.main_quit() was NOT called (no GTK is running).

    def test_reconnected_monitor_gets_new_bar(self):
        mgr, created = self._setup()
        mgr.add("mon1")
        mgr.remove("mon1")
        mgr.add("mon1")
        self.assertEqual(len(mgr._bars), 1)
        self.assertEqual(len(created), 2)
        self.assertFalse(created[1].destroyed)


class TestWaitForWayland(unittest.TestCase):
    def test_returns_true_immediately_if_socket_exists(self):
        with tempfile.TemporaryDirectory() as d:
            socket = os.path.join(d, "wayland-0")
            open(socket, "w").close()
            with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": d, "WAYLAND_DISPLAY": "wayland-0"}):
                self.assertTrue(ambient._wait_for_wayland(timeout_seconds=1))

    def test_returns_false_on_timeout(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": d, "WAYLAND_DISPLAY": "wayland-0"}):
                start = time.monotonic()
                self.assertFalse(ambient._wait_for_wayland(timeout_seconds=0.2))
                self.assertLess(time.monotonic() - start, 1.5)

    def test_waits_until_socket_appears(self):
        with tempfile.TemporaryDirectory() as d:
            socket = os.path.join(d, "wayland-0")

            def _create():
                time.sleep(0.3)
                open(socket, "w").close()

            threading.Thread(target=_create, daemon=True).start()
            with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": d, "WAYLAND_DISPLAY": "wayland-0"}):
                self.assertTrue(ambient._wait_for_wayland(timeout_seconds=3))


HERE = os.path.dirname(os.path.abspath(__file__))


def _load_service(filename):
    """Parse a systemd service file, returning all key=value pairs."""
    cfg = configparser.RawConfigParser(strict=False)
    cfg.read(os.path.join(HERE, filename))
    result = {}
    for section in cfg.sections():
        for k, v in cfg.items(section):
            result[k.lower()] = v
    return result


class TestServiceConfig(unittest.TestCase):
    def test_ambient_never_gives_up_restarting(self):
        cfg = _load_service("breaktimer-ambient.service")
        self.assertEqual(cfg.get("startlimitintervalsec"), "0",
                         "ambient must set StartLimitIntervalSec=0 so it retries indefinitely")

    def test_core_never_gives_up_restarting(self):
        cfg = _load_service("breaktimer-core.service")
        self.assertEqual(cfg.get("startlimitintervalsec"), "0",
                         "core must set StartLimitIntervalSec=0 so it retries indefinitely")

    def test_ambient_restarts_on_any_exit(self):
        cfg = _load_service("breaktimer-ambient.service")
        self.assertEqual(cfg.get("restart"), "always")

    def test_core_restarts_on_any_exit(self):
        cfg = _load_service("breaktimer-core.service")
        self.assertEqual(cfg.get("restart"), "always")

    def test_ambient_faster_restart_than_core(self):
        # Ambient recovers from Wayland compositor crashes faster than core
        # recovers from its own restarts (core holds persistent state).
        ambient_sec = int(_load_service("breaktimer-ambient.service").get("restartsec", "5"))
        core_sec = int(_load_service("breaktimer-core.service").get("restartsec", "5"))
        self.assertLessEqual(ambient_sec, core_sec)


if __name__ == "__main__":
    unittest.main()
