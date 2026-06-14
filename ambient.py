#!/usr/bin/env python3
"""Always-on ambient mana bar — a thin layer-shell strip on the top screen edge.

Renders the live snapshot the timer core publishes (see status.py) with the
shared palette. Normally a few pixels tall with that sliver of screen reserved;
expands with detail text on hover or when time runs low (the expansion overlays
windows rather than shoving them around). If the core stops publishing, the
strip turns grey instead of lying about remaining time.
"""
import os
import sys
import time

import cairo
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("GtkLayerShell", "0.1")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gtk, Gdk, GLib, GtkLayerShell, Pango, PangoCairo

import brightness_control
import status
from status import format_time

def _wait_for_wayland(timeout_seconds=120):
    """Block until the Wayland compositor socket appears.

    The ambient service starts as soon as graphical-session.target becomes
    active, but the Wayland socket may not exist yet. Without this wait the
    process crashes immediately and systemd has to retry 9+ times over 45s.
    Returns True when ready, False if the socket never appears before timeout.
    """
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
    display = os.environ.get("WAYLAND_DISPLAY", "wayland-0")
    socket = os.path.join(runtime_dir, display)
    deadline = time.monotonic() + timeout_seconds
    while not os.path.exists(socket):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)
    return True


STRIP_HEIGHT = 6
EXPANDED_HEIGHT = 28
EXPAND_SECONDS = 10 * 60   # bar expands and shows time at this threshold
WARN_SECONDS = 5 * 60      # warning text ("wrap up", "save") appears below this
PULSE_MS = 500             # grace-mode background pulse interval
FONT = Pango.FontDescription("monospace 10")


def _mix(rgb, target, t):
    """Blend an (r, g, b) triplet toward a target colour by fraction t."""
    return tuple(c + (tc - c) * t for c, tc in zip(rgb, target))


def _lighten(rgb, t):
    return _mix(rgb, (255, 255, 255), t)


def _darken(rgb, t):
    return _mix(rgb, (0, 0, 0), t)


