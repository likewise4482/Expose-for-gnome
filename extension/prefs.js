import Adw from 'gi://Adw';
import Gio from 'gi://Gio';
import Gtk from 'gi://Gtk';

import {ExtensionPreferences} from 'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js';

import {HOMEPAGE, SHELL_BUS_NAME, SHELL_OBJECT_PATH} from './ids.js';

function shortcutRow(settings, key, title) {
    const row = new Adw.EntryRow({
        title,
        show_apply_button: true,
        text: settings.get_strv(key)[0] ?? '',
    });
    row.connect('apply', () => {
        const text = row.text.trim();
        if (text === '') {
            settings.set_strv(key, []);
            row.remove_css_class('error');
            return;
        }
        const [ok] = Gtk.accelerator_parse(text);
        if (ok) {
            settings.set_strv(key, [text]);
            row.remove_css_class('error');
        } else {
            row.add_css_class('error');
        }
    });
    return row;
}

function switchRow(settings, key, title, subtitle) {
    const row = new Adw.SwitchRow({title, subtitle});
    settings.bind(key, row, 'active', Gio.SettingsBindFlags.DEFAULT);
    return row;
}

export default class ExposeForGnomePreferences extends ExtensionPreferences {
    fillPreferencesWindow(window) {
        const settings = this.getSettings();
        const page = new Adw.PreferencesPage();

        const behaviour = new Adw.PreferencesGroup({title: 'App Exposé'});
        behaviour.add(switchRow(settings, 'all-workspaces', 'Windows from all workspaces',
            'Off: only the current workspace'));
        behaviour.add(switchRow(settings, 'include-minimized', 'Show minimized windows',
            'Shown as cards along the bottom edge'));
        behaviour.add(switchRow(settings, 'dismiss-with-extra-buttons',
            'Side mouse buttons close it',
            'Pressing mouse button 8/9 while App Exposé or the overview is open closes it'));
        const anim = new Adw.SpinRow({
            title: 'Animation time (ms)',
            adjustment: new Gtk.Adjustment({lower: 0, upper: 1000, step_increment: 20}),
        });
        settings.bind('animation-time', anim, 'value', Gio.SettingsBindFlags.DEFAULT);
        behaviour.add(anim);
        page.add(behaviour);

        const keys = new Adw.PreferencesGroup({
            title: 'Keyboard shortcuts',
            description: 'GTK accelerator syntax, e.g. <Control><Super>Down. Press Enter or the ✓ to apply; leave empty to disable.',
        });
        keys.add(shortcutRow(settings, 'efg-toggle-app-expose', 'Toggle App Exposé'));
        keys.add(shortcutRow(settings, 'efg-toggle-overview', 'Toggle overview (Mission Control)'));
        page.add(keys);

        const cli = new Adw.PreferencesGroup({
            title: 'Other triggers',
            description: `D-Bus: ${SHELL_BUS_NAME} ${SHELL_OBJECT_PATH}\n` +
                'methods ToggleAppExpose, ToggleOverview, ToggleAppGrid, SwitchToLastWindow, …\n' +
                'Mouse buttons and the expose-for-gnome-ctl CLI come with the optional companion app:',
        });
        const link = new Adw.ActionRow({
            title: 'Companion app (button mapper)',
            subtitle: HOMEPAGE,
            activatable: true,
        });
        link.add_suffix(new Gtk.Image({icon_name: 'adw-external-link-symbolic'}));
        link.connect('activated', () => {
            new Gtk.UriLauncher({uri: HOMEPAGE}).launch(window, null, null);
        });
        cli.add(link);
        page.add(cli);

        window.add(page);
    }
}
