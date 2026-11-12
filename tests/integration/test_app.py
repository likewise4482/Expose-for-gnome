#!/usr/bin/env python3
"""A GTK4 app with N windows (one application, so App Exposé tiles them)."""
import sys

import gi

gi.require_version('Gtk', '4.0')
from gi.repository import Gtk  # noqa: E402

APP_ID = 'org.example.EfgTestApp'
COLORS = ['#e01b24', '#2ec27e', '#3584e4', '#f6d32d', '#9141ac', '#ff7800']


class App(Gtk.Application):
    def __init__(self, count):
        super().__init__(application_id=APP_ID)
        self.count = count

    def do_activate(self):
        css = Gtk.CssProvider()
        css.load_from_data(''.join(
            f'.w{i} {{ background: {c}; }} ' for i, c in enumerate(COLORS)).encode())
        Gtk.StyleContext.add_provider_for_display(
            Gtk.Widget.get_display(Gtk.Label()), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        for i in range(self.count):
            win = Gtk.ApplicationWindow(application=self, title=f'Test window {i + 1}',
                                        default_width=480 + 60 * i, default_height=320)
            label = Gtk.Label(label=f'Window {i + 1}')
            label.add_css_class(f'w{i % len(COLORS)}')
            win.set_child(label)
            win.present()


if __name__ == '__main__':
    App(int(sys.argv[1]) if len(sys.argv) > 1 else 3).run([sys.argv[0]])
