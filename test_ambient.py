"""Tests for ambient bar logic that does not require a live GTK/Wayland session."""
import unittest

import ambient
from ambient import AmbientBar, BarManager, EXPAND_SECONDS, WARN_SECONDS


class TestWarningText(unittest.TestCase):
    def test_grace_mode_shows_countdown_and_idle_hint(self):
        s = {"grace_remaining": 45.0, "remaining_seconds": 0}
        text = AmbientBar._warning_text(s)
        self.assertIn("SHUTTING DOWN", text)
        self.assertIn("0:45", text)
        self.assertIn("go idle to cancel", text)

    def test_under_2min_shows_save_warning(self):
        s = {"remaining_seconds": 90, "grace_remaining": None}
        text = AmbientBar._warning_text(s)
        self.assertIn("save your work", text)
        self.assertNotIn("wrap up", text)

    def test_between_2_and_warn_shows_wrap_up(self):
        s = {"remaining_seconds": 3 * 60, "grace_remaining": None}
        text = AmbientBar._warning_text(s)
        self.assertIn("wrap up soon", text)
        self.assertNotIn("save your work", text)

    def test_above_warn_threshold_returns_none(self):
        s = {"remaining_seconds": WARN_SECONDS + 1, "grace_remaining": None}
        self.assertIsNone(AmbientBar._warning_text(s))

    def test_grace_takes_priority_over_remaining(self):
        # even with remaining_seconds > 0, grace_remaining drives the message
        s = {"remaining_seconds": 500, "grace_remaining": 30.0}
        text = AmbientBar._warning_text(s)
        self.assertIn("SHUTTING DOWN", text)


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
        bar = self._bar_with_snapshot({"grace_remaining": 40.0, "remaining_seconds": 0})
        self.assertTrue(bar.is_critical())

    def test_below_expand_threshold_is_critical(self):
        bar = self._bar_with_snapshot({"grace_remaining": None, "remaining_seconds": EXPAND_SECONDS - 1})
        self.assertTrue(bar.is_critical())

    def test_above_expand_threshold_not_critical(self):
        bar = self._bar_with_snapshot({"grace_remaining": None, "remaining_seconds": EXPAND_SECONDS + 1})
        self.assertFalse(bar.is_critical())


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
        self.assertEqual(mgr.count(), 1)
        self.assertEqual(len(created), 1)

    def test_add_same_monitor_twice_is_idempotent(self):
        mgr, created = self._setup()
        mgr.add("mon1")
        mgr.add("mon1")
        self.assertEqual(mgr.count(), 1)
        self.assertEqual(len(created), 1)

    def test_add_multiple_monitors(self):
        mgr, created = self._setup()
        mgr.add("mon1")
        mgr.add("mon2")
        self.assertEqual(mgr.count(), 2)
        self.assertEqual(len(created), 2)

    def test_remove_destroys_bar_and_decrements_count(self):
        mgr, created = self._setup()
        mgr.add("mon1")
        mgr.remove("mon1")
        self.assertTrue(created[0].destroyed)
        self.assertEqual(mgr.count(), 0)

    def test_remove_nonexistent_is_noop(self):
        mgr, _ = self._setup()
        mgr.remove("never-added")  # must not raise
        self.assertEqual(mgr.count(), 0)

    def test_remove_all_monitors_does_not_quit(self):
        # Regression: removing the last monitor used to call Gtk.main_quit(),
        # killing the process. A reconnected monitor would get no bar without a
        # full restart. BarManager must survive zero-bar state.
        mgr, _ = self._setup()
        mgr.add("mon1")
        mgr.remove("mon1")
        self.assertEqual(mgr.count(), 0)
        # If we get here, Gtk.main_quit() was NOT called (no GTK is running).

    def test_reconnected_monitor_gets_new_bar(self):
        mgr, created = self._setup()
        mgr.add("mon1")
        mgr.remove("mon1")
        mgr.add("mon1")
        self.assertEqual(mgr.count(), 1)
        self.assertEqual(len(created), 2)
        self.assertFalse(created[1].destroyed)


if __name__ == "__main__":
    unittest.main()
