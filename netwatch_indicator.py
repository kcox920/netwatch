#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Kieran Cox
"""GNOME top-bar light for netwatch.

green  = Cloudflare speed >= 20 Mbit/s and ping healthy
yellow = 3-20 Mbit/s, or ping degraded
red    = < 3 Mbit/s, ping dead/very slow, and it BLINKS
grey   = the logger has stopped reporting (don't trust the line, fix the logger)

The light is the worse of the speed level and the ping level (see
netwatch.classify). Click it for the numbers and "Quick check now".
"""
import fcntl
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import netwatch as nw  # noqa: E402

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AI
except (ValueError, ImportError):
    gi.require_version("AppIndicator3", "0.1")
    from gi.repository import AppIndicator3 as AI
from gi.repository import GLib, Gtk  # noqa: E402

ICONS = HERE / "icons"
PY = sys.executable
PROBE = str(HERE / "netwatch.py")

# (minutes, menu label); 0 = scheduled tests paused. Anything else is "Custom...".
PRESETS = [(5, "Every 5 min"), (10, "Every 10 min"), (15, "Every 15 min"), (30, "Every 30 min"),
           (60, "Every hour"), (120, "Every 2 hours"), (360, "Every 6 hours"),
           (0, "Paused (no scheduled speed tests)")]


def fmt_interval(n):
    if not n:
        return "paused"
    if n >= 60 and n % 60 == 0:
        return f"every {n // 60} h"
    return f"every {n} min"


def ago(ts):
    s = max(int(time.time() - ts), 0)
    if s < 90:
        return f"{s}s ago"
    if s < 5400:
        return f"{s // 60} min ago"
    return f"{s / 3600:.1f} h ago"


