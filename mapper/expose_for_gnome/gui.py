"""GTK4/libadwaita app: a calm home screen to assign buttons, with device
details one step away. Setup stays available from the menu."""
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
              'keyboard': 'input-keyboard-symbolic'}
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


def action_label(binding):
    action = (binding or {}).get('action', 'none')
    if action == 'keys':
        return binding.get('keys') or config.ACTIONS['keys']
    if action == 'command':
        return binding.get('command') or config.ACTIONS['command']
    return config.ACTIONS.get(action, action)


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
        super().__init__(application=app, title=ids.PRODUCT_NAME, default_width=520,
                         default_height=680)
        self.set_icon_name(ids.APP_ID)
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.daemon = Daemon(self.bus)
        self.first_run = not config.CONFIG_PATH.exists()
        self.cfg = config.load()
        self.hidpp = None
        self.hidpp_error = None
        self._loading = False
        self._device_page = None

        self.toast = Adw.ToastOverlay()
        self.nav = Adw.NavigationView()
        self.banner = Adw.Banner(button_label='Set up')
        self.banner.connect('button-clicked', lambda *_: self.open_setup())

        shell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        shell.append(self.banner)
        shell.append(self.nav)
        self.nav.set_vexpand(True)
        self.toast.set_child(shell)

        toolbar = Adw.ToolbarView()
        toolbar.set_content(self.toast)
        self.set_content(toolbar)

        self.home_page = Adw.NavigationPage(title=ids.PRODUCT_NAME, tag='home')
        self.home_page.set_child(self._build_home())
        self.nav.add(self.home_page)

        self.setup_page = Adw.NavigationPage(title='Setup', tag='setup')
        self.setup_page.set_child(self._build_setup_page())

        self.ensure_daemon()
        self.refresh_home()
        self.refresh_status()
        self.load_hidpp()
        if permissions.check()['needs_setup']:
            self.banner.set_title('Mouse buttons need a one-time setup on Wayland')
            self.banner.set_revealed(True)
        GLib.timeout_add_seconds(3, self.refresh_status)

    # --- persistence ---------------------------------------------------------
    def save(self):
        if self._loading:
            return
        config.save(self.cfg)
        self.first_run = False
        self.toast.add_toast(Adw.Toast(title='Saved', timeout=1))
        self.refresh_home()
        if self._device_page and self.nav.find_page('device'):
            # Rebuild detail so editors stay in sync after a save from home/learn.
            pass

    def ensure_daemon(self):
        if self.daemon.running:
            return
        code, out = systemctl('start', ids.SYSTEMD_UNIT)
        if code:
            self.toast.add_toast(Adw.Toast(title=f'Could not start the mapper: {out}'))

    # --- home ----------------------------------------------------------------
    def _build_home(self):
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        menu_btn = Gtk.MenuButton(icon_name='open-menu-symbolic', tooltip_text='Menu')
        menu = Gio.Menu()
        menu.append('Setup', 'win.setup')
        menu.append('About', 'win.about')
        menu_btn.set_menu_model(menu)
        header.pack_end(menu_btn)
        toolbar.add_top_bar(header)

        self.home_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.home_box.set_margin_top(18)
        self.home_box.set_margin_bottom(24)
        self.home_box.set_margin_start(18)
        self.home_box.set_margin_end(18)
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(Adw.Clamp(child=self.home_box, maximum_size=520))
        toolbar.set_content(scroll)

        app = self.get_application()
        if app:
            app.create_action = getattr(app, 'create_action', None)
        self._install_actions()
        return toolbar

    def _install_actions(self):
        setup = Gio.SimpleAction.new('setup', None)
        setup.connect('activate', lambda *_: self.open_setup())
        self.add_action(setup)
        about = Gio.SimpleAction.new('about', None)
        about.connect('activate', self._on_about)
        self.add_action(about)

    def refresh_home(self):
        while child := self.home_box.get_first_child():
            self.home_box.remove(child)

        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                       halign=Gtk.Align.CENTER)
        hero.set_margin_top(12)
        hero.set_margin_bottom(6)
        title = Gtk.Label(label='Press a button to assign', wrap=True, justify=Gtk.Justification.CENTER)
        title.add_css_class('title-1')
        subtitle = Gtk.Label(
            label='Click the button below, then press a mouse button or key.',
            wrap=True, justify=Gtk.Justification.CENTER)
        subtitle.add_css_class('dim-label')
        assign = Gtk.Button(label='Press a button…', halign=Gtk.Align.CENTER)
        assign.add_css_class('pill')
        assign.add_css_class('suggested-action')
        assign.set_size_request(220, 0)
        assign.connect('clicked', lambda *_: self.start_learn())
        refresh = Gtk.Button(halign=Gtk.Align.CENTER)
        refresh.set_child(Adw.ButtonContent(icon_name='view-refresh-symbolic',
                                            label='Refresh devices'))
        refresh.add_css_class('pill')
        refresh.set_size_request(220, 0)
        refresh.connect('clicked', self.refresh_devices)
        hero.append(title)
        hero.append(subtitle)
        hero.append(assign)
        hero.append(refresh)
        self.home_box.append(hero)

        mapped = self._mapped_entries()
        mapped_group = Adw.PreferencesGroup(title='Mapped buttons' if mapped else '')
        if mapped:
            for entry in mapped:
                row = Adw.ActionRow(title=entry['title'], subtitle=entry['subtitle'])
                row.set_activatable(True)
                row.add_suffix(Gtk.Image(icon_name='go-next-symbolic'))
                row.connect('activated', self._on_open_mapped, entry)
                mapped_group.add(row)
        else:
            empty = Adw.ActionRow(
                title='Nothing mapped yet',
                subtitle='Your Back, Forward and DPI buttons will show up here.')
            mapped_group.add(empty)
        self.home_box.append(mapped_group)

        phys = devices.list_physical_devices()
        devices_group = Adw.PreferencesGroup(
            title='Your devices',
            description='Mice and keyboards only. Open one to change its buttons.')
        if not phys:
            devices_group.add(Adw.ActionRow(title='No mice or keyboards found'))
        for dev in phys:
            n = self._mapped_count(dev)
            row = Adw.ActionRow(title=dev.display,
                                subtitle=f'{n} mapped' if n else dev.bus_name or dev.kind)
            row.add_prefix(Gtk.Image(icon_name=KIND_ICONS.get(dev.kind, 'input-mouse-symbolic')))
            row.add_suffix(Gtk.Image(icon_name='go-next-symbolic'))
            row.set_activatable(True)
            row.connect('activated', lambda _r, d=dev: self.open_device(d))
            devices_group.add(row)
        self.home_box.append(devices_group)

    def _mapped_entries(self):
        """Short list of active bindings for the home screen."""
        phys = {p.key: p for p in devices.list_physical_devices()}
        entries = []
        for dkey, dev in self.cfg.get('devices', {}).items():
            if dkey == config.ANY:
                dname = 'Any device'
                display = dname
            else:
                dname = dev.get('name') or dkey
                display = devices.display_name(dname)
                # Prefer the live physical card's display name when known.
                for p in phys.values():
                    if p.key == dkey or dkey in config.Mapping(self.cfg).matching_keys(p.key):
                        display = p.display
                        break
                else:
                    if dkey.startswith('046d:hidpp:'):
                        display = devices.display_name(dname)
            for bkey, binding in (dev.get('bindings') or {}).items():
                if binding.get('action', 'none') == 'none':
                    continue
                entries.append({
                    'dkey': dkey, 'dname': dname, 'bkey': bkey,
                    'title': f'{codes.label(bkey)}  ·  {action_label(binding)}',
                    'subtitle': display,
                    'hidpp': codes.hidpp_cid(bkey) is not None,
                })
        order = {'BTN_SIDE': 0, 'BTN_EXTRA': 1, 'HIDPP:0x00fd': 2}
        entries.sort(key=lambda e: (order.get(e['bkey'], 50), e['title']))
        return entries

    def _mapped_count(self, phys):
        mapping = config.Mapping(self.cfg)
        keys = set()
        for k in mapping.matching_keys(phys.key):
            keys |= {b for b, bind in mapping.per_device.get(k, {}).items()
                     if codes.hidpp_cid(b) is None}
        # Count HID++ bindings that belong to this mouse.
        for dkey, bindings in mapping.per_device.items():
            if not dkey.startswith('046d:hidpp:'):
                continue
            hid_name = dkey.split(':', 2)[-1]
            if phys.matches_hidpp(hid_name):
                keys |= set(bindings)
        return len(keys)

    def _on_open_mapped(self, _row, entry):
        if entry['hidpp']:
            for phys in devices.list_physical_devices():
                if phys.matches_hidpp(entry['dname']) or phys.matches_hidpp(
                        entry['dkey'].split(':', 2)[-1]):
                    self.open_device(phys)
                    return
        for phys in devices.list_physical_devices():
            if phys.key == entry['dkey'] or entry['dkey'] in config.Mapping(self.cfg).matching_keys(
                    phys.key):
                self.open_device(phys)
                return
        self.toast.add_toast(Adw.Toast(title='That device is not connected'))

    # --- device detail -------------------------------------------------------
    def open_device(self, phys):
        page = Adw.NavigationPage(title=phys.display, tag='device')
        page.set_child(self._build_device_page(phys))
        # Replace any previous device page so Back always returns home.
        if self.nav.find_page('device'):
            self.nav.pop_to_tag('home')
        self.nav.push(page)
        self._device_page = page

    def _build_device_page(self, phys):
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.set_margin_top(12)
        box.set_margin_bottom(24)
        box.set_margin_start(18)
        box.set_margin_end(18)

        intro = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        kind = {'mouse': 'Mouse', 'trackball': 'Trackball', 'keyboard': 'Keyboard'}.get(
            phys.kind, phys.kind.title())
        heading = Gtk.Label(label=phys.display, xalign=0)
        heading.add_css_class('title-2')
        blurb = Gtk.Label(label=f'{kind} · {phys.bus_name}' if phys.bus_name else kind, xalign=0)
        blurb.add_css_class('dim-label')
        intro.append(heading)
        intro.append(blurb)
        box.append(intro)

        assign = Gtk.Button(label='Press a button on this device…')
        assign.add_css_class('pill')
        assign.connect('clicked', lambda *_: self.start_learn())
        box.append(assign)

        bindings_group = Adw.PreferencesGroup(title='Buttons')
        mapping = config.Mapping(self.cfg)
        bound_keys = []
        for k in mapping.matching_keys(phys.key):
            for bkey, binding in mapping.per_device.get(k, {}).items():
                if codes.hidpp_cid(bkey) is None:
                    bound_keys.append((k, phys.name if k == phys.key else
                                       self.cfg['devices'].get(k, {}).get('name', phys.name),
                                       bkey))
        # Prefer the live physical key for new edits when we have a match.
        edit_key = phys.key
        edit_name = phys.name
        if bound_keys:
            # Keep saving to the existing config key so we don't fork duplicates.
            edit_key, edit_name, _ = bound_keys[0]
            edit_name = self.cfg['devices'].get(edit_key, {}).get('name', edit_name)
        seen = set()
        for dkey, dname, bkey in bound_keys:
            if bkey in seen:
                continue
            seen.add(bkey)
            ed = BindingEditor(self, dkey, dname, bkey, codes.label(bkey))
            for row in ed.rows():
                bindings_group.add(row)
        extras = phys.mouse_buttons if phys.kind in ('mouse', 'trackball') else phys.extra_keys
        choices = [codes.name(c) for c in extras
                   if codes.name(c) not in seen and codes.name(c) not in
                   ('BTN_LEFT', 'BTN_RIGHT')]
        # Prefer the familiar thumb buttons first.
        preferred = [k for k in ('BTN_SIDE', 'BTN_EXTRA', 'BTN_MIDDLE', 'BTN_FORWARD',
                                 'BTN_BACK', 'BTN_TASK') if k in choices]
        others = [k for k in choices if k not in preferred]
        add_choices = preferred + others
        if add_choices:
            picker = Adw.ComboRow(title='Add a button',
                                  model=string_list(['Choose…'] +
                                                    [codes.label(k) for k in add_choices]))

            def on_pick(row, *_):
                i = row.get_selected()
                if i <= 0:
                    return
                bkey = add_choices[i - 1]
                config.set_binding(self.cfg, edit_key, edit_name, bkey,
                                   {'action': 'app-expose'})
                self.save()
                self.open_device(phys)
            picker.connect('notify::selected', on_pick)
            bindings_group.add(picker)
        if not bound_keys and not add_choices:
            bindings_group.add(Adw.ActionRow(
                title='No extra buttons advertised',
                subtitle='Use “Press a button…” if this device sends a special key.'))
        box.append(bindings_group)

        hid = self._hidpp_for(phys)
        if hid:
            more = Adw.PreferencesGroup(title='More buttons on this mouse…')
            exp = Adw.ExpanderRow(title='Logitech firmware controls',
                                  subtitle='DPI switch and similar buttons handled inside the mouse')
            exp.set_expanded(any(
                codes.hidpp_key(c['cid']) in self.cfg.get('devices', {}).get(hid['key'], {})
                .get('bindings', {}) for c in hid.get('controls', [])))
            has_rows = False
            for c in hid.get('controls', []):
                if not c.get('divertable') or c.get('os_visible'):
                    continue
                has_rows = True
                key = codes.hidpp_key(c['cid'])
                ed = BindingEditor(self, hid['key'], hid['name'], key, c['name'])
                for row in ed.rows():
                    exp.add_row(row)
            if has_rows:
                more.add(exp)
                box.append(more)

        advanced = Adw.PreferencesGroup()
        adv = Adw.ExpanderRow(title='Advanced', subtitle='Technical identifiers')
        adv.add_row(Adw.ActionRow(title='Device id', subtitle=phys.key))
        adv.add_row(Adw.ActionRow(title='Identity', subtitle=phys.identity))
        if phys.uniq:
            adv.add_row(Adw.ActionRow(title='Unique id', subtitle=phys.uniq))
        paths = ', '.join(n.path for n in phys.nodes)
        adv.add_row(Adw.ActionRow(title='Event nodes', subtitle=paths))
        if hid:
            adv.add_row(Adw.ActionRow(title='HID++', subtitle=f'{hid["path"]} · {hid["key"]}'))
        advanced.add(adv)
        box.append(advanced)

        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(Adw.Clamp(child=box, maximum_size=520))
        toolbar.set_content(scroll)
        return toolbar

    def _hidpp_for(self, phys):
        if not self.hidpp or phys.kind not in ('mouse', 'trackball'):
            return None
        for d in self.hidpp.get('devices', []):
            if phys.matches_hidpp(d.get('name', '')):
                return d
        return None

    def load_hidpp(self):
        if not self.daemon.running:
            self.hidpp, self.hidpp_error = None, 'start the mapper on the Setup page'
            return

        def done(payload, err):
            self.hidpp = payload if payload else {'devices': [], 'errors': [{'error': err}]}
            self.refresh_home()
        self.daemon.call('ListHidppDevices', None, done, timeout=30000)

    def refresh_devices(self, *_):
        """Rescan mice/keyboards and Logitech HID++ (same effect as reopening the app)."""
        self.ensure_daemon()
        self.cfg = config.load()
        if self.daemon.running:
            # Re-apply grabs against the current X11/evdev device set, then force
            # a HID++ enumeration so firmware controls reappear after Bluetooth drops.
            self.daemon.call('Reload')
        was_on_device = bool(self.nav.find_page('device'))
        previous = None
        if was_on_device and self._device_page:
            previous = self._device_page.get_title()

        def finish(payload=None, err=None):
            if payload is not None or err is not None:
                self.hidpp = payload if payload else {
                    'devices': [], 'errors': [{'error': err or 'scan failed'}]}
            elif not self.daemon.running:
                self.hidpp, self.hidpp_error = None, 'start the mapper on the Setup page'
            if was_on_device:
                self.nav.pop_to_tag('home')
            self.refresh_home()
            if previous:
                for phys in devices.list_physical_devices():
                    if phys.display == previous:
                        self.open_device(phys)
                        break
            n = len(devices.list_physical_devices())
            hid_n = len((self.hidpp or {}).get('devices', []))
            self.toast.add_toast(Adw.Toast(
                title=f'Refreshed · {n} device{"s" if n != 1 else ""}'
                      + (f', {hid_n} Logitech' if hid_n else ''),
                timeout=2))

        if self.daemon.running:
            # Give Reload a moment to regrab before listing HID++.
            def scan():
                self.daemon.call('ListHidppDevices', None, finish, timeout=30000)
                return False
            GLib.timeout_add(200, scan)
        else:
            finish()

    # --- learn mode -------------------------------------------------------------
    def start_learn(self):
        if not self.daemon.running:
            self.toast.add_toast(Adw.Toast(title='The mapper is not running (see Setup)'))
            return
        dialog = message_dialog(self, 'Press a button',
                                'Press the mouse button or key you want to assign.\n'
                                'Esc cancels.')
        dialog.add_response('cancel', 'Cancel')
        dialog.connect('response', lambda *_: self.daemon.call('CancelLearn'))

        def done(result, err):
            dialog.close() if hasattr(dialog, 'close') else dialog.destroy()
            if err:
                self.toast.add_toast(Adw.Toast(title=f'Learn failed: {err}'))
            elif result.get('timeout'):
                self.toast.add_toast(Adw.Toast(
                    title='Nothing detected. On Wayland the one-time setup is needed.',
                    timeout=6))
            elif result.get('cancelled'):
                return
            elif result.get('binding_key'):
                self.assign_dialog(result)
        self.daemon.call('Learn', GLib.Variant('(u)', (LEARN_TIMEOUT_MS,)), done,
                         timeout=LEARN_TIMEOUT_MS + 5000)
        dialog.present_()

    def assign_dialog(self, result):
        bkey, dkey, dname = result['binding_key'], result['device_key'], result['device_name']
        hid = codes.hidpp_cid(bkey) is not None
        # Prefer an existing config key for this physical device so reconnects
        # do not fork a second copy of the mappings.
        mapping = config.Mapping(self.cfg)
        aliases = mapping.matching_keys(dkey)
        if aliases:
            dkey = aliases[0]
            dname = self.cfg['devices'].get(dkey, {}).get('name', dname)
        dialog = message_dialog(self, f'Assign {result["label"]}',
                                f'Detected on “{devices.display_name(dname)}”.')
        box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        box.add_css_class('boxed-list')
        scopes = [(dkey, dname)] + ([] if hid else [(config.ANY, 'Any device')])
        scope = Adw.ComboRow(title='Applies to',
                             model=string_list([
                                 devices.display_name(n) if k != config.ANY else n
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
            self.save()
            for phys in devices.list_physical_devices():
                if phys.key == k or k in config.Mapping(self.cfg).matching_keys(phys.key) or \
                        (hid and phys.matches_hidpp(n)):
                    self.open_device(phys)
                    break
        dialog.connect('response', on_response)
        dialog.present_()

    # --- Setup page ---------------------------------------------------------------
    def open_setup(self):
        if not self.nav.find_page('setup'):
            self.nav.push(self.setup_page)
        else:
            self.nav.push_by_tag('setup') if hasattr(self.nav, 'push_by_tag') else \
                self.nav.push(self.setup_page)

    def _build_setup_page(self):
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()
        intro = Adw.PreferencesGroup(
            title='Setup',
            description='App Exposé is a GNOME Shell extension. Mouse buttons are handled by a '
                        'small background service. On X11 it needs no extra permissions.')
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
        self.hid_row = Adw.ActionRow(title='Logitech devices',
                                     subtitle='Needed for firmware buttons such as a DPI switch')
        self.hid_btn = Gtk.Button(label='Set up', valign=Gtk.Align.CENTER)
        self.hid_btn.connect('clicked', lambda *_: self.run_setup(evdev=False, hidraw=True))
        self.hid_row.add_suffix(self.hid_btn)
        perms.add(self.hid_row)
        explain = Adw.ActionRow(
            title='What the setup does',
            subtitle='Installs udev rules so the active session can read input devices and talk '
                     'to Logitech firmware. No logout needed.')
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
        self.autostart = Adw.SwitchRow(title='Run at login')
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
        toolbar.set_content(page)
        return toolbar

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
            self.ext_row.set_subtitle('not installed: run install.sh')
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
            names = [devices.display_name(n) for n in hid['names']
                     if 'cam' not in n.lower() and 'stream' not in n.lower()]
            self.hid_row.set_subtitle('OK' + (': ' + ', '.join(names) if names else ''))
        else:
            self.hid_row.set_subtitle('No access to ' + ', '.join(
                devices.display_name(n) for n in hid['names']))
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
                    text += ' · handling ' + ', '.join(
                        devices.display_name(n) for n in st['grabbed'])
                if st.get('problem'):
                    text += f'\n⚠ {st["problem"]}'
                for w in st.get('warnings', []):
                    text += f'\nⓘ {w}'
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

    def _on_about(self, *_args):
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
