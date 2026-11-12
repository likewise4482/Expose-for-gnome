"""expose-for-gnome-ctl: trigger shell actions and inspect the mapper from a terminal."""
import argparse
import json
import sys

from gi.repository import Gio, GLib

from . import actions, devices, ids

VERBS = {
    'app-expose': 'ToggleAppExpose', 'expose': 'ToggleAppExpose',
    'show': 'ShowAppExpose', 'hide': 'HideAppExpose',
    'overview': 'ToggleOverview', 'mission-control': 'ToggleOverview',
    'app-grid': 'ToggleAppGrid', 'apps': 'ToggleAppGrid',
    'workspace-prev': 'WorkspacePrevious', 'workspace-next': 'WorkspaceNext',
    'last-window': 'SwitchToLastWindow',
}


def daemon_call(bus, method, args=None, timeout=5000):
    res = bus.call_sync(ids.DAEMON_BUS_NAME, ids.DAEMON_OBJECT_PATH, ids.DAEMON_BUS_NAME,
                        method, args, None, Gio.DBusCallFlags.NONE, timeout, None)
    out = res.unpack()
    return json.loads(out[0]) if out else None


def main(argv=None):
    p = argparse.ArgumentParser(prog=f'{ids.SLUG}-ctl', description=ids.PRODUCT_NAME)
    p.add_argument('verb', choices=sorted(VERBS) + ['status', 'devices', 'hidpp', 'learn'],
                   help='shell action, or: status (shell + mapper), devices (input devices '
                        'and advertised buttons), hidpp (Logitech controls), learn (print the '
                        'next button/key pressed)')
    p.add_argument('--all', action='store_true', help='devices: include every input device')
    p.add_argument('--json', action='store_true', help='machine readable output')
    args = p.parse_args(argv)
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    try:
        if args.verb in VERBS:
            actions.call_shell(VERBS[args.verb], bus)
            return 0
        if args.verb == 'devices':
            devs = [d.to_dict() for d in devices.list_devices(include_all=args.all)]
            if args.json:
                print(json.dumps(devs, indent=2))
            for d in [] if args.json else devs:
                print(f'{d["path"]}  {d["name"]}  [{d["kind"]}, {d["bus"]}, '
                      f'{d["vendor"]}:{d["product"]}]\n    key: {d["key"]}')
                if d['mouse_buttons']:
                    print('    buttons: ' + ' '.join(d['mouse_buttons']))
                if d['extra_keys']:
                    print('    extra keys: ' + ' '.join(d['extra_keys'][:30]))
            return 0
        if args.verb == 'status':
            out = {}
            for prop in ('Version', 'ShellVersion', 'AppExposeVisible'):
                try:
                    res = bus.call_sync(ids.SHELL_BUS_NAME, ids.SHELL_OBJECT_PATH,
                                        'org.freedesktop.DBus.Properties', 'Get',
                                        GLib.Variant('(ss)', (ids.SHELL_BUS_NAME, prop)), None,
                                        Gio.DBusCallFlags.NONE, 2000, None)
                    out[prop] = res.unpack()[0]
                except GLib.Error as e:
                    out['extension_error'] = e.message
                    break
            try:
                out['mapper'] = daemon_call(bus, 'GetStatus')
            except GLib.Error as e:
                out['mapper_error'] = e.message
            print(json.dumps(out, indent=2))
            return 0
        if args.verb == 'hidpp':
            print(json.dumps(daemon_call(bus, 'ListHidppDevices', timeout=30000), indent=2))
            return 0
        if args.verb == 'learn':
            print('Press a button or key (Esc cancels)…', file=sys.stderr)
            print(json.dumps(daemon_call(bus, 'Learn', GLib.Variant('(u)', (15000,)),
                                         timeout=20000), indent=2))
            return 0
    except GLib.Error as e:
        print(f'{ids.SLUG}-ctl: {e.message}', file=sys.stderr)
        return 1
    return 2


if __name__ == '__main__':
    sys.exit(main())