class Light:
    def __init__(self):
        self.ind = AI.Indicator.new_with_path("netwatch", "netwatch-unknown",
                                              AI.IndicatorCategory.SYSTEM_SERVICES, str(ICONS))
        self.ind.set_status(AI.IndicatorStatus.ACTIVE)
        self.ind.set_title("Netwatch")
        self.state = None
        self.dim = False
        self.c = nw.cfg()

        menu = Gtk.Menu()
        self.items = {}
        self._updating = False
        for key in ("state", "speed", "ping", "next"):
            it = Gtk.MenuItem(label="...")
            it.set_sensitive(False)
            menu.append(it)
            self.items[key] = it
        menu.append(Gtk.SeparatorMenuItem())
        self._action(menu, "Quick check now (~5 s)", self.on_quick)
        self._action(menu, "Full speed test now (25 MB)", self.on_full)
        self.build_freq_menu(menu)
        menu.append(Gtk.SeparatorMenuItem())
        self._action(menu, "24 h report...", self.on_report)
        self._action(menu, "Open data folder", lambda *_: subprocess.Popen(["xdg-open", str(nw.DATA)]))
        menu.append(Gtk.SeparatorMenuItem())
        self._action(menu, "Quit", lambda *_: Gtk.main_quit())
        menu.show_all()
        self.ind.set_menu(menu)

        GLib.timeout_add_seconds(5, self.refresh)
        GLib.timeout_add(600, self.blink)
        self.refresh()

    def _action(self, menu, label, cb):
        it = Gtk.MenuItem(label=label)
        it.connect("activate", cb)
        menu.append(it)

    # -- speed test frequency -------------------------------------------
    def build_freq_menu(self, menu):
        self.freq_parent = Gtk.MenuItem(label="Speed test frequency")
        sub = Gtk.Menu()
        self.radios = {}
        first = None
        for minutes, label in PRESETS:
            it = Gtk.RadioMenuItem.new_with_label_from_widget(first, label)
            first = first or it
            it.connect("toggled", self.on_freq_toggled, minutes)
            sub.append(it)
            self.radios[minutes] = it
        # hidden radio = "none of the presets" (a custom value is active)
        self.radio_custom_active = Gtk.RadioMenuItem.new_with_label_from_widget(first, "")
        self.radio_custom_active.set_no_show_all(True)
        sub.append(self.radio_custom_active)
        sub.append(Gtk.SeparatorMenuItem())
        self.custom_item = Gtk.MenuItem(label="Custom...")
        self.custom_item.connect("activate", self.on_custom)
        sub.append(self.custom_item)
        self.freq_parent.set_submenu(sub)
        menu.append(self.freq_parent)

    def on_freq_toggled(self, item, minutes):
        if self._updating or not item.get_active():
            return
        nw.set_config("bw_interval_min", minutes)
        self.refresh()

    def on_custom(self, *_):
        current = nw.cfg()["bw_interval_min"] or 30
        dlg = Gtk.Dialog(title="Speed test frequency", modal=True)
        dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dlg.add_button("Set", Gtk.ResponseType.OK)
        box = dlg.get_content_area()
        box.set_spacing(10)
        box.set_border_width(14)
        row = Gtk.Box(spacing=8)
        row.pack_start(Gtk.Label(label="Run a full speed test every"), False, False, 0)
        spin = Gtk.SpinButton.new_with_range(1, 1440, 1)
        spin.set_value(current)
        row.pack_start(spin, False, False, 0)
        row.pack_start(Gtk.Label(label="minutes"), False, False, 0)
        note = Gtk.Label()
        note.set_xalign(0)

        def update_note(*_):
            n = spin.get_value_int()
            note.set_text(f"Uses up to ~{25 * 1440 / n / 1000:.1f} GB/day on a fast line "
                          "(far less when the line is slow).")
        spin.connect("value-changed", update_note)
        update_note()
        box.pack_start(row, False, False, 0)
        box.pack_start(note, False, False, 0)
        dlg.show_all()
        if dlg.run() == Gtk.ResponseType.OK:
            nw.set_config("bw_interval_min", spin.get_value_int())
        dlg.destroy()
        self.refresh()

    def update_freq(self, latest):
        minutes = self.c["bw_interval_min"] or 0
        self.freq_parent.set_label(f"Speed test frequency: {fmt_interval(minutes)}")
        self._updating = True
        try:
            (self.radios.get(minutes) or self.radio_custom_active).set_active(True)
            self.custom_item.set_label(f"Custom... (now {fmt_interval(minutes)})" if minutes not in self.radios
                                       else "Custom...")
        finally:
            self._updating = False
        if not minutes:
            txt = "Next speed test: paused"
        else:
            last = (latest.get("bw_full") or {}).get("ts")
            remaining = (last + minutes * 60 - time.time()) if last else 0
            txt = ("Next speed test: due now" if remaining <= 60
                   else f"Next speed test: in {int(remaining // 60)} min")
        self.items["next"].set_label(txt)

    # -- actions ---------------------------------------------------------
    def spawn(self, *args):
        subprocess.Popen([PY, PROBE, *args], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for delay in (3, 7, 12):  # pick the result up quickly instead of waiting for the 5 s poll
            GLib.timeout_add_seconds(delay, lambda: (self.refresh(), False)[1])

    def on_quick(self, *_):
        self.items["state"].set_label("Quick check running...")
        self.spawn("quick", "--notify")

    def on_full(self, *_):
        self.items["state"].set_label("Speed test running...")
        self.spawn("bw", "--notify")

    def on_report(self, *_):
        out = subprocess.run([PY, PROBE, "report", "--hours", "24"], capture_output=True, text=True).stdout
        dlg = Gtk.MessageDialog(message_type=Gtk.MessageType.INFO, buttons=Gtk.ButtonsType.CLOSE,
                                text="Netwatch - last 24 hours")
        dlg.format_secondary_text(out.strip())
        dlg.run()
        dlg.destroy()

    # -- display ---------------------------------------------------------
    def set_icon(self, name):
        self.ind.set_icon_full(name, name)

    def refresh(self):
        self.c = nw.cfg()  # re-read each time so edits (menu or by hand) apply immediately
        latest = nw.load_latest()
        self.update_freq(latest)
        state, detail = nw.classify(self.c, latest)
        if state != self.state:
            self.state, self.dim = state, False
            self.set_icon(f"netwatch-{state}")
        p, b = latest.get("ping"), latest.get("bw")
        label = {"green": "Connection good", "yellow": "Connection degraded",
                 "red": "Connection BAD", "unknown": "No data - logger not running?"}[state]
        self.items["state"].set_label(label)
        if b:
            self.items["speed"].set_label(f"Speed: {b['mbit']:.1f} Mbit/s  ({b['type']}, {ago(b['ts'])})")
        else:
            self.items["speed"].set_label("Speed: no test yet")
        if p:
            ms = f"{p['avg']:.0f} ms" if "avg" in p else "no reply"
            self.items["ping"].set_label(f"Ping {self.c['ping_host']}: {ms}, {p['loss']:.0f}% loss  ({ago(p['ts'])})")
        else:
            self.items["ping"].set_label("Ping: no data yet")
        return True

    def blink(self):
        if self.state == "red":
            self.dim = not self.dim
            self.set_icon("netwatch-red-off" if self.dim else "netwatch-red")
        return True


def main():
    DATA = nw.DATA
    DATA.mkdir(parents=True, exist_ok=True)
    lock = open(DATA / "indicator.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0  # already running
    Light()
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
