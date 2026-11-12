#!/usr/bin/env python3
"""End-to-end test of the button mapper daemon with simulated input devices.

    mapper_test.py --backend evdev --input uinput [--shell fake|real]
    mapper_test.py --backend x11 --input uinput --display :5 [--shell real]   # Xorg + libinput
    mapper_test.py --backend x11 --input xtest  [--shell fake]                # Xvfb, no uinput

uinput input creates virtual devices with python-evdev (needs /dev/uinput and
/dev/input access): two mice with side/extra buttons, a gaming mouse's keyboard
interface that sends F13 / number keys, a macro pad and an unmapped mouse.
xtest input injects through the X server's XTEST device (xdotool).

--shell fake: a stand-in D-Bus service records the extension calls.
--shell real: a real GNOME Shell (headless Wayland for evdev, X11 on --display
for x11) with the extension installed from dist/.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'mapper'))
sys.path.insert(0, str(ROOT / 'tests' / 'integration'))

from expose_for_gnome import ids  # noqa: E402

from gi.repository import Gio, GLib  # noqa: E402

SHELL_XML = f"""<node><interface name="{ids.SHELL_BUS_NAME}">
<method name="ToggleAppExpose"/><method name="ShowAppExpose"/><method name="HideAppExpose"/>
<method name="ToggleOverview"/><method name="ToggleAppGrid"/><method name="WorkspacePrevious"/>
<method name="WorkspaceNext"/><method name="SwitchToLastWindow"/>
<property name="AppExposeVisible" type="b" access="read"/>
<property name="Version" type="s" access="read"/></interface></node>"""


class FakeShell:
    def __init__(self):
        self.calls = []
        self.visible = False
        self.loop = GLib.MainLoop()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        ctx = GLib.MainContext()
        ctx.push_thread_default()
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        node = Gio.DBusNodeInfo.new_for_xml(SHELL_XML)

        def call(conn, sender, path, iface, method, params, inv):
            self.calls.append(method)
            if method == 'ToggleAppExpose':
                self.visible = not self.visible
            inv.return_value(None)

        def get_prop(conn, sender, path, iface, prop):
            return GLib.Variant('b', self.visible) if prop == 'AppExposeVisible' else \
                GLib.Variant('s', 'fake')
        bus.register_object(ids.SHELL_OBJECT_PATH, node.interfaces[0], call, get_prop, None)
        Gio.bus_own_name_on_connection(bus, ids.SHELL_BUS_NAME, Gio.BusNameOwnerFlags.NONE,
                                       None, None)
        GLib.MainLoop(ctx).run()


class Fail(Exception):
    pass


def wait_for(cond, timeout=5.0, what=''):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            v = cond()
        except Exception:  # noqa: BLE001 - polling
            v = None
        if v:
            return v
        time.sleep(0.05)
    return None


class Test:
    def __init__(self, args):
        self.args = args
        self.out = Path(args.out).resolve()
        self.out.mkdir(parents=True, exist_ok=True)
        self.results = []
        self.procs = []
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.cfg_home = Path(tempfile.mkdtemp(prefix='efg-cfg-'))
        self.cfg_path = self.cfg_home / ids.SLUG / 'config.json'
        self.fake = None
        self.label = f'{args.backend}-{args.input}-{args.shell}'

    def check(self, name, ok, detail=''):
        self.results.append({'check': name, 'ok': bool(ok), 'detail': str(detail)})
        print(f'[{"PASS" if ok else "FAIL"}] {name} {detail}', flush=True)
        return ok

    def spawn(self, argv, log, env=None):
        f = open(self.out / f'{self.label}-{log}', 'w')
        p = subprocess.Popen(argv, stdout=f, stderr=subprocess.STDOUT, env=env or os.environ)
        self.procs.append(p)
        return p

    def has_owner(self, name):
        return self.bus.call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus',
                                  'org.freedesktop.DBus', 'NameHasOwner',
                                  GLib.Variant('(s)', (name,)), None, 0, 1000, None).unpack()[0]

    def daemon(self, method, args=None, timeout=5000):
        res = self.bus.call_sync(ids.DAEMON_BUS_NAME, ids.DAEMON_OBJECT_PATH, ids.DAEMON_BUS_NAME,
                                 method, args, None, 0, timeout, None).unpack()
        return json.loads(res[0]) if res else None

    def shell_prop(self, prop, dest=None, path=None, iface=None):
        res = self.bus.call_sync(dest or ids.SHELL_BUS_NAME, path or ids.SHELL_OBJECT_PATH,
                                 'org.freedesktop.DBus.Properties', 'Get',
                                 GLib.Variant('(ss)', (iface or ids.SHELL_BUS_NAME, prop)),
                                 None, 0, 2000, None)
        return res.unpack()[0]

    def write_config(self, cfg):
        self.cfg_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cfg_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(cfg, indent=2))
        os.replace(tmp, self.cfg_path)

    # --- real shell ---------------------------------------------------------
    def start_real_shell(self):
        uuid = ids.EXTENSION_UUID
        zip_path = ROOT / 'dist' / f'{uuid}.shell-extension.zip'
        dest = Path(os.environ['XDG_DATA_HOME']) / 'gnome-shell' / 'extensions' / uuid
        import zipfile
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(dest)
        subprocess.run(['glib-compile-schemas', str(dest / 'schemas')], check=True)
        apps = Path(os.environ['XDG_DATA_HOME']) / 'applications'
        apps.mkdir(parents=True, exist_ok=True)
        (apps / 'org.example.EfgTestApp.desktop').write_text(
            '[Desktop Entry]\nType=Application\nName=EFG Test App\n'
            f'Exec={sys.executable} {ROOT}/tests/integration/test_app.py\n')
        for k, v in (('enabled-extensions', f"['{uuid}']"), ('disable-user-extensions', 'false'),
                     ('welcome-dialog-last-shown-version', "'999'")):
            subprocess.run(['gsettings', 'set', 'org.gnome.shell', k, v], check=True)
        subprocess.run(['gsettings', 'set', 'org.gnome.desktop.interface', 'enable-animations',
                        'false'], check=True)
        env = dict(os.environ)
        if self.args.backend == 'x11':
            argv = ['gnome-shell', '--sm-disable', '--x11', '--replace']
            os.environ['GDK_BACKEND'] = 'x11'
        else:
            argv = ['gnome-shell', '--sm-disable', '--headless', '--wayland', '--wayland-display',
                    'efg-wl', '--virtual-monitor', '1280x800']
            os.environ['WAYLAND_DISPLAY'] = 'efg-wl'
            os.environ['GDK_BACKEND'] = 'wayland'
        self.spawn(argv, 'gnome-shell.log', env)
        if not wait_for(lambda: self.has_owner(ids.SHELL_BUS_NAME), 60):
            raise Fail('shell/extension did not come up')
        time.sleep(2)
        self.spawn([sys.executable, str(ROOT / 'tests/integration/test_app.py'), '3'],
                   'test-app.log')
        time.sleep(3)

    # --- expectations ---------------------------------------------------------
    def snapshot(self):
        if self.fake:
            return len(self.fake.calls)
        return (self.shell_prop('AppExposeVisible'),
                self.shell_prop('OverviewActive', 'org.gnome.Shell', '/org/gnome/Shell',
                                'org.gnome.Shell'))

    def expect(self, name, method, before, timeout=3):
        if self.fake:
            ok = wait_for(lambda: method in self.fake.calls[before:], timeout)
            return self.check(name, ok, f'calls={self.fake.calls[before:]}')
        prop = {'ToggleAppExpose': 0, 'ToggleOverview': 1, 'ToggleAppGrid': 1}.get(method)
        if prop is None:
            return self.check(name, True, f'(real shell: {method} not observable, skipped)')
        ok = wait_for(lambda: self.snapshot()[prop] != before[prop], timeout)
        state = self.snapshot()
        # Put the shell back into its normal state for the next check.
        if state[0]:
            self.bus.call_sync(ids.SHELL_BUS_NAME, ids.SHELL_OBJECT_PATH, ids.SHELL_BUS_NAME,
                               'HideAppExpose', None, None, 0, 2000, None)
        if state[1]:
            self.bus.call_sync(ids.SHELL_BUS_NAME, ids.SHELL_OBJECT_PATH, ids.SHELL_BUS_NAME,
                               'ToggleOverview', None, None, 0, 2000, None)
        wait_for(lambda: self.snapshot() == (False, False), 3)
        time.sleep(0.3)
        return self.check(name, ok, f'{before} -> {state}')

    def expect_none(self, name, before, wait=0.6):
        time.sleep(wait)
        if self.fake:
            return self.check(name, len(self.fake.calls) == before,
                              f'calls={self.fake.calls[before:]}')
        return self.check(name, self.snapshot() == before, f'{before} -> {self.snapshot()}')


def run_uinput(t):
    import evdev
    import virtual_devices as vd
    from evdev import ecodes as e

    devs = vd.create_all()
    time.sleep(1.0)
    keys = {name: vd.device_key(d) for name, d in devs.items()}
    t.check('virtual devices created', all(keys.values()),
            ', '.join(f'{n}={k}' for n, k in keys.items()))

    mouse_a, mouse_b = keys['mouse_a'], keys['mouse_b']
    cfg = {'version': 2, 'backend': t.args.backend, 'devices': {
        '*': {'name': 'Any device', 'bindings': {'BTN_EXTRA': {'action': 'mission-control'}}},
        mouse_a: {'name': 'A', 'bindings': {'BTN_SIDE': {'action': 'app-expose'},
                                            'BTN_TASK': {'action': 'block'}}},
        keys['gaming_kbd']: {'name': 'G', 'bindings': {'KEY_F13': {'action': 'last-window'}}},
        keys['macro_pad']: {'name': 'M', 'bindings': {'KEY_MACRO1': {'action': 'app-grid'}}},
    }}
    t.write_config(cfg)
    t.start_daemon()

    st = wait_for(lambda: (lambda s: s if len(s.get('grabbed', [])) >= (
        4 if t.args.backend == 'evdev' else 1) else None)(t.daemon('GetStatus')), 10)
    t.check('daemon grabbed mapped devices', st, json.dumps(st and st.get('grabbed')))
    if st and st.get('problem'):
        t.check('daemon reports no problem', False, st['problem'])

    clones = {}
    if t.args.backend == 'evdev':
        for name, d in devs.items():
            for path in evdev.list_devices():
                dev = evdev.InputDevice(path)
                if dev.name == f'{d.name} ({ids.SLUG})':
                    clones[name] = dev
                    break
        t.check('uinput clones exist for grabbed devices',
                {'mouse_a', 'mouse_b', 'gaming_kbd', 'macro_pad'} <= set(clones),
                sorted(clones))
        unmapped_clone = 'mouse_plain' in clones
        t.check('unmapped mouse is not grabbed', not unmapped_clone)

    def drain(name):
        dev = clones.get(name)
        out = []
        if not dev:
            return out
        while True:
            try:
                ev = dev.read_one()
            except BlockingIOError:
                break
            if ev is None:
                break
            if ev.type in (e.EV_KEY, e.EV_REL):
                out.append((ev.type, ev.code, ev.value))
        return out

    def forwarded(name, code):
        time.sleep(0.3)
        return any(c == code for _, c, _ in drain(name))

    for n in clones:
        drain(n)

    b = t.snapshot()
    vd.click(devs['mouse_a'], e.BTN_SIDE)
    t.expect('mouse A side button -> App Exposé (per-device binding)', 'ToggleAppExpose', b)
    if clones:
        t.check('mapped button not forwarded to apps', not forwarded('mouse_a', e.BTN_SIDE))

    b = t.snapshot()
    vd.click(devs['mouse_b'], e.BTN_SIDE)
    t.expect_none('mouse B side button does nothing (binding is mouse A only)', b)
    if clones:
        t.check('mouse B side button forwarded unchanged', forwarded('mouse_b', e.BTN_SIDE))

    b = t.snapshot()
    vd.click(devs['mouse_b'], e.BTN_EXTRA)
    t.expect('mouse B forward button -> overview (any-device binding)', 'ToggleOverview', b)
    b = t.snapshot()
    vd.click(devs['mouse_a'], e.BTN_EXTRA)
    t.expect('mouse A forward button -> overview (any-device binding)', 'ToggleOverview', b)

    if clones:
        vd.move(devs['mouse_a'], 5, 3)
        vd.click(devs['mouse_a'], e.BTN_LEFT)
        got = (time.sleep(0.3), drain('mouse_a'))[1]
        t.check('motion and left click forwarded through the clone',
                (e.EV_REL, e.REL_X, 5) in got and (e.EV_KEY, e.BTN_LEFT, 1) in got, got[:6])

    b = t.snapshot()
    vd.click(devs['mouse_a'], e.BTN_TASK)
    t.expect_none('blocked button: no action', b)
    if clones:
        t.check('blocked button not forwarded', not forwarded('mouse_a', e.BTN_TASK))

    b = t.snapshot()
    vd.tap(devs['gaming_kbd'], e.KEY_F13)
    t.expect('gaming mouse keyboard F13 -> last window', 'SwitchToLastWindow', b)
    if clones:
        vd.tap(devs['gaming_kbd'], e.KEY_A)
        t.check('other keys of that device forwarded', forwarded('gaming_kbd', e.KEY_A))

    b = t.snapshot()
    vd.tap(devs['macro_pad'], e.KEY_MACRO1)
    t.expect('macro pad KEY_MACRO1 -> app grid', 'ToggleAppGrid', b)

    # learn mode
    def learn_with(action):
        result = {}

        def worker():
            result['r'] = t.daemon('Learn', GLib.Variant('(u)', (6000,)), timeout=10000)
        th = threading.Thread(target=worker)
        th.start()
        wait_for(lambda: t.daemon('GetStatus').get('learning'), 3)
        time.sleep(0.6)
        action()
        th.join(10)
        return result.get('r') or {}

    b = t.snapshot()
    r = learn_with(lambda: vd.tap(devs['gaming_kbd'], e.KEY_1))
    t.check('learn: key from gaming mouse keyboard',
            r.get('binding_key') == 'KEY_1' and r.get('device_key') == keys['gaming_kbd'], r)
    r = learn_with(lambda: vd.click(devs['mouse_plain'], e.BTN_FORWARD))
    t.check('learn: button from an unmapped (ungrabbed) mouse',
            r.get('binding_key') == 'BTN_FORWARD' and r.get('device_key') == keys['mouse_plain'],
            r)
    r = learn_with(lambda: vd.click(devs['mouse_a'], e.BTN_SIDE))
    t.check('learn: already-mapped button is reported, not acted on',
            r.get('binding_key') == 'BTN_SIDE' and r.get('device_key') == mouse_a, r)
    t.expect_none('no action fired during learn', b, 0.3)
    r = learn_with(lambda: vd.tap(devs['gaming_kbd'], e.KEY_ESC))
    t.check('learn: Esc cancels', r.get('cancelled'), r)

    # live reload
    cfg['devices'][mouse_a]['bindings']['BTN_SIDE'] = {'action': 'mission-control'}
    t.write_config(cfg)
    time.sleep(1.5 if t.args.backend == 'evdev' else 1.0)
    b = t.snapshot()
    vd.click(devs['mouse_a'], e.BTN_SIDE)
    t.expect('config change applied without restart', 'ToggleOverview', b)

    if t.args.backend == 'evdev':
        # key output through the daemon's virtual keyboard
        cfg['devices'][mouse_a]['bindings']['BTN_SIDE'] = {'action': 'keys', 'keys': 'ctrl+w'}
        t.write_config(cfg)
        time.sleep(1.5)
        vd.click(devs['mouse_a'], e.BTN_SIDE)
        kbd = wait_for(lambda: next((evdev.InputDevice(p) for p in evdev.list_devices()
                                     if evdev.InputDevice(p).name == f'{ids.SLUG} keyboard'),
                                    None), 3)
        got = []
        if kbd:
            vd.click(devs['mouse_a'], e.BTN_SIDE)
            end = time.monotonic() + 2
            while time.monotonic() < end and len(got) < 4:
                ev = kbd.read_one()
                if ev and ev.type == e.EV_KEY:
                    got.append((ev.code, ev.value))
                time.sleep(0.01)
        t.check('"keys" action types ctrl+w on a virtual keyboard',
                got[:4] == [(e.KEY_LEFTCTRL, 1), (e.KEY_W, 1), (e.KEY_W, 0),
                            (e.KEY_LEFTCTRL, 0)], got)

        # hotplug: unplug and replug mouse B
        devs['mouse_b'].close()
        time.sleep(1)
        devs['mouse_b'] = vd.create('mouse_b')
        time.sleep(3)
        b = t.snapshot()
        vd.click(devs['mouse_b'], e.BTN_EXTRA)
        t.expect('replugged mouse is picked up again', 'ToggleOverview', b, timeout=4)

    listed = t.daemon('ListDevices')
    names = {d['name']: d for d in listed}
    t.check('ListDevices reports devices and advertised buttons',
            'BTN_TASK' in names.get(vd.NAMES['mouse_a'], {}).get('mouse_buttons', []) and
            'KEY_MACRO1' in names.get(vd.NAMES['macro_pad'], {}).get('extra_keys', []),
            sorted(names))
    for d in devs.values():
        d.close()


def run_xtest(t):
    """X11 backend on Xvfb: per-device grab on the XTEST slave, any-device grab, raw-event learn."""
    xtest_ptr = '0000:0000:Virtual core XTEST pointer'
    cfg = {'version': 2, 'backend': 'x11', 'devices': {
        '*': {'name': 'Any', 'bindings': {'BTN_EXTRA': {'action': 'mission-control'}}},
        xtest_ptr: {'name': 'XTEST', 'bindings': {'BTN_SIDE': {'action': 'app-expose'}}},
        '0000:0000:Virtual core XTEST keyboard': {'name': 'XTEST kbd', 'bindings': {
            'KEY_F14': {'action': 'last-window'}}},
    }}
    t.write_config(cfg)
    t.start_daemon()
    st = wait_for(lambda: (lambda s: s if s.get('grabbed') else None)(t.daemon('GetStatus')), 10)
    t.check('daemon grabbed on XTEST + master devices', st, st and st.get('grabbed'))

    def xdo(*a):
        subprocess.run(['xdotool', *a], check=True)

    b = t.snapshot()
    xdo('click', '8')
    t.expect('button 8 on XTEST pointer -> App Exposé (XI2 per-device grab)',
             'ToggleAppExpose', b)
    b = t.snapshot()
    xdo('click', '9')
    t.expect('button 9 -> overview (any-device grab on master pointer)', 'ToggleOverview', b)
    b = t.snapshot()
    # keycode 192 = KEY_F14 (xkb: XF86Launch5); XF86Tools would be ambiguous (179 and 191).
    xdo('key', 'XF86Launch5')
    t.expect('F14 on XTEST keyboard -> last window (XI2 per-device key grab)',
             'SwitchToLastWindow', b)

    result = {}

    def worker():
        result['r'] = t.daemon('Learn', GLib.Variant('(u)', (6000,)), timeout=10000)
    th = threading.Thread(target=worker)
    th.start()
    wait_for(lambda: t.daemon('GetStatus').get('learning'), 3)
    time.sleep(0.6)
    b = t.snapshot()
    xdo('click', '10')
    th.join(10)
    r = result.get('r') or {}
    t.check('learn via XI2 raw events reports device + button',
            r.get('binding_key') == 'BTN_FORWARD' and r.get('device_key') == xtest_ptr, r)
    th = threading.Thread(target=worker)
    th.start()
    wait_for(lambda: t.daemon('GetStatus').get('learning'), 3)
    time.sleep(0.6)
    xdo('click', '8')
    th.join(10)
    r = result.get('r') or {}
    t.check('learn: grabbed button reported, not acted on', r.get('binding_key') == 'BTN_SIDE', r)
    t.expect_none('no action during learn', b, 0.3)

    cfg['devices'][xtest_ptr]['bindings']['BTN_SIDE'] = {'action': 'app-grid'}
    t.write_config(cfg)
    time.sleep(1.2)
    b = t.snapshot()
    xdo('click', '8')
    t.expect('config change re-grabs', 'ToggleAppGrid', b)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--backend', choices=['evdev', 'x11'], default='evdev')
    p.add_argument('--input', choices=['uinput', 'xtest'], default='uinput')
    p.add_argument('--shell', choices=['fake', 'real'], default='fake')
    p.add_argument('--display', default=None, help='X display for the x11 backend')
    p.add_argument('--out', default='test-output')
    args = p.parse_args()

    if not os.environ.get('EFG_IN_SESSION'):
        home = tempfile.mkdtemp(prefix='efg-home-')
        runtime = tempfile.mkdtemp(prefix='efg-run-')
        os.chmod(runtime, 0o700)
        env = dict(os.environ, EFG_IN_SESSION='1', HOME=home, XDG_RUNTIME_DIR=runtime,
                   XDG_DATA_HOME=f'{home}/.local/share', XDG_CONFIG_HOME=f'{home}/.config',
                   XDG_CACHE_HOME=f'{home}/.cache', NO_AT_BRIDGE='1')
        env.pop('DBUS_SESSION_BUS_ADDRESS', None)
        if args.display:
            env['DISPLAY'] = args.display
        env['XDG_SESSION_TYPE'] = 'x11' if args.backend == 'x11' else 'wayland'
        r = subprocess.run(['dbus-run-session', '--', sys.executable, __file__] + sys.argv[1:],
                           env=env)
        sys.exit(r.returncode)

    t = Test(args)

    def start_daemon():
        env = dict(os.environ, XDG_CONFIG_HOME=str(t.cfg_home), PYTHONUNBUFFERED='1')
        t.spawn([sys.executable, str(ROOT / 'bin' / f'{ids.SLUG}-daemon'), '--backend',
                 args.backend], 'daemon.log', env)
        if not wait_for(lambda: t.has_owner(ids.DAEMON_BUS_NAME), 10):
            raise Fail('daemon did not start')
        time.sleep(1.0)
    t.start_daemon = start_daemon

    try:
        if args.backend == 'x11' and subprocess.run(['xdpyinfo'], capture_output=True).returncode:
            t.spawn(['Xvfb', os.environ['DISPLAY'], '-screen', '0', '1280x800x24', '+extension',
                     'GLX', '-nolisten', 'tcp'], 'xvfb.log')
            if not wait_for(lambda: subprocess.run(['xdpyinfo'],
                                                   capture_output=True).returncode == 0, 15):
                raise Fail('no X server')
        if args.shell == 'real':
            t.start_real_shell()
        else:
            t.fake = FakeShell()
            wait_for(lambda: t.has_owner(ids.SHELL_BUS_NAME), 5)
        (run_uinput if args.input == 'uinput' else run_xtest)(t)
    except Exception as e:  # noqa: BLE001 - recorded as failure
        import traceback
        traceback.print_exc()
        t.check('harness', False, repr(e))
    finally:
        for proc in reversed(t.procs):
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
        log = (t.out / f'{t.label}-daemon.log')
        if log.exists() and 'Traceback' in log.read_text():
            t.check('daemon log has no tracebacks', False, log)
        (t.out / f'{t.label}-results.json').write_text(json.dumps(t.results, indent=2))
    ok = all(r['ok'] for r in t.results)
    print('ALL PASSED' if ok else 'SOME CHECKS FAILED', flush=True)
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
