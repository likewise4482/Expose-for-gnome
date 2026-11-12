import shlex
import subprocess
import threading

from gi.repository import Gio, GLib

from . import ids

DBUS_METHODS = {
    'app-expose': 'ToggleAppExpose',
    'mission-control': 'ToggleOverview',
    'app-grid': 'ToggleAppGrid',
    'workspace-prev': 'WorkspacePrevious',
    'workspace-next': 'WorkspaceNext',
    'last-window': 'SwitchToLastWindow',
}

MODIFIER_ALIASES = {
    'ctrl': 'ctrl', 'control': 'ctrl', 'primary': 'ctrl',
    'shift': 'shift',
    'alt': 'alt',
    'super': 'super', 'meta': 'super', 'win': 'super', 'logo': 'super',
}


def parse_keys(spec):
    """'ctrl+shift+t' -> (['ctrl', 'shift'], 't'). Raises ValueError."""
    parts = [p.strip() for p in spec.split('+') if p.strip()]
    if not parts:
        raise ValueError('empty key combination')
    mods = []
    for p in parts[:-1]:
        m = MODIFIER_ALIASES.get(p.lower())
        if not m:
            raise ValueError(f'unknown modifier {p!r}')
        mods.append(m)
    return mods, parts[-1]


def call_shell(method, bus=None, timeout=3000):
    bus = bus or Gio.bus_get_sync(Gio.BusType.SESSION, None)
    return bus.call_sync(ids.SHELL_BUS_NAME, ids.SHELL_OBJECT_PATH, ids.SHELL_BUS_NAME,
                         method, None, None, Gio.DBusCallFlags.NONE, timeout, None)


class ActionRunner:
    def __init__(self, key_sender=None):
        self.key_sender = key_sender
        self.log = []  # (action, ok) of recent actions, for status and tests
        self._bus = None

    def _record(self, action, ok):
        self.log = (self.log + [(action, ok)])[-20:]

    def _call_shell(self, action, method):
        try:
            if self._bus is None:
                self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            call_shell(method, self._bus)
            self._record(action, True)
        except GLib.Error as e:
            self._record(action, False)
            print(f'{ids.SLUG}: D-Bus {method} failed ({e.message}). '
                  f'Is the {ids.PRODUCT_NAME} extension enabled?', flush=True)

    def run(self, binding):
        action = binding.get('action', 'none')
        print(f'{ids.SLUG}: action {action}', flush=True)
        if action in DBUS_METHODS:
            threading.Thread(target=self._call_shell, args=(action, DBUS_METHODS[action]),
                             daemon=True).start()
        elif action == 'command':
            cmd = binding.get('command', '').strip()
            if cmd:
                subprocess.Popen(['/bin/sh', '-c', cmd], start_new_session=True,
                                 stdin=subprocess.DEVNULL)
            self._record(action, bool(cmd))
        elif action == 'keys':
            spec = binding.get('keys', '')
            if not self.key_sender:
                print(f'{ids.SLUG}: key output not available on this backend', flush=True)
                self._record(action, False)
                return
            try:
                mods, key = parse_keys(spec)
                self.key_sender.send(mods, key)
                self._record(action, True)
            except ValueError as e:
                self._record(action, False)
                print(f'{ids.SLUG}: bad key combination {shlex.quote(spec)}: {e}', flush=True)
        else:
            self._record(action, True)
