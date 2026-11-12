"""GTK4/libadwaita app: per-device button mappings, learn mode, Logitech
controls and the first-run / permission setup."""
import json
import os
import subprocess
import sys

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from . import actions, codes, config, devices, ids, permissions  # noqa: E402

ACTION_IDS = list(config.ACTIONS)
BACKEND_IDS = list(config.BACKENDS)
KIND_ICONS = {'mouse': 'input-mouse-symbolic', 'trackball': 'input-mouse-symbolic',
              'keyboard': 'input-keyboard-symbolic', 'keys': 'input-keyboard-symbolic',
              'buttons': 'input-gaming-symbolic', 'touchpad': 'input-touchpad-symbolic'}
LEARN_TIMEOUT_MS = 15000


def systemctl(*args):
    try:
        r = subprocess.run(['systemctl', '--user', *args], capture_output=True, text=True,
                           timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return r.returncode, r.stdout.strip() or r.stderr.strip()


def string_list(labels):
    model = Gtk.StringList()
    for label in labels:
        model.append(label)
    return model


def name_has_owner(bus, name):
    try:
        res = bus.call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus',
                            'org.freedesktop.DBus', 'NameHasOwner', GLib.Variant('(s)', (name,)),
                            None, Gio.DBusCallFlags.NONE, 1000, None)
        return res.unpack()[0]
    except GLib.Error:
        return False


def shell_property(bus, prop):
    try:
        res = bus.call_sync(ids.SHELL_BUS_NAME, ids.SHELL_OBJECT_PATH,
                            'org.freedesktop.DBus.Properties', 'Get',
                            GLib.Variant('(ss)', (ids.SHELL_BUS_NAME, prop)), None,
                            Gio.DBusCallFlags.NONE, 1000, None)
        return res.unpack()[0]
    except GLib.Error:
        return None


class Daemon:
    """Async client for the daemon's D-Bus API (JSON payloads)."""

    def __init__(self, bus):
        self.bus = bus

    @property
    def running(self):
        return name_has_owner(self.bus, ids.DAEMON_BUS_NAME)

    def call(self, method, args=None, callback=None, timeout=5000):
        def done(bus, res):
            try:
                out = bus.call_finish(res).unpack()
                payload = json.loads(out[0]) if out else None
                err = None
            except (GLib.Error, ValueError) as e:
                payload, err = None, getattr(e, 'message', str(e))
            if callback:
                callback(payload, err)
        self.bus.call(ids.DAEMON_BUS_NAME, ids.DAEMON_OBJECT_PATH, ids.DAEMON_BUS_NAME, method,
                      args, None, Gio.DBusCallFlags.NONE, timeout, None, done)


def message_dialog(parent, heading, body):
    if hasattr(Adw, 'AlertDialog'):
        d = Adw.AlertDialog(heading=heading, body=body)
        d.present_ = lambda: d.present(parent)
    else:
        d = Adw.MessageDialog(transient_for=parent, heading=heading, body=body)
        d.present_ = d.present
    return d