class AmbientBar(Gtk.Window):
    def __init__(self, monitor=None):
        super().__init__()
        self.snapshot = None
        self.hovered = False
        self._pulse = False
        self.brightness_pause_until = 0.0

        GtkLayerShell.init_for_window(self)
        GtkLayerShell.set_namespace(self, "breaktimer")
        GtkLayerShell.set_layer(self, GtkLayerShell.Layer.TOP)
        for edge in (GtkLayerShell.Edge.TOP, GtkLayerShell.Edge.LEFT,
                     GtkLayerShell.Edge.RIGHT):
            GtkLayerShell.set_anchor(self, edge, True)
        GtkLayerShell.set_exclusive_zone(self, STRIP_HEIGHT)
        if monitor is not None:
            GtkLayerShell.set_monitor(self, monitor)

        visual = self.get_screen().get_rgba_visual()
        if visual:
            self.set_visual(visual)
        self.set_app_paintable(True)

        self.area = Gtk.DrawingArea()
        self.area.connect("draw", self.on_draw)
        self.add(self.area)

        self.add_events(Gdk.EventMask.ENTER_NOTIFY_MASK
                        | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.connect("enter-notify-event", self.on_hover, True)
        self.connect("leave-notify-event", self.on_hover, False)

        self.set_size_request(-1, STRIP_HEIGHT)
        GLib.timeout_add(1000, self.refresh)
        GLib.timeout_add(PULSE_MS, self._pulse_tick)
        self.refresh()

    # -- state -----------------------------------------------------------
    def _pulse_tick(self):
        in_grace = self.snapshot and self.snapshot.get("grace_remaining") is not None
        if in_grace:
            self._pulse = not self._pulse
            self.area.queue_draw()
        elif self._pulse:
            self._pulse = False
            self.area.queue_draw()
        return True

    def refresh(self):
        self.snapshot = status.read_status()
        self.brightness_pause_until = brightness_control.pause_until()
        self.set_size_request(-1, self.target_height())
        self.area.queue_draw()
        return True  # keep the GLib timer alive

    def on_hover(self, _widget, _event, entered):
        self.hovered = entered
        self.set_size_request(-1, self.target_height())
        self.area.queue_draw()

    def is_critical(self):
        s = self.snapshot
        if not s:
            return False
        return (s.get("grace_remaining") is not None
                or s["remaining_seconds"] < EXPAND_SECONDS
                or s.get("refill_rate", 1.0) <= 0)  # past the daily limit: stay expanded

    def target_height(self):
        return EXPANDED_HEIGHT if (self.hovered or self.is_critical()) else STRIP_HEIGHT

    # -- drawing ----------------------------------------------------------
    def on_draw(self, area, cr):
        w = area.get_allocated_width()
        h = area.get_allocated_height()

        in_grace = self.snapshot and self.snapshot.get("grace_remaining") is not None
        if in_grace and self._pulse:
            cr.set_source_rgba(0.42, 0.03, 0.03, 0.96)
        else:
            cr.set_source_rgba(0.05, 0.05, 0.06, 0.88)
        cr.paint()

        s = self.snapshot
        if not s:
            # core silent: a flat desaturated rail — no colour, no claim about time
            cr.set_source_rgba(0.42, 0.42, 0.45, 0.55)
            cr.rectangle(0, 0, w, h)
            cr.fill()
            if h >= EXPANDED_HEIGHT:
                self._text(cr, w / 2, h, "breaktimer core not running",
                           align="center", rgb=(210, 210, 210))
            return

        fraction = (s["remaining_seconds"] / s["max_seconds"]
                    if s["max_seconds"] else 0.0)
        self._fill_bar(cr, w, h, fraction,
                       status.color_for_fraction(fraction), s["is_active"])

        if h >= EXPANDED_HEIGHT:
            self._draw_detail(cr, w, h, s, fraction)

    @staticmethod
    def _fill_bar(cr, w, h, fraction, rgb, active):
        """Draw the mana bar: a dim full-width ghost rail of the current hue,
        the lit portion over it with a top-lit vertical gradient and catch-light,
        and a soft glow at the leading edge. Idle (replenishing) reads as dimmed."""
        r, g, b = (c / 255 for c in rgb)

        # ghost of the spent remainder — keeps the strip one continuous rail
        # instead of a bright stub trailing into a black slab
        cr.set_source_rgba(r, g, b, 0.14 if active else 0.08)
        cr.rectangle(0, 0, w, h)
        cr.fill()

        fill_w = w * fraction
        if fill_w <= 0:
            return
        alpha = 1.0 if active else 0.5  # dimmed while replenishing

        top = _lighten(rgb, 0.45)
        bot = _darken(rgb, 0.18)
        grad = cairo.LinearGradient(0, 0, 0, h)
        grad.add_color_stop_rgba(0.0, *(c / 255 for c in top), alpha)
        grad.add_color_stop_rgba(0.4, r, g, b, alpha)
        grad.add_color_stop_rgba(1.0, *(c / 255 for c in bot), alpha)
        cr.set_source(grad)
        cr.rectangle(0, 0, fill_w, h)
        cr.fill()

        # catch-light along the very top edge
        cr.set_source_rgba(1, 1, 1, 0.30 * alpha)
        cr.rectangle(0, 0, fill_w, 1)
        cr.fill()

        # soft bright glow at the leading edge — reads as charged, not cut off
        if fraction < 1.0:
            glow_w = min(8.0, fill_w)
            x0 = fill_w - glow_w
            glow = cairo.LinearGradient(x0, 0, fill_w, 0)
            glow.add_color_stop_rgba(0.0, *(c / 255 for c in top), 0.0)
            glow.add_color_stop_rgba(1.0, 1, 1, 1, 0.55 * alpha)
            cr.set_source(glow)
            cr.rectangle(x0, 0, glow_w, h)
            cr.fill()

    def _draw_detail(self, cr, w, h, s, fraction):
        icon = "●" if s["is_active"] else "○"
        left = f" {format_time(s['remaining_seconds'])} {icon}"
        self._text(cr, 0, h, left, align="left")

        history = s.get("history")
        if history:
            self._text(cr, w - 8, h, history, align="right",
                       rgb=self._history_rgb(s))

        center, rgb = self._center_text(s)
        if center:
            self._text(cr, w / 2, h, center, align="center", rgb=rgb)

    @staticmethod
    def _history_rgb(s):
        """History text mirrors the day's fatigue: white in budget, amber past
        the budget, red once refill is gone."""
        rate = s.get("refill_rate", 1.0)
        if rate <= 0:
            return (255, 80, 80)
        if rate < 1:
            return (255, 190, 80)
        return (255, 255, 255)

    def _center_text(self, s):
        warning = self._warning_text(s)
        if warning:
            return warning, (255, 80, 80)
        if s.get("refill_rate", 1.0) <= 0:
            return "day limit reached — no refill", (255, 190, 80)
        pause_left = self.brightness_pause_until - time.time()
        if pause_left > 0:
            return f"☀ brightness paused  {format_time(pause_left)}", (120, 200, 255)
        return None, None

    @staticmethod
    def _warning_text(s):
        grace = s.get("grace_remaining")
        if grace is not None:
            if s.get("refill_rate", 1.0) <= 0:
                return f"DAY LIMIT — SHUTTING DOWN IN {format_time(grace)}"
            return f"SHUTTING DOWN IN {format_time(grace)} — go idle to cancel"
        remaining = s["remaining_seconds"]
        if remaining < 2 * 60:
            return "⚠ save your work now"
        if remaining < WARN_SECONDS:
            return "⚠ wrap up soon"
        return None

    def _text(self, cr, x, h, text, align="left", rgb=(255, 255, 255)):
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(FONT)
        layout.set_text(text, -1)
        tw, th = layout.get_pixel_size()
        if align == "center":
            x -= tw / 2
        elif align == "right":
            x -= tw
        y = (h - th) / 2
        # shadow first so the text reads over any bar colour
        cr.move_to(x + 1, y + 1)
        cr.set_source_rgba(0, 0, 0, 0.85)
        PangoCairo.show_layout(cr, layout)
        cr.move_to(x, y)
        cr.set_source_rgba(rgb[0] / 255, rgb[1] / 255, rgb[2] / 255, 1.0)
        PangoCairo.show_layout(cr, layout)


class BarManager:
    """Creates and tracks one AmbientBar per monitor.

    Never calls Gtk.main_quit() — the process lives until SIGTERM so that
    reconnected monitors always get a new bar without requiring a restart.
    """

    def __init__(self, create_bar):
        self._bars = {}
        self._create_bar = create_bar

    def add(self, monitor):
        if monitor in self._bars:
            return
        self._bars[monitor] = self._create_bar(monitor)

    def remove(self, monitor):
        bar = self._bars.pop(monitor, None)
        if bar is not None:
            bar.destroy()


def main():
    lock = status.acquire_singleton_lock("ambient")
    if lock is None:
        print("ambient bar already running — exiting", file=sys.stderr)
        sys.exit(0)

    if not _wait_for_wayland():
        print("Wayland socket not available after 120s — giving up", file=sys.stderr)
        sys.exit(1)

    def _create_bar(monitor):
        bar = AmbientBar(monitor=monitor)
        bar.show_all()
        return bar

    manager = BarManager(_create_bar)

    display = Gdk.Display.get_default()
    display.connect("monitor-added", lambda _d, m: manager.add(m))
    display.connect("monitor-removed", lambda _d, m: manager.remove(m))

    for i in range(display.get_n_monitors()):
        manager.add(display.get_monitor(i))

    Gtk.main()


if __name__ == "__main__":
    main()
