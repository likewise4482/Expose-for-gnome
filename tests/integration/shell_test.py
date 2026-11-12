#!/usr/bin/env python3
"""End-to-end test of the extension in a throwaway GNOME Shell.

    tests/integration/shell_test.py --mode headless   # Wayland, no display needed
    tests/integration/shell_test.py --mode nested     # Wayland, nested in Xvfb
    tests/integration/shell_test.py --mode x11        # X11 shell on Xvfb

Runs in a private D-Bus session with a temporary HOME, installs the extension
zip (and optionally another extension, e.g. the original personal build, to
check that both coexist), opens a 3-window test app and drives App Exposé over
D-Bus. Screenshots and the shell log go to --out. Exit status 0 = all passed.

Screenshots and window introspection are restricted in GNOME 45+; this script
claims a portal bus name that the shell allows (fine in the private session).
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
import sync_ids  # noqa: E402

ENV = sync_ids.load_env()
UUID = ENV['EXTENSION_UUID']
BUS = ENV['SHELL_BUS_NAME']
PATH = ENV['SHELL_OBJECT_PATH']
ALLOWED_NAME = 'org.freedesktop.impl.portal.desktop.gnome'
TEST_APP_ID = 'org.example.EfgTestApp'


class Fail(Exception):
    pass


def run(*args, check=True, **kw):
    r = subprocess.run(args, capture_output=True, text=True, **kw)
    if check and r.returncode:
        raise Fail(f'{args}: {r.stderr.strip() or r.stdout.strip()}')
    return r.stdout.strip()


def wait_for(cond, timeout=30, what='condition'):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            v = cond()
            if v:
                return v
        except Exception:  # noqa: BLE001 - polling
            pass
        time.sleep(0.25)
    raise Fail(f'timed out waiting for {what}')


class Harness:
    def __init__(self, args):
        self.args = args
        self.out = Path(args.out).resolve()
        self.out.mkdir(parents=True, exist_ok=True)
        self.results = []
        self.procs = []
        from gi.repository import Gio, GLib
        self.Gio, self.GLib = Gio, GLib
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

    # --- helpers ------------------------------------------------------------
    def check(self, name, ok, detail=''):
        self.results.append({'check': name, 'ok': bool(ok), 'detail': str(detail)})
        print(f'[{"PASS" if ok else "FAIL"}] {name} {detail}', flush=True)
        return ok

    def call(self, dest, path, iface, method, args=None, timeout=5000):
        res = self.bus.call_sync(dest, path, iface, method, args, None,
                                 self.Gio.DBusCallFlags.NONE, timeout, None)
        return res.unpack() if res else None

    def has_owner(self, name):
        return self.call('org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus',
                         'NameHasOwner', self.GLib.Variant('(s)', (name,)))[0]

    def prop(self, name, dest=BUS, path=PATH, iface=BUS):
        return self.call(dest, path, 'org.freedesktop.DBus.Properties', 'Get',
                         self.GLib.Variant('(ss)', (iface, name)))[0]

    def ext_info(self, uuid):
        return self.call('org.gnome.Shell', '/org/gnome/Shell',
                         'org.gnome.Shell.Extensions', 'GetExtensionInfo',
                         self.GLib.Variant('(s)', (uuid,)))[0]

    def expose(self, method):
        self.call(BUS, PATH, BUS, method)

    def windows(self):
        res = self.call('org.gnome.Shell', '/org/gnome/Shell/Introspect',
                        'org.gnome.Shell.Introspect', 'GetWindows')[0]
        return [dict(v, id=k) for k, v in res.items()]

    def focused(self):
        return next((w for w in self.windows() if w.get('has-focus')), None)

    def screenshot(self, name):
        path = str(self.out / f'{self.args.mode}-{name}.png')
        ok, used = self.call('org.gnome.Shell', '/org/gnome/Shell/Screenshot',
                             'org.gnome.Shell.Screenshot', 'Screenshot',
                             self.GLib.Variant('(bbs)', (False, False, path)), timeout=10000)
        return used if ok else None

    def spawn(self, args, log, env=None):
        f = open(self.out / log, 'w')
        p = subprocess.Popen(args, stdout=f, stderr=subprocess.STDOUT, env=env or os.environ)
        self.procs.append(p)
        return p

    # --- setup --------------------------------------------------------------
    def install_extension(self, zip_path):
        with zipfile.ZipFile(zip_path) as z:
            uuid = json.loads(z.read('metadata.json'))['uuid']
            dest = Path(os.environ['XDG_DATA_HOME']) / 'gnome-shell' / 'extensions' / uuid
            z.extractall(dest)
        if (dest / 'schemas').is_dir():
            run('glib-compile-schemas', str(dest / 'schemas'))
        return uuid

    def setup(self):
        uuids = [self.install_extension(self.args.zip)]
        for extra in self.args.also:
            uuids.append(self.install_extension(extra))
        apps = Path(os.environ['XDG_DATA_HOME']) / 'applications'
        apps.mkdir(parents=True, exist_ok=True)
        (apps / f'{TEST_APP_ID}.desktop').write_text(
            '[Desktop Entry]\nType=Application\nName=EFG Test App\n'
            f'Exec={sys.executable} {ROOT}/tests/integration/test_app.py\n')
        for key, value in (('enabled-extensions', str(uuids)),
                           ('disable-user-extensions', 'false'),
                           ('welcome-dialog-last-shown-version', "'999'"),
                           ('disable-extension-version-validation', 'false')):
            run('gsettings', 'set', 'org.gnome.shell', key, value)
        run('gsettings', 'set', 'org.gnome.desktop.interface', 'enable-animations', 'false')
        return uuids

    def start_shell(self):
        env = dict(os.environ)
        mode = self.args.mode
        if mode in ('x11', 'nested'):
            display = f':{self.args.display}'
            self.spawn(['Xvfb', display, '-screen', '0', '1280x800x24', '+extension', 'GLX',
                        '-nolisten', 'tcp'], 'xvfb.log')
            env['DISPLAY'] = display
            os.environ['DISPLAY'] = display
            wait_for(lambda: subprocess.run(['xdpyinfo'], capture_output=True,
                                            env=env).returncode == 0, 15, 'Xvfb')
        args = ['gnome-shell', '--sm-disable']
        if mode == 'headless':
            args += ['--headless', '--wayland', '--wayland-display', 'efg-wayland',
                     '--virtual-monitor', '1280x800']
        elif mode == 'nested':
            env['MUTTER_DEBUG_DUMMY_MODE_SPECS'] = '1280x800'
            args += ['--nested', '--wayland', '--wayland-display', 'efg-wayland']
        else:
            args += ['--x11', '--replace']
        if mode != 'x11':
            os.environ['WAYLAND_DISPLAY'] = 'efg-wayland'
            os.environ['GDK_BACKEND'] = 'wayland'
            os.environ.pop('DISPLAY', None) if mode == 'headless' else None
            os.environ['XDG_SESSION_TYPE'] = 'wayland'
        else:
            os.environ['GDK_BACKEND'] = 'x11'
            os.environ['XDG_SESSION_TYPE'] = 'x11'
        self.shell = self.spawn(args, 'gnome-shell.log', env)
        wait_for(lambda: self.prop('ShellVersion', 'org.gnome.Shell', '/org/gnome/Shell',
                                   'org.gnome.Shell'), 60, 'org.gnome.Shell on the bus')
        time.sleep(2)

    # --- tests --------------------------------------------------------------
    def run_tests(self, uuids):
        version = self.prop('ShellVersion', 'org.gnome.Shell', '/org/gnome/Shell',
                            'org.gnome.Shell')
        self.check('shell started', True, f'GNOME Shell {version}, mode {self.args.mode}')
        info = self.ext_info(UUID)
        self.check('extension state is running (1)', info.get('state') == 1,
                   f'state={info.get("state")} error={info.get("error", "")!r}')
        if not self.check('extension owns its D-Bus name',
                          wait_for(lambda: self.has_owner(BUS), 15, BUS), BUS):
            return
        self.check('Version property', self.prop('Version') == ENV['VERSION'], self.prop('Version'))
        for other in uuids[1:]:
            st = self.ext_info(other)
            self.check(f'other extension {other} running alongside', st.get('state') == 1,
                       f'state={st.get("state")} error={st.get("error", "")!r}')

        self.bus.call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus',
                           'RequestName', self.GLib.Variant('(su)', (ALLOWED_NAME, 4)), None,
                           self.Gio.DBusCallFlags.NONE, 2000, None)
        time.sleep(1)

        self.spawn([sys.executable, str(ROOT / 'tests/integration/test_app.py'), '3'],
                   'test-app.log')
        wins = wait_for(lambda: [w for w in self.windows() if 'Test window' in w.get('title', '')]
                        if len([w for w in self.windows()
                                if 'Test window' in w.get('title', '')]) >= 3 else None,
                        30, '3 test windows')
        self.check('3 test windows mapped', len(wins) == 3,
                   ', '.join(f'{w["title"]} ({w.get("app-id")})' for w in wins))
        time.sleep(1)
        self.screenshot('0-desktop')

        before = self.focused()
        self.expose('ShowAppExpose')
        visible = wait_for(lambda: self.prop('AppExposeVisible'), 5, 'App Exposé visible')
        self.check('ShowAppExpose opens App Exposé', visible)
        time.sleep(0.5)
        shot = self.screenshot('1-app-expose')
        self.check('screenshot of App Exposé', shot, shot)
        self.expose('HideAppExpose')
        hidden = wait_for(lambda: not self.prop('AppExposeVisible'), 5, 'App Exposé hidden')
        self.check('HideAppExpose closes it', hidden)

        self.expose('ToggleAppExpose')
        wait_for(lambda: self.prop('AppExposeVisible'), 5, 'toggle on')
        self.expose('ToggleAppExpose')
        self.check('ToggleAppExpose toggles', wait_for(lambda: not self.prop('AppExposeVisible'),
                                                       5, 'toggle off'))

        self.expose('ToggleOverview')
        ov = wait_for(lambda: self.prop('OverviewActive', 'org.gnome.Shell', '/org/gnome/Shell',
                                        'org.gnome.Shell'), 5, 'overview')
        self.check('ToggleOverview opens the overview', ov)
        self.screenshot('2-overview')
        self.expose('ToggleOverview')
        wait_for(lambda: not self.prop('OverviewActive', 'org.gnome.Shell', '/org/gnome/Shell',
                                       'org.gnome.Shell'), 5, 'overview closed')
        time.sleep(0.5)

        before = self.focused()
        self.expose('SwitchToLastWindow')
        time.sleep(0.7)
        after = self.focused()
        self.check('SwitchToLastWindow changes focus',
                   before and after and before['id'] != after['id'],
                   f'{before and before.get("title")} -> {after and after.get("title")}')
        self.expose('SwitchToLastWindow')
        time.sleep(0.7)
        back = self.focused()
        self.check('SwitchToLastWindow again goes back', back and before and
                   back['id'] == before['id'], back and back.get('title'))

        self.expose('ShowAppExpose')
        wait_for(lambda: self.prop('AppExposeVisible'), 5, 'reopen')
        self.call('org.gnome.Shell', '/org/gnome/Shell',
                  'org.gnome.Shell.Extensions', 'DisableExtension',
                  self.GLib.Variant('(s)', (UUID,)))
        released = wait_for(lambda: not self.has_owner(BUS), 10, 'name released')
        self.check('disable() while open releases D-Bus name', released)
        time.sleep(0.5)
        self.screenshot('3-after-disable')
        self.call('org.gnome.Shell', '/org/gnome/Shell',
                  'org.gnome.Shell.Extensions', 'EnableExtension',
                  self.GLib.Variant('(s)', (UUID,)))
        self.check('re-enable works', wait_for(lambda: self.has_owner(BUS), 10, 're-enable'))

    def finish(self):
        for p in reversed(self.procs):
            p.terminate()
        for p in self.procs:
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
        log = (self.out / 'gnome-shell.log').read_text(errors='ignore')
        ours = [ln for ln in log.splitlines()
                if ('JS ERROR' in ln or 'JS WARNING' in ln or 'Extension' in ln)
                and (UUID in ln or 'extension.js' in ln or 'prefs.js' in ln)]
        self.check('no JS errors/warnings from the extension in the shell log', not ours,
                   ' | '.join(ours[:5]))
        clashes = [ln for ln in log.splitlines() if 'keybinding' in ln and
                   ('re-add' in ln or 'non-existent' in ln)]
        self.check('no keybinding name clashes (side-by-side installs)', not clashes,
                   ' | '.join(clashes[:3]))
        (self.out / f'{self.args.mode}-results.json').write_text(json.dumps(self.results,
                                                                             indent=2))
        return all(r['ok'] for r in self.results)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--mode', choices=['headless', 'nested', 'x11'], default='headless')
    p.add_argument('--zip', default=str(ROOT / 'dist' / f'{UUID}.shell-extension.zip'))
    p.add_argument('--also', action='append', default=[],
                   help='another extension zip to install and enable alongside')
    p.add_argument('--out', default='test-output')
    p.add_argument('--display', type=int, default=97)
    args = p.parse_args()

    if not os.environ.get('EFG_IN_SESSION'):
        home = tempfile.mkdtemp(prefix='efg-home-')
        runtime = tempfile.mkdtemp(prefix='efg-run-')
        os.chmod(runtime, 0o700)
        env = dict(os.environ, EFG_IN_SESSION='1', HOME=home, XDG_RUNTIME_DIR=runtime,
                   XDG_DATA_HOME=f'{home}/.local/share', XDG_CONFIG_HOME=f'{home}/.config',
                   XDG_CACHE_HOME=f'{home}/.cache', GSETTINGS_BACKEND='dconf',
                   NO_AT_BRIDGE='1', XDG_CURRENT_DESKTOP='GNOME')
        env.pop('DBUS_SESSION_BUS_ADDRESS', None)
        env.pop('WAYLAND_DISPLAY', None)
        argv = [sys.executable, __file__] + sys.argv[1:]
        args.zip = str(Path(args.zip).resolve())
        argv = ['dbus-run-session', '--'] + argv
        r = subprocess.run(argv, env=env)
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(runtime, ignore_errors=True)
        sys.exit(r.returncode)

    h = Harness(args)
    ok = False
    try:
        uuids = h.setup()
        h.start_shell()
        h.run_tests(uuids)
    except Exception as e:  # noqa: BLE001 - recorded as a failed check
        h.check('harness', False, repr(e))
    finally:
        ok = h.finish()
    print('ALL PASSED' if ok else 'SOME CHECKS FAILED', flush=True)
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