class BindingEditor:
    """Action combo + key / command entries for one binding; saves on change."""

    def __init__(self, window, dkey, dname, bkey, title, subtitle=''):
        self.window = window
        self.dkey, self.dname, self.bkey = dkey, dname, bkey
        binding = window.cfg['devices'].get(dkey, {}).get('bindings', {}).get(bkey,
                                                                               {'action': 'none'})
        self.binding = dict(binding)
        self.combo = Adw.ComboRow(title=title, subtitle=subtitle,
                                  model=string_list(config.ACTIONS.values()))
        self.combo.set_selected(ACTION_IDS.index(self.binding.get('action', 'none')))
        self.keys = Adw.EntryRow(title='Key combination, e.g. ctrl+w, super+Left, alt+F4',
                                 text=self.binding.get('keys', ''), show_apply_button=True)
        self.cmd = Adw.EntryRow(title='Shell command', text=self.binding.get('command', ''),
                                show_apply_button=True)
        self.combo.connect('notify::selected', self._on_action)
        self.keys.connect('apply', self._on_keys)
        self.cmd.connect('apply', self._on_cmd)
        self._sync()

    def rows(self):
        return [self.combo, self.keys, self.cmd]

    def _sync(self):
        action = ACTION_IDS[self.combo.get_selected()]
        self.keys.set_visible(action == 'keys')
        self.cmd.set_visible(action == 'command')

    def _store(self):
        config.set_binding(self.window.cfg, self.dkey, self.dname, self.bkey,
                           dict(self.binding))
        self.window.save()

    def _on_action(self, *_):
        self.binding['action'] = ACTION_IDS[self.combo.get_selected()]
        self._sync()
        action = self.binding['action']
        if action in ('keys', 'command') and not self.binding.get(action):
            return
        self._store()

    def _on_keys(self, row):
        text = row.get_text().strip()
        try:
            actions.parse_keys(text)
            row.remove_css_class('error')
        except ValueError:
            row.add_css_class('error')
            return
        self.binding['keys'] = text
        self._store()

    def _on_cmd(self, row):
        self.binding['command'] = row.get_text().strip()
        self._store()


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=ids.PRODUCT_NAME, default_width=640,
                         default_height=780)
        self.set_icon_name(ids.APP_ID)
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.daemon = Daemon(self.bus)
        self.first_run = not config.CONFIG_PATH.exists()
        self.cfg = config.load()
        self.pending = set()        # (dkey, bkey) rows added in the UI but not yet bound
        self.expanded = set()
        self.hidpp = None
        self.hidpp_error = None
        self._loading = False

        self.toast = Adw.ToastOverlay()
        self.stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header = Adw.HeaderBar(title_widget=switcher)
        learn = Gtk.Button(icon_name='list-add-symbolic',
                           tooltip_text='Press to assign: map the next button or key you press')
        learn.connect('clicked', lambda *_: self.start_learn())
        header.pack_start(learn)
        about = Gtk.Button(icon_name='help-about-symbolic', tooltip_text='About')
        about.connect('clicked', self._on_about)
        header.pack_end(about)

        self.banner = Adw.Banner(button_label='Set up')
        self.banner.connect('button-clicked', lambda *_: self.stack.set_visible_child_name('setup'))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.banner)
        box.append(self.stack)
        self.stack.set_vexpand(True)
        self.toast.set_child(box)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(self.toast)
        self.set_content(toolbar)

        self.buttons_page = Adw.PreferencesPage()
        self.stack.add_titled_with_icon(self.buttons_page, 'buttons', 'Buttons',
                                        'input-mouse-symbolic')
        self.setup_page = self._build_setup_page()
        self.stack.add_titled_with_icon(self.setup_page, 'setup', 'Setup',
                                        'emblem-system-symbolic')

        self.ensure_daemon()
        self.rebuild_buttons()
        self.refresh_status()
        self.load_hidpp()
        if self.first_run or permissions.check()['needs_setup']:
            self.stack.set_visible_child_name('setup')
        GLib.timeout_add_seconds(3, self.refresh_status)

    # --- persistence ---------------------------------------------------------
    def save(self):
        if self._loading:
            return
        config.save(self.cfg)
        self.first_run = False
        self.toast.add_toast(Adw.Toast(title='Saved', timeout=1))

    def ensure_daemon(self):
        if self.daemon.running:
            return
        code, out = systemctl('start', ids.SYSTEMD_UNIT)
        if code:
            self.toast.add_toast(Adw.Toast(title=f'Could not start the mapper: {out}'))

    # --- Buttons page -------------------------------------------------------------
    def rebuild_buttons(self):
        page = self.buttons_page
        for group in getattr(self, '_groups', []):
            page.remove(group)
        self._groups = []

        def add_group(group):
            page.add(group)
            self._groups.append(group)

        if self.first_run:
            welcome = Adw.PreferencesGroup(
                title=f'Welcome to {ids.PRODUCT_NAME}',
                description='Map mouse buttons, extra keys and Logitech firmware buttons to '
                            'App Exposé, the overview and more. Use + (top left) and press a '
                            'button to assign it, or start with the suggestion below.')
            row = Adw.ActionRow(title='Suggested mapping',
                                subtitle='Back thumb button → App Exposé, '
                                         'forward thumb button → Activities overview')
            use = Gtk.Button(label='Use', valign=Gtk.Align.CENTER)
            use.add_css_class('suggested-action')
            use.connect('clicked', self._on_use_suggested)
            row.add_suffix(use)
            welcome.add(row)
            add_group(welcome)

        any_group = Adw.PreferencesGroup(
            title='Any device',
            description='Applies to every mouse/keyboard unless a device below has its own '
                        'mapping for the same button.')
        self._fill_bindings(any_group, config.ANY, 'Any device', set())
        add_group(any_group)

        devs = devices.list_devices()
        dev_group = Adw.PreferencesGroup(
            title='Devices',
            description='Mappings that apply to one device only. Buttons listed are what the '
                        'device advertises; use + and press the button if unsure which is which.')
        if not devs:
            dev_group.add(Adw.ActionRow(title='No input devices found',
                                        subtitle='/sys/class/input has no mice or keyboards'))
        for dev in devs:
            dev_group.add(self._device_row(dev))
        add_group(dev_group)

        add_group(self._hidpp_group())

    def _fill_bindings(self, container, dkey, dname, advertised):
        bindings = self.cfg['devices'].get(dkey, {}).get('bindings', {})
        keys = [k for k in bindings if codes.hidpp_cid(k) is None]
        keys += [b for d, b in sorted(self.pending) if d == dkey and b not in keys]
        add = container.add_row if isinstance(container, Adw.ExpanderRow) else container.add
        for bkey in keys:
            ed = BindingEditor(self, dkey, dname, bkey, codes.label(bkey), bkey)
            for row in ed.rows():
                add(row)
        choices = sorted({codes.name(c) for c in advertised} - set(keys),
                         key=lambda k: codes.code(k) or 0)
        if dkey == config.ANY:
            choices = [k for k in ('BTN_SIDE', 'BTN_EXTRA', 'BTN_MIDDLE', 'BTN_FORWARD',
                                   'BTN_BACK', 'BTN_TASK') if k not in keys]
        if choices:
            picker = Adw.ComboRow(title='Add a button',
                                  model=string_list(['Choose…'] + [
                                      f'{codes.label(k)} ({k})' for k in choices]))

            def on_pick(row, *_):
                i = row.get_selected()
                if i > 0:
                    self.pending.add((dkey, choices[i - 1]))
                    self.expanded.add(dkey)
                    GLib.idle_add(lambda: self.rebuild_buttons() and False)
            picker.connect('notify::selected', on_pick)
            add(picker)

    def _device_row(self, dev):
        bound = self.cfg['devices'].get(dev.key, {}).get('bindings', {})
        n = len([k for k in bound if codes.hidpp_cid(k) is None])
        extras = dev.mouse_buttons if dev.kind in ('mouse', 'trackball') else dev.extra_keys
        details = [dev.kind, dev.bus_name, f'{dev.vendor:04x}:{dev.product:04x}']
        if n:
            details.append(f'{n} mapped')
        row = Adw.ExpanderRow(title=GLib.markup_escape_text(dev.name), subtitle=' · '.join(details))
        row.add_prefix(Gtk.Image(icon_name=KIND_ICONS.get(dev.kind, 'input-gaming-symbolic')))
        row.set_expanded(dev.key in self.expanded)
        row.connect('notify::expanded', lambda r, *_: (self.expanded.add if r.get_expanded()
                                                        else self.expanded.discard)(dev.key))
        if extras:
            shown = ', '.join(codes.name(c) for c in extras[:24])
            more = f' and {len(extras) - 24} more' if len(extras) > 24 else ''
            info = Adw.ActionRow(title='Advertised buttons' if dev.kind in ('mouse', 'trackball')
                                 else 'Extra keys', subtitle=shown + more)
        else:
            info = Adw.ActionRow(title='Advertised buttons', subtitle='only standard keys/buttons')
        info.set_subtitle_selectable(True)
        row.add_row(info)
        self._fill_bindings(row, dev.key, dev.name, extras)
        learn = Adw.ActionRow(title='Press to assign',
                              subtitle='Map whatever this device sends next (button or key)')
        b = Gtk.Button(label='Press…', valign=Gtk.Align.CENTER)
        b.connect('clicked', lambda *_: self.start_learn())
        learn.add_suffix(b)
        row.add_row(learn)
        return row

    def _hidpp_group(self):
        group = Adw.PreferencesGroup(
            title='Logitech firmware controls (HID++)',
            description='Buttons handled inside the device (DPI switch, gesture button, …) send '
                        'nothing to the computer. Assigning an action diverts the control to '
                        f'{ids.PRODUCT_NAME}; "Default" gives it back to the firmware.')
        refresh = Gtk.Button(icon_name='view-refresh-symbolic', valign=Gtk.Align.CENTER,
                             tooltip_text='Scan again')
        refresh.add_css_class('flat')
        refresh.connect('clicked', lambda *_: self.load_hidpp())
        group.set_header_suffix(refresh)
        if self.hidpp is None:
            group.add(Adw.ActionRow(title='Scanning…' if self.daemon.running else
                                    'Mapper not running', subtitle=self.hidpp_error or ''))
            return group
        for err in self.hidpp.get('errors', []):
            row = Adw.ActionRow(title=err.get('name') or err.get('path', 'Logitech device'),
                                subtitle=err.get('error', ''))
            if err.get('permission'):
                row.set_subtitle('No access to the device. Run the one-time setup.')
                b = Gtk.Button(label='Set up', valign=Gtk.Align.CENTER)
                b.connect('clicked', lambda *_: self.run_setup(evdev=False, hidraw=True))
                row.add_suffix(b)
            group.add(row)
        devs = self.hidpp.get('devices', [])
        if not devs and not self.hidpp.get('errors'):
            group.add(Adw.ActionRow(title='No Logitech HID++ devices found'))
        for d in devs:
            exp = Adw.ExpanderRow(title=GLib.markup_escape_text(d['name']),
                                  subtitle=f'{d["path"]} · device index {d["index"]:#x} · '
                                           f'{len(d["controls"])} controls')
            exp.add_prefix(Gtk.Image(icon_name='input-mouse-symbolic'))
            exp.set_expanded(d['key'] in self.expanded)
            exp.connect('notify::expanded', lambda r, *_, k=d['key']: (
                self.expanded.add if r.get_expanded() else self.expanded.discard)(k))
            for c in d['controls']:
                key = codes.hidpp_key(c['cid'])
                flags = []
                if not c['divertable']:
                    flags.append('fixed')
                if c['os_visible']:
                    flags.append('also a normal button')
                subtitle = f'{c["cid"]:#06x}' + (f' ({", ".join(flags)})' if flags else '')
                if not c['divertable']:
                    exp.add_row(Adw.ActionRow(title=c['name'], subtitle=subtitle))
                    continue
                ed = BindingEditor(self, d['key'], d['name'], key, c['name'], subtitle)
                for r in ed.rows():
                    exp.add_row(r)
            group.add(exp)
        return group

    def load_hidpp(self):
        if not self.daemon.running:
            self.hidpp, self.hidpp_error = None, 'start the mapper on the Setup page'
            return

        def done(payload, err):
            self.hidpp = payload if payload else {'devices': [], 'errors': [{'error': err}]}
            self.rebuild_buttons()
        self.daemon.call('ListHidppDevices', None, done, timeout=30000)

    def _on_use_suggested(self, *_):
        for bkey, b in config.SUGGESTED.items():
            config.set_binding(self.cfg, config.ANY, 'Any device', bkey, dict(b))
        self.save()
        self.rebuild_buttons()

    # --- learn mode -------------------------------------------------------------
    def start_learn(self):
        if not self.daemon.running:
            self.toast.add_toast(Adw.Toast(title='The mapper is not running (see Setup)'))
            return
        dialog = message_dialog(self, 'Press a button',
                                'Press the mouse button or key you want to assign.\n'
                                'Logitech firmware buttons are detected too when access '
                                'is set up. Esc cancels.')
        dialog.add_response('cancel', 'Cancel')
        dialog.connect('response', lambda *_: self.daemon.call('CancelLearn'))

        def done(result, err):
            dialog.close() if hasattr(dialog, 'close') else dialog.destroy()
            if err:
                self.toast.add_toast(Adw.Toast(title=f'Learn failed: {err}'))
            elif result.get('timeout'):
                self.toast.add_toast(Adw.Toast(
                    title='Nothing detected. On Wayland the one-time setup is needed; '
                          'firmware buttons need Logitech access.', timeout=6))
            elif result.get('binding_key'):
                self.assign_dialog(result)
        self.daemon.call('Learn', GLib.Variant('(u)', (LEARN_TIMEOUT_MS,)), done,
                         timeout=LEARN_TIMEOUT_MS + 5000)
        dialog.present_()

    def assign_dialog(self, result):
        bkey, dkey, dname = result['binding_key'], result['device_key'], result['device_name']
        hid = codes.hidpp_cid(bkey) is not None
        dialog = message_dialog(self, f'Assign {result["label"]}',
                                f'Detected {bkey} from “{dname}”.')
        box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        box.add_css_class('boxed-list')
        scopes = [(dkey, dname)] + ([] if hid else [(config.ANY, 'Any device')])
        scope = Adw.ComboRow(title='Applies to',
                             model=string_list([f'This device ({n})' if k != config.ANY else n
                                                for k, n in scopes]))
        act_ids = [a for a in ACTION_IDS if a != 'none']
        action = Adw.ComboRow(title='Action',
                              model=string_list([config.ACTIONS[a] for a in act_ids]))
        keys = Adw.EntryRow(title='Key combination (for "Keyboard shortcut")')
        cmd = Adw.EntryRow(title='Shell command (for "Run command")')
        for r in (scope, action, keys, cmd):
            box.append(r)
        dialog.set_extra_child(box)
        dialog.add_response('cancel', 'Cancel')
        dialog.add_response('assign', 'Assign')
        dialog.set_response_appearance('assign', Adw.ResponseAppearance.SUGGESTED)

        def on_response(_d, response):
            if response != 'assign':
                return
            k, n = scopes[scope.get_selected()]
            binding = {'action': act_ids[action.get_selected()]}
            if binding['action'] == 'keys':
                binding['keys'] = keys.get_text().strip()
            if binding['action'] == 'command':
                binding['command'] = cmd.get_text().strip()
            config.set_binding(self.cfg, k, n, bkey, binding)
            self.expanded.add(k)
            self.save()
            self.rebuild_buttons()
        dialog.connect('response', on_response)
        dialog.present_()

    # --- Setup page ---------------------------------------------------------------
    def _build_setup_page(self):
        page = Adw.PreferencesPage()
        intro = Adw.PreferencesGroup(
            title='Setup',
            description='App Exposé itself is a GNOME Shell extension. Mouse buttons are handled '
                        'by a small background service. On X11 it needs no extra permissions; '
                        'on Wayland it needs a one-time setup (administrator password).')
        page.add(intro)

        self.session_row = Adw.ActionRow(title='Session')
        intro.add(self.session_row)
        self.ext_row = Adw.ActionRow(title='Shell extension')
        ext_prefs = Gtk.Button(label='Settings', valign=Gtk.Align.CENTER)
        ext_prefs.connect('clicked', lambda *_: subprocess.Popen(
            ['gnome-extensions', 'prefs', ids.EXTENSION_UUID]))
        self.ext_row.add_suffix(ext_prefs)
        self.ext_enable = Gtk.Button(label='Enable', valign=Gtk.Align.CENTER)
        self.ext_enable.connect('clicked', self._on_enable_extension)
        self.ext_row.add_suffix(self.ext_enable)
        intro.add(self.ext_row)

        perms = Adw.PreferencesGroup(title='Permissions (one-time)')
        self.input_row = Adw.ActionRow(title='Input devices',
                                       subtitle='Needed on Wayland and for the evdev backend')
        self.input_btn = Gtk.Button(label='Set up', valign=Gtk.Align.CENTER)
        self.input_btn.add_css_class('suggested-action')
        self.input_btn.connect('clicked', lambda *_: self.run_setup(evdev=True, hidraw=True))
        self.input_row.add_suffix(self.input_btn)
        perms.add(self.input_row)
        self.hid_row = Adw.ActionRow(title='Logitech devices (HID++)',
                                     subtitle='Needed for firmware buttons such as a DPI switch')
        self.hid_btn = Gtk.Button(label='Set up', valign=Gtk.Align.CENTER)
        self.hid_btn.connect('clicked', lambda *_: self.run_setup(evdev=False, hidraw=True))
        self.hid_row.add_suffix(self.hid_btn)
        perms.add(self.hid_row)
        explain = Adw.ActionRow(
            title='What the setup does',
            subtitle='Installs udev rules that let the user of the active local session read '
                     'input devices and create virtual ones (/dev/uinput), and talk to Logitech '
                     'devices. Effective immediately, no logout. Same access as the "input" '
                     'group that tools like input-remapper use.')
        explain.set_subtitle_lines(0)
        remove = Gtk.Button(label='Remove', valign=Gtk.Align.CENTER)
        remove.connect('clicked', lambda *_: self.run_setup(remove=True))
        explain.add_suffix(remove)
        perms.add(explain)
        page.add(perms)

        svc = Adw.PreferencesGroup(title='Button mapper service')
        self.status_row = Adw.ActionRow(title='Status')
        self.status_row.set_subtitle_lines(0)
        restart = Gtk.Button(label='Restart', valign=Gtk.Align.CENTER)
        restart.connect('clicked', self._on_restart)
        self.status_row.add_suffix(restart)
        svc.add(self.status_row)
        self.autostart = Adw.SwitchRow(title='Run at login',
                                       subtitle=f'systemd user service {ids.SYSTEMD_UNIT}')
        self.autostart.connect('notify::active', self._on_autostart)
        svc.add(self.autostart)
        self.backend_row = Adw.ComboRow(title='Backend',
                                        model=string_list(config.BACKENDS.values()))
        self.backend_row.set_selected(BACKEND_IDS.index(self.cfg.get('backend', 'auto')))
        self.backend_row.connect('notify::selected', self._on_backend)
        svc.add(self.backend_row)
        test = Adw.ActionRow(title='Test', subtitle='Runs after 1.5 s so you can focus '
                             'another app first')
        for label, method in (('App Exposé', 'ToggleAppExpose'), ('Overview', 'ToggleOverview')):
            b = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
            b.connect('clicked', self._on_test, method)
            test.add_suffix(b)
        svc.add(test)
        page.add(svc)
        return page

    def run_setup(self, evdev=True, hidraw=True, remove=False):
        try:
            argv = permissions.setup_command(evdev=evdev, hidraw=hidraw, remove=remove)
        except FileNotFoundError as e:
            self.toast.add_toast(Adw.Toast(title=str(e)))
            return
        if not permissions.check()['pkexec']:
            self.toast.add_toast(Adw.Toast(title='pkexec not found; run in a terminal: sudo ' +
                                           ' '.join(argv[1:]), timeout=10))
            return
        try:
            proc = Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDOUT_PIPE |
                                      Gio.SubprocessFlags.STDERR_MERGE)
        except GLib.Error as e:
            self.toast.add_toast(Adw.Toast(title=e.message))
            return

        def done(p, res):
            try:
                _, out, _ = p.communicate_utf8_finish(res)
            except GLib.Error as e:
                out = e.message
            if p.get_exit_status() == 0:
                self.toast.add_toast(Adw.Toast(title='Permissions updated'))
                systemctl('restart', ids.SYSTEMD_UNIT)
            elif p.get_exit_status() in (126, 127):
                self.toast.add_toast(Adw.Toast(title='Cancelled'))
            else:
                self.toast.add_toast(Adw.Toast(title=f'Setup failed: {(out or "").strip()[-200:]}',
                                               timeout=10))
            GLib.timeout_add(1500, lambda: (self.refresh_status(), self.load_hidpp()) and False)
        proc.communicate_utf8_async(None, None, done)

    def refresh_status(self):
        perm = permissions.check()
        session = perm['session_type']
        self.session_row.set_subtitle(
            {'x11': 'X11: mouse buttons work without extra permissions',
             'wayland': 'Wayland: the mapper needs the one-time setup below'}.get(
                session, f'{session}'))

        version = shell_property(self.bus, 'Version')
        state = self._extension_state()
        if version:
            self.ext_row.set_subtitle(f'running (version {version})')
        elif state == 'enabled':
            self.ext_row.set_subtitle('enabled but not loaded yet: log out and back in '
                                      '(on X11 you can also press Alt+F2, type r, Enter)')
        elif state == 'missing':
            self.ext_row.set_subtitle('not installed: run install.sh or get it from '
                                      'extensions.gnome.org')
        else:
            self.ext_row.set_subtitle('installed but disabled')
        self.ext_enable.set_visible(state == 'disabled')

        missing = perm['evdev_missing']
        if not missing:
            self.input_row.set_subtitle('OK')
        elif not perm['evdev_module']:
            self.input_row.set_subtitle('python3-evdev is not installed (needed on Wayland)')
        else:
            needed = 'needed for this Wayland session' if perm['evdev_needed'] else \
                'optional on X11 (only for the evdev backend)'
            self.input_row.set_subtitle(f'Missing {missing}: {needed}')
        self.input_btn.set_visible(bool(missing))
        self.input_btn.set_css_classes(['suggested-action'] if perm['evdev_needed'] else [])

        hid = perm['hidraw']
        if not hid['nodes']:
            self.hid_row.set_subtitle('No Logitech device connected')
        elif hid['accessible'] == hid['nodes']:
            self.hid_row.set_subtitle('OK: ' + ', '.join(hid['names']))
        else:
            self.hid_row.set_subtitle('No access to ' + ', '.join(hid['names']))
        self.hid_btn.set_visible(hid['accessible'] < hid['nodes'])

        if perm['evdev_needed'] and missing:
            self.banner.set_title('Mouse buttons need a one-time setup on Wayland')
            self.banner.set_revealed(True)
        elif not version:
            self.banner.set_title(f'The {ids.PRODUCT_NAME} shell extension is not running')
            self.banner.set_revealed(True)
        else:
            self.banner.set_revealed(False)

        _, active = systemctl('is-active', ids.SYSTEMD_UNIT)
        _, enabled = systemctl('is-enabled', ids.SYSTEMD_UNIT)
        self._loading = True
        self.autostart.set_active(enabled == 'enabled')
        self._loading = False
        if self.daemon.running:
            def done(st, err):
                if not st:
                    self.status_row.set_subtitle(f'{active}: {err}')
                    return
                text = f'{active}, {st["backend"]} backend'
                if st.get('grabbed'):
                    text += ' · handling ' + ', '.join(st['grabbed'])
                if st.get('problem'):
                    text += f'\n⚠ {st["problem"]}'
                self.status_row.set_subtitle(text)
            self.daemon.call('GetStatus', None, done)
        else:
            self.status_row.set_subtitle(f'{active} ({enabled} at login)')
        return True

    def _extension_state(self):
        try:
            res = self.bus.call_sync('org.gnome.Shell', '/org/gnome/Shell',
                                     'org.gnome.Shell.Extensions', 'GetExtensionInfo',
                                     GLib.Variant('(s)', (ids.EXTENSION_UUID,)), None,
                                     Gio.DBusCallFlags.NONE, 1000, None)
            info = res.unpack()[0]
            if not info:
                return 'missing'
            return 'enabled' if info.get('enabled') else 'disabled'
        except GLib.Error:
            ext_dir = os.path.expanduser(f'~/.local/share/gnome-shell/extensions/'
                                         f'{ids.EXTENSION_UUID}')
            return 'enabled' if os.path.isdir(ext_dir) else 'missing'

    def _on_enable_extension(self, *_):
        try:
            self.bus.call_sync('org.gnome.Shell', '/org/gnome/Shell',
                               'org.gnome.Shell.Extensions', 'EnableExtension',
                               GLib.Variant('(s)', (ids.EXTENSION_UUID,)), None,
                               Gio.DBusCallFlags.NONE, 3000, None)
        except GLib.Error as e:
            self.toast.add_toast(Adw.Toast(title=e.message))
        self.refresh_status()

    def _on_backend(self, *_):
        self.cfg['backend'] = BACKEND_IDS[self.backend_row.get_selected()]
        self.save()

    def _on_restart(self, *_):
        code, out = systemctl('restart', ids.SYSTEMD_UNIT)
        if code:
            self.toast.add_toast(Adw.Toast(title=f'Restart failed: {out}'))
        GLib.timeout_add(1000, lambda: (self.refresh_status(), self.load_hidpp()) and False)

    def _on_autostart(self, row, *_):
        if self._loading:
            return
        args = ('enable', '--now') if row.get_active() else ('disable', '--now')
        code, out = systemctl(*args, ids.SYSTEMD_UNIT)
        if code:
            self.toast.add_toast(Adw.Toast(title=out))
        self.refresh_status()

    def _on_test(self, _button, method):
        def fire():
            try:
                actions.call_shell(method, self.bus)
            except GLib.Error as e:
                self.toast.add_toast(Adw.Toast(title=e.message))
            return False
        GLib.timeout_add(1500, fire)

    def _on_about(self, *_):
        kwargs = dict(application_name=ids.PRODUCT_NAME, application_icon=ids.APP_ID,
                      version=ids.VERSION, website=ids.HOMEPAGE,
                      issue_url=f'{ids.HOMEPAGE}/issues', license_type=Gtk.License.GPL_2_0,
                      developer_name=f'{ids.PRODUCT_NAME} contributors')
        if hasattr(Adw, 'AboutDialog'):
            Adw.AboutDialog(**kwargs).present(self)
        else:
            Adw.AboutWindow(transient_for=self, **kwargs).present()


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=ids.APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        win = self.props.active_window or MainWindow(self)
        win.present()


def main():
    local_bin = os.path.expanduser('~/.local/bin')
    path = os.environ.get('PATH', '')
    if local_bin not in path.split(':'):
        os.environ['PATH'] = f'{local_bin}:{path}'
    return App().run(sys.argv)


if __name__ == '__main__':
    sys.exit(main())
