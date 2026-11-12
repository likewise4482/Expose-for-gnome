"""Button mapper daemon: runs a backend (X11 or evdev), the HID++ source and a
small D-Bus service used by the GUI (status, device lists, learn mode)."""
import argparse
import json
import os
import signal
import sys
import threading

from gi.repository import Gio, GLib

from . import config, devices, hidpp, ids, permissions
from .actions import ActionRunner
from .learn import Learner

DBUS_XML = f"""
<node>
  <interface name="{ids.DAEMON_BUS_NAME}">
    <method name="GetStatus"><arg type="s" direction="out"/></method>
    <method name="ListDevices"><arg type="s" direction="out"/></method>
    <method name="ListHidppDevices"><arg type="s" direction="out"/></method>
    <method name="Learn">
      <arg name="timeout_ms" type="u" direction="in"/>
      <arg type="s" direction="out"/>
    </method>
    <method name="CancelLearn"/>
    <method name="Reload"/>
  </interface>
</node>"""


class NullBackend:
    """Used when no input backend can run (e.g. Wayland before the one-time setup)."""
    name = 'none'

    def __init__(self, runner, learner=None, problem=''):
        self.problem = problem

    def apply(self, mapping):
        pass

    def status(self):
        return {'backend': self.name, 'problem': self.problem, 'grabbed': []}

    def run(self, poll_reload, stop):
        while not stop.wait(0.5):
            poll_reload()

    def close(self):
        pass

    def send(self, mods, key):
        raise ValueError('no input backend running')


def pick_backend(name, runner, learner):
    session = permissions.session_type()
    if name == 'auto':
        name = 'x11' if session == 'x11' and os.environ.get('DISPLAY') else 'evdev'
    if name == 'x11':
        try:
            from .x11 import X11Backend
            return X11Backend(runner, learner)
        except (OSError, RuntimeError) as e:
            print(f'{ids.SLUG}: X11 backend unavailable ({e}); trying evdev', flush=True)
    try:
        from .evdev_backend import EvdevBackend
    except ImportError:
        return NullBackend(runner, learner, 'python3-evdev is not installed '
                           '(needed on Wayland): install python3-evdev')
    missing = permissions.evdev_missing()
    if missing:
        return NullBackend(runner, learner, f'one-time setup needed: {missing}')
    return EvdevBackend(runner, learner)


class Daemon:
    def __init__(self, backend_override=None):
        self.backend_override = backend_override
        self.cfg = config.load()
        self.learner = Learner()
        self.runner = ActionRunner()
        self.backend = pick_backend(backend_override or self.cfg['backend'],
                                    self.runner, self.learner)
        self.runner.key_sender = self.backend
        self.hidpp = hidpp.HidppSource(self.runner, self.learner)
        self.stop = threading.Event()
        self.loop = GLib.MainLoop()
        self.restart = False
        self._last_mtime = config.mtime()
        self._force_reload = False
        print(f'{ids.SLUG} {ids.VERSION}: {self.backend.name} backend, '
              f'config {config.CONFIG_PATH}', flush=True)

    # --- config ------------------------------------------------------------
    def apply(self):
        mapping = config.Mapping(self.cfg)
        self.backend.apply(mapping)
        self.hidpp.configure(mapping.hidpp_bindings())

    def poll_reload(self):
        m = config.mtime()
        if m == self._last_mtime and not self._force_reload:
            return
        self._last_mtime = m
        self._force_reload = False
        new = config.load()
        if not self.backend_override and new['backend'] != self.cfg['backend']:
            print(f'{ids.SLUG}: backend changed, restarting', flush=True)
            self.restart = True
            self.stop.set()
            GLib.idle_add(self.loop.quit)
            return
        self.cfg = new
        print(f'{ids.SLUG}: config reloaded', flush=True)
        self.apply()

    # --- D-Bus -------------------------------------------------------------
    def status(self):
        st = self.backend.status()
        st.update({
            'version': ids.VERSION,
            'session_type': permissions.session_type(),
            'configured_backend': self.cfg['backend'],
            'permissions': permissions.check(),
            'learning': self.learner.active,
            'config_path': str(config.CONFIG_PATH),
            'recent_actions': self.runner.log[-5:],
        })
        return st

    def _on_call(self, conn, sender, path, iface, method, params, invocation):
        def reply(obj):
            GLib.idle_add(lambda: invocation.return_value(
                GLib.Variant('(s)', (json.dumps(obj),))) and False)

        if method == 'GetStatus':
            reply(self.status())
        elif method == 'ListDevices':
            grabbed = set(self.backend.status().get('grabbed', []))
            devs = [dict(d.to_dict(), grabbed=d.name in grabbed)
                    for d in devices.list_devices()]
            reply(devs)
        elif method == 'ListHidppDevices':
            def work():
                try:
                    reply(self.hidpp.list_devices())
                except Exception as e:  # noqa: BLE001 - reported to the GUI
                    reply({'devices': [], 'errors': [{'error': str(e)}]})
            threading.Thread(target=work, daemon=True).start()
        elif method == 'Learn':
            timeout = max(1000, min(params.unpack()[0], 60000)) / 1000
            self.learner.start(timeout, reply)
        elif method == 'CancelLearn':
            self.learner.cancel()
            invocation.return_value(None)
        elif method == 'Reload':
            self._force_reload = True
            invocation.return_value(None)

    def _export(self, conn):
        node = Gio.DBusNodeInfo.new_for_xml(DBUS_XML)
        conn.register_object(ids.DAEMON_OBJECT_PATH, node.interfaces[0], self._on_call)

    def run(self):
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self._export(bus)

        def lost(*_):
            print(f'{ids.SLUG}: another instance owns {ids.DAEMON_BUS_NAME}; exiting',
                  flush=True)
            self.stop.set()
            self.loop.quit()
        Gio.bus_own_name_on_connection(bus, ids.DAEMON_BUS_NAME,
                                       Gio.BusNameOwnerFlags.DO_NOT_QUEUE, None, lost)

        def backend_thread():
            try:
                self.apply()
                self.backend.run(self.poll_reload, self.stop)
            except Exception as e:  # noqa: BLE001 - logged, daemon exits for systemd restart
                print(f'{ids.SLUG}: backend crashed: {e!r}', flush=True)
                import traceback
                traceback.print_exc()
            finally:
                GLib.idle_add(self.loop.quit)

        self.hidpp.start()
        worker = threading.Thread(target=backend_thread, daemon=True, name='backend')
        worker.start()
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, self._quit)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, self._quit)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGHUP, self._reload_signal)
        try:
            self.loop.run()
        finally:
            self.stop.set()
            worker.join(timeout=3)
            self.hidpp.stop()
            self.backend.close()

    def _quit(self):
        self.stop.set()
        self.loop.quit()
        return GLib.SOURCE_REMOVE

    def _reload_signal(self):
        self._force_reload = True
        return GLib.SOURCE_CONTINUE


def main(argv=None):
    parser = argparse.ArgumentParser(description=f'{ids.PRODUCT_NAME} button mapper daemon')
    parser.add_argument('--backend', choices=sorted(config.BACKENDS),
                        help='override the backend from the config file')
    parser.add_argument('--version', action='version', version=ids.VERSION)
    args = parser.parse_args(argv)
    daemon = Daemon(args.backend)
    daemon.run()
    if daemon.restart:
        os.execv(sys.executable, [sys.executable] + sys.argv)


if __name__ == '__main__':
    main()
