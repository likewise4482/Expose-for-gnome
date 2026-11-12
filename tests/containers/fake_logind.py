#!/usr/bin/env python3
"""Owns org.freedesktop.login1 on a container's system bus so GNOME Shell 48+
starts without systemd. Exports an empty Manager; the shell tolerates the
missing session object."""
import sys

from gi.repository import Gio, GLib

XML = '''<node><interface name="org.freedesktop.login1.Manager">
  <method name="GetSession"><arg type="s" direction="in"/><arg type="o" direction="out"/></method>
  <method name="GetSessionByPID"><arg type="u" direction="in"/><arg type="o" direction="out"/></method>
  <method name="Inhibit"><arg type="s" direction="in"/><arg type="s" direction="in"/>
    <arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="h" direction="out"/></method>
  <signal name="PrepareForSleep"><arg type="b"/></signal>
</interface></node>'''


def call(conn, sender, path, iface, method, params, inv):
    inv.return_dbus_error('org.freedesktop.login1.NoSuchSession', 'container test stub')


def acquired(conn, name):
    print('fake logind ready', flush=True)


bus = Gio.bus_get_sync(Gio.BusType.SYSTEM)
bus.register_object('/org/freedesktop/login1',
                    Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0], call, None, None)
Gio.bus_own_name_on_connection(bus, 'org.freedesktop.login1', 0, acquired,
                               lambda *a: sys.exit('could not own org.freedesktop.login1'))
GLib.MainLoop().run()
