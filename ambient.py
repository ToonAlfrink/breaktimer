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

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("GtkLayerShell", "0.1")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gtk, Gdk, GLib, GtkLayerShell, Pango, PangoCairo

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


class AmbientBar(Gtk.Window):
    def __init__(self, monitor=None):
        super().__init__()
        self.snapshot = None
        self.hovered = False
        self._pulse = False

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
                        | Gdk.EventMask.LEAVE_NOTIFY_MASK
                        | Gdk.EventMask.BUTTON_PRESS_MASK)
        self.area.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.connect("enter-notify-event", self.on_hover, True)
        self.connect("leave-notify-event", self.on_hover, False)
        self.connect("button-press-event", self.on_click)

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
        self.set_size_request(-1, self.target_height())
        self.area.queue_draw()
        return True  # keep the GLib timer alive

    def on_click(self, _widget, event):
        if event.button == 1:
            status.write_command({"type": "extend", "seconds": 600})

    def on_hover(self, _widget, _event, entered):
        self.hovered = entered
        self.set_size_request(-1, self.target_height())
        self.area.queue_draw()

    def is_critical(self):
        s = self.snapshot
        if not s:
            return False
        return (s.get("grace_remaining") is not None
                or s["remaining_seconds"] < EXPAND_SECONDS)

    def target_height(self):
        expanded = self.hovered or self.is_critical()
        return EXPANDED_HEIGHT if expanded else STRIP_HEIGHT

    # -- drawing ----------------------------------------------------------
    def on_draw(self, area, cr):
        w = area.get_allocated_width()
        h = area.get_allocated_height()

        in_grace = self.snapshot and self.snapshot.get("grace_remaining") is not None
        if in_grace and self._pulse:
            cr.set_source_rgba(0.40, 0.03, 0.03, 0.96)
        else:
            cr.set_source_rgba(0.06, 0.06, 0.06, 0.92)
        cr.paint()

        s = self.snapshot
        if not s:
            cr.set_source_rgba(0.4, 0.4, 0.4, 0.9)
            cr.rectangle(0, 0, w, h)
            cr.fill()
            if h >= EXPANDED_HEIGHT:
                self._text(cr, w / 2, h, "breaktimer core not running",
                           align="center", rgb=(255, 255, 255))
            return

        fraction = (s["remaining_seconds"] / s["max_seconds"]
                    if s["max_seconds"] else 0.0)
        r, g, b = status.color_for_fraction(fraction)
        alpha = 1.0 if s["is_active"] else 0.45  # dimmed while replenishing
        cr.set_source_rgba(r / 255, g / 255, b / 255, alpha)
        cr.rectangle(0, 0, w * fraction, h)
        cr.fill()

        if h >= EXPANDED_HEIGHT:
            self._draw_detail(cr, w, h, s, fraction)

    def _draw_detail(self, cr, w, h, s, fraction):
        icon = "●" if s["is_active"] else "○"
        left = f" {format_time(s['remaining_seconds'])} {icon}"
        self._text(cr, 0, h, left, align="left")

        history = s.get("history")
        if history:
            self._text(cr, w - 8, h, history, align="right")

        warning = self._warning_text(s)
        if warning:
            self._text(cr, w / 2, h, warning, align="center", rgb=(255, 80, 80))

    @staticmethod
    def _warning_text(s):
        grace = s.get("grace_remaining")
        if grace is not None:
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
