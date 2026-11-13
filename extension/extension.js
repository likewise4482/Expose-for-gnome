import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import St from 'gi://St';

import * as Config from 'resource:///org/gnome/shell/misc/config.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

import {SHELL_BUS_NAME, SHELL_OBJECT_PATH, VERSION} from './ids.js';

const DBUS_IFACE = `
<node>
  <interface name="${SHELL_BUS_NAME}">
    <method name="ToggleAppExpose"/>
    <method name="ShowAppExpose"/>
    <method name="HideAppExpose"/>
    <method name="ToggleOverview"/>
    <method name="ToggleAppGrid"/>
    <method name="WorkspacePrevious"/>
    <method name="WorkspaceNext"/>
    <method name="SwitchToLastWindow"/>
    <property name="AppExposeVisible" type="b" access="read"/>
    <property name="Version" type="s" access="read"/>
    <property name="ShellVersion" type="s" access="read"/>
  </interface>
</node>`;

const WINDOW_TYPES = [
    Meta.WindowType.NORMAL,
    Meta.WindowType.DIALOG,
    Meta.WindowType.MODAL_DIALOG,
];

const PADDING = 32;
const GAP = 28;
const HIGHLIGHT_PAD = 6;

/**
 * Exposé layout: split the windows (in order) into rows, scale every window
 * by the same factor and pick the row count that gives the largest factor.
 *
 * @param {{w: number, h: number}[]} sizes window sizes
 * @param {{x: number, y: number, width: number, height: number}} area target area
 * @returns {{x: number, y: number, w: number, h: number}[]} slots, same order as sizes
 */
function computeLayout(sizes, area) {
    const n = sizes.length;
    if (n === 0)
        return [];

    let best = null;
    for (let rows = 1; rows <= n; rows++) {
        const perRow = Math.ceil(n / rows);
        const rowList = [];
        for (let i = 0; i < n; i += perRow)
            rowList.push(sizes.slice(i, i + perRow).map((s, j) => ({...s, index: i + j})));

        const rowH = (area.height - GAP * (rowList.length - 1)) / rowList.length;
        let scale = 1;
        for (const row of rowList) {
            const totalW = row.reduce((acc, s) => acc + s.w, 0);
            const maxH = Math.max(...row.map(s => s.h));
            scale = Math.min(scale,
                (area.width - GAP * (row.length - 1)) / totalW,
                rowH / maxH);
        }
        if (!best || scale > best.scale + 1e-6)
            best = {scale, rowList};
    }

    const {scale, rowList} = best;
    const slots = new Array(n);
    const rowHeights = rowList.map(row => Math.max(...row.map(s => s.h)) * scale);
    const totalH = rowHeights.reduce((a, b) => a + b, 0) + GAP * (rowList.length - 1);
    let y = area.y + (area.height - totalH) / 2;
    rowList.forEach((row, r) => {
        const rowW = row.reduce((acc, s) => acc + s.w * scale, 0) + GAP * (row.length - 1);
        let x = area.x + (area.width - rowW) / 2;
        for (const s of row) {
            const w = s.w * scale;
            const h = s.h * scale;
            slots[s.index] = {
                x: Math.round(x),
                y: Math.round(y + (rowHeights[r] - h) / 2),
                w: Math.round(w),
                h: Math.round(h),
            };
            x += w + GAP;
        }
        y += rowHeights[r] + GAP;
    });
    return slots;
}

function truncate(text, max) {
    if (!text)
        return '';
    return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

class AppExposeView {
    constructor(settings, onDestroyed) {
        this._settings = settings;
        this._onDestroyed = onDestroyed;
        this._items = [];
        this._hiddenActors = [];
        this._windowSignals = [];
        this._selected = -1;
        this._closing = false;
        this._grab = null;
        this._root = null;
        this._timeouts = new Set();
    }

    _addTimeout(ms, func) {
        const id = GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, () => {
            this._timeouts.delete(id);
            func();
            return GLib.SOURCE_REMOVE;
        });
        this._timeouts.add(id);
    }

    _clearTimeouts() {
        for (const id of this._timeouts)
            GLib.source_remove(id);
        this._timeouts.clear();
    }

    get _duration() {
        return St.Settings.get().enable_animations
            ? this._settings.get_int('animation-time') : 0;
    }

    open() {
        const tracker = Shell.WindowTracker.get_default();
        let focusWin = global.display.focus_window;
        if (focusWin && !WINDOW_TYPES.includes(focusWin.get_window_type()))
            focusWin = null;
        const app = (focusWin ? tracker.get_window_app(focusWin) : null) ?? tracker.focus_app;
        if (!app)
            return false;

        const activeWs = global.workspace_manager.get_active_workspace();
        const allWs = this._settings.get_boolean('all-workspaces');
        const windows = app.get_windows()
            .filter(w => WINDOW_TYPES.includes(w.get_window_type()) && !w.skip_taskbar)
            .filter(w => allWs || w.located_on_workspace(activeWs))
            .sort((a, b) => a.get_stable_sequence() - b.get_stable_sequence());
        const shown = windows.filter(w => !w.minimized && w.get_compositor_private());
        const minimized = this._settings.get_boolean('include-minimized')
            ? windows.filter(w => w.minimized) : [];
        if (shown.length + minimized.length === 0)
            return false;

        let monitor = focusWin?.get_monitor() ?? -1;
        if (monitor < 0)
            monitor = Main.layoutManager.primaryIndex;
        const work = Main.layoutManager.getWorkAreaForMonitor(monitor);

        this._root = new St.Widget({
            reactive: true,
            can_focus: true,
            x: 0,
            y: 0,
            width: global.stage.width,
            height: global.stage.height,
        });
        Main.layoutManager.uiGroup.add_child(this._root);

        this._grab = Main.pushModal(this._root, {actionMode: Shell.ActionMode.POPUP});
        // Clutter.Grab.get_seat_state() was removed in GNOME 50.
        if (this._grab.get_seat_state &&
            (this._grab.get_seat_state() & Clutter.GrabState.KEYBOARD) === 0) {
            Main.popModal(this._grab);
            this._grab = null;
            this._root.destroy();
            this._root = null;
            return false;
        }

        this._backdrop = new St.Widget({
            style_class: 'efg-backdrop',
            reactive: true,
            x: 0,
            y: 0,
            width: global.stage.width,
            height: global.stage.height,
            opacity: 0,
        });
        // Clicks and hovers over a thumbnail can land here instead of on its
        // Clutter.Clone: picking a clone goes through its source window's paint
        // volume, which mutter sometimes cannot provide (e.g. when the window
        // is fully covered by another one). So hit-test the slots ourselves.
        this._backdrop.connect('motion-event', (_a, event) => {
            const index = this._itemAt(...event.get_coords());
            if (index >= 0 && index !== this._selected)
                this._select(index);
            return Clutter.EVENT_PROPAGATE;
        });
        this._backdrop.connect('button-release-event', (_a, event) => {
            const button = event.get_button();
            const index = this._itemAt(...event.get_coords());
            if (button === 1 && index >= 0)
                this.close(this._items[index].win);
            else if (button <= 3)
                this.close(null);
            return Clutter.EVENT_STOP;
        });
        this._root.add_child(this._backdrop);

        this._chrome = new St.Widget({x: 0, y: 0, opacity: 0});
        this._root.add_child(this._chrome);

        const header = new St.BoxLayout({style_class: 'efg-header'});
        header.add_child(app.create_icon_texture(40));
        header.add_child(new St.Label({
            text: app.get_name(),
            style_class: 'efg-header-label',
            y_align: Clutter.ActorAlign.CENTER,
        }));
        this._chrome.add_child(header);
        const [, headerW] = header.get_preferred_width(-1);
        const [, headerH] = header.get_preferred_height(-1);
        header.set_position(Math.round(work.x + (work.width - headerW) / 2), work.y + PADDING / 2);

        let bottomReserve = 0;
        if (minimized.length > 0)
            bottomReserve = this._buildMinimizedStrip(app, minimized, work);

        this._highlight = new St.Widget({style_class: 'efg-highlight', visible: false});
        this._root.add_child(this._highlight);

        this._clones = new St.Widget({x: 0, y: 0});
        this._root.add_child(this._clones);

        this._title = new St.Label({style_class: 'efg-title', visible: false});
        this._root.add_child(this._title);

        const area = {
            x: work.x + PADDING,
            y: work.y + PADDING / 2 + headerH + PADDING / 2,
            width: work.width - 2 * PADDING,
            height: work.height - (PADDING / 2 + headerH + PADDING / 2) - PADDING - bottomReserve,
        };
        const sizes = shown.map(w => {
            const actor = w.get_compositor_private();
            return {w: Math.max(actor.width, 1), h: Math.max(actor.height, 1)};
        });
        const slots = computeLayout(sizes, area);
        const duration = this._duration;

        shown.forEach((win, index) => {
            const actor = win.get_compositor_private();
            const slot = slots[index];
            const animateFromWindow = actor.visible && win.located_on_workspace(activeWs);
            const clone = new Clutter.Clone({
                source: actor,
                reactive: true,
                x: animateFromWindow ? actor.x : slot.x,
                y: animateFromWindow ? actor.y : slot.y,
                width: animateFromWindow ? actor.width : slot.w,
                height: animateFromWindow ? actor.height : slot.h,
                opacity: animateFromWindow ? 255 : 0,
            });
            clone.connect('enter-event', () => this._select(index));
            clone.connect('button-press-event', () => Clutter.EVENT_STOP);
            clone.connect('button-release-event', (_a, event) => {
                if (event.get_button() === 1)
                    this.close(win);
                return Clutter.EVENT_STOP;
            });
            this._clones.add_child(clone);
            clone.ease({
                x: slot.x,
                y: slot.y,
                width: slot.w,
                height: slot.h,
                opacity: 255,
                duration,
                mode: Clutter.AnimationMode.EASE_OUT_QUAD,
            });

            if (animateFromWindow) {
                this._hiddenActors.push([actor, actor.opacity]);
                actor.opacity = 0;
            }
            this._items.push({win, clone, slot});
        });

        // Stack the clones like the real windows, so overlapping windows don't
        // swap places while they animate out of (and back into) position.
        for (const win of global.display.sort_windows_by_stacking(shown)) {
            const item = this._items.find(it => it.win === win);
            this._clones.set_child_above_sibling(item.clone, null);
        }

        for (const win of windows) {
            this._windowSignals.push([win, win.connect('unmanaged', () => this.close(null, true))]);
        }

        if (shown.length === 0) {
            const label = new St.Label({
                text: 'All windows are minimized',
                style_class: 'efg-empty',
            });
            this._chrome.add_child(label);
            const [, lw] = label.get_preferred_width(-1);
            label.set_position(Math.round(work.x + (work.width - lw) / 2),
                Math.round(area.y + area.height / 2));
        }

        this._backdrop.ease({opacity: 255, duration, mode: Clutter.AnimationMode.EASE_OUT_QUAD});
        this._chrome.ease({opacity: 255, duration, mode: Clutter.AnimationMode.EASE_OUT_QUAD});

        this._root.connect('key-press-event', this._onKeyPress.bind(this));
        this._root.connect('captured-event', this._onCapturedEvent.bind(this));
        this._root.grab_key_focus();

        const focusIndex = this._items.findIndex(it => it.win === focusWin);
        if (this._items.length > 0) {
            this._addTimeout(duration, () => {
                if (!this._closing && this._selected < 0)
                    this._select(Math.max(focusIndex, 0));
            });
        }
        return true;
    }

    _buildMinimizedStrip(app, minimized, work) {
        const strip = new St.BoxLayout({style_class: 'efg-min-strip'});
        for (const win of minimized) {
            const box = new St.BoxLayout({style_class: 'efg-min-card-box'});
            box.add_child(app.create_icon_texture(24));
            box.add_child(new St.Label({
                text: truncate(win.get_title() || app.get_name(), 40),
                y_align: Clutter.ActorAlign.CENTER,
            }));
            const button = new St.Button({
                style_class: 'efg-min-card',
                child: box,
                track_hover: true,
            });
            button.connect('clicked', () => this.close(win));
            strip.add_child(button);
        }
        this._chrome.add_child(strip);
        const maxW = work.width - 2 * PADDING;
        let [, stripW] = strip.get_preferred_width(-1);
        stripW = Math.min(stripW, maxW);
        strip.set_width(stripW);
        const [, stripH] = strip.get_preferred_height(stripW);
        strip.set_position(Math.round(work.x + (work.width - stripW) / 2),
            Math.round(work.y + work.height - PADDING - stripH));
        return stripH + PADDING;
    }

    _itemAt(x, y) {
        return this._items.findIndex(({slot}) =>
            x >= slot.x && x < slot.x + slot.w && y >= slot.y && y < slot.y + slot.h);
    }

    _select(index) {
        if (this._closing || index < 0 || index >= this._items.length)
            return;
        this._selected = index;
        const {win, slot} = this._items[index];

        this._highlight.set_position(slot.x - HIGHLIGHT_PAD, slot.y - HIGHLIGHT_PAD);
        this._highlight.set_size(slot.w + 2 * HIGHLIGHT_PAD, slot.h + 2 * HIGHLIGHT_PAD);
        this._highlight.show();

        this._title.text = truncate(win.get_title() || '', 80);
        const [, tw] = this._title.get_preferred_width(-1);
        const [, th] = this._title.get_preferred_height(-1);
        const x = Math.max(0, Math.min(global.stage.width - tw, slot.x + (slot.w - tw) / 2));
        const y = Math.min(global.stage.height - th, slot.y + slot.h - th / 2);
        this._title.set_position(Math.round(x), Math.round(y));
        this._title.show();
    }

    _moveSelection(dx, dy) {
        if (this._items.length === 0)
            return;
        if (this._selected < 0) {
            this._select(0);
            return;
        }
        const cur = this._items[this._selected].slot;
        const cx = cur.x + cur.w / 2;
        const cy = cur.y + cur.h / 2;
        let best = -1;
        let bestScore = Infinity;
        this._items.forEach((it, i) => {
            if (i === this._selected)
                return;
            const ox = it.slot.x + it.slot.w / 2 - cx;
            const oy = it.slot.y + it.slot.h / 2 - cy;
            const along = ox * dx + oy * dy;
            if (along <= 0)
                return;
            const across = Math.abs(ox * dy) + Math.abs(oy * dx);
            const score = along + 2 * across;
            if (score < bestScore) {
                bestScore = score;
                best = i;
            }
        });
        if (best >= 0)
            this._select(best);
    }

    // Our grab keeps events inside this._root, so the stage-level handler in
    // the extension never sees side buttons while App Exposé is open.
    _onCapturedEvent(_actor, event) {
        const type = event.type();
        if (type !== Clutter.EventType.BUTTON_PRESS && type !== Clutter.EventType.BUTTON_RELEASE)
            return Clutter.EVENT_PROPAGATE;
        if (event.get_button() < 8 || !this._settings.get_boolean('dismiss-with-extra-buttons'))
            return Clutter.EVENT_PROPAGATE;
        if (type === Clutter.EventType.BUTTON_RELEASE)
            this.close(null);
        return Clutter.EVENT_STOP;
    }

    _onKeyPress(_actor, event) {
        const sym = event.get_key_symbol();
        switch (sym) {
        case Clutter.KEY_Escape:
            this.close(null);
            break;
        case Clutter.KEY_Return:
        case Clutter.KEY_KP_Enter:
        case Clutter.KEY_space:
            if (this._selected >= 0)
                this.close(this._items[this._selected].win);
            break;
        case Clutter.KEY_Left:
            this._moveSelection(-1, 0);
            break;
        case Clutter.KEY_Right:
            this._moveSelection(1, 0);
            break;
        case Clutter.KEY_Up:
            this._moveSelection(0, -1);
            break;
        case Clutter.KEY_Down:
            this._moveSelection(0, 1);
            break;
        case Clutter.KEY_Tab:
            if (this._items.length)
                this._select((this._selected + 1) % this._items.length);
            break;
        case Clutter.KEY_ISO_Left_Tab:
            if (this._items.length)
                this._select((this._selected - 1 + this._items.length) % this._items.length);
            break;
        }
        return Clutter.EVENT_STOP;
    }

    close(win = null, immediate = false) {
        if (this._closing) {
            if (immediate && this._finish) {
                this._clearTimeouts();
                this._finish();
            }
            return;
        }
        this._closing = true;
        this._clearTimeouts();

        for (const [w, id] of this._windowSignals)
            w.disconnect(id);
        this._windowSignals = [];

        if (this._grab) {
            Main.popModal(this._grab);
            this._grab = null;
        }

        const duration = immediate ? 0 : this._duration;
        this._highlight?.hide();
        this._title?.hide();

        // The chosen window ends up on top, so its clone must fly back on top.
        const chosen = this._items.find(it => it.win === win);
        if (chosen)
            this._clones.set_child_above_sibling(chosen.clone, null);

        const startWs = global.workspace_manager.get_active_workspace();
        for (const {win: w, clone} of this._items) {
            const actor = w.get_compositor_private();
            const returns = actor && (w === win || (!w.minimized && w.located_on_workspace(startWs)));
            if (returns) {
                clone.ease({
                    x: actor.x,
                    y: actor.y,
                    width: actor.width,
                    height: actor.height,
                    duration,
                    mode: Clutter.AnimationMode.EASE_OUT_QUAD,
                });
            } else {
                clone.ease({opacity: 0, duration, mode: Clutter.AnimationMode.EASE_OUT_QUAD});
            }
        }
        this._backdrop?.ease({opacity: 0, duration, mode: Clutter.AnimationMode.EASE_OUT_QUAD});
        this._chrome?.ease({opacity: 0, duration, mode: Clutter.AnimationMode.EASE_OUT_QUAD});

        if (win)
            Main.activateWindow(win);

        this._finish = () => {
            this._finish = null;
            for (const [actor, opacity] of this._hiddenActors) {
                try {
                    actor.opacity = opacity;
                } catch {
                    // The window was destroyed while App Exposé was open.
                }
            }
            this._hiddenActors = [];
            this._root?.destroy();
            this._root = null;
            this._items = [];
            this._onDestroyed?.();
        };
        if (duration === 0)
            this._finish();
        else
            this._addTimeout(duration + 20, () => this._finish?.());
    }
}

export default class ExposeForGnomeExtension extends Extension {
    enable() {
        this._settings = this.getSettings();
        this._switcherSettings = new Gio.Settings({schema_id: 'org.gnome.shell.window-switcher'});
        this._view = null;
        this._pendingExtraButton = false;

        Main.wm.addKeybinding('efg-toggle-app-expose', this._settings,
            Meta.KeyBindingFlags.IGNORE_AUTOREPEAT,
            Shell.ActionMode.NORMAL | Shell.ActionMode.OVERVIEW | Shell.ActionMode.POPUP,
            () => this.toggleAppExpose());
        Main.wm.addKeybinding('efg-toggle-overview', this._settings,
            Meta.KeyBindingFlags.IGNORE_AUTOREPEAT,
            Shell.ActionMode.NORMAL | Shell.ActionMode.OVERVIEW,
            () => this.toggleOverview());

        this._capturedId = global.stage.connect('captured-event',
            this._onCapturedEvent.bind(this));

        const ext = this;
        this._dbus = Gio.DBusExportedObject.wrapJSObject(DBUS_IFACE, {
            ToggleAppExpose() {
                ext.toggleAppExpose();
            },
            ShowAppExpose() {
                if (!ext._view)
                    ext.toggleAppExpose();
            },
            HideAppExpose() {
                ext._view?.close(null);
            },
            ToggleOverview() {
                ext.toggleOverview();
            },
            ToggleAppGrid() {
                ext.toggleAppGrid();
            },
            WorkspacePrevious() {
                ext.moveWorkspace(-1);
            },
            WorkspaceNext() {
                ext.moveWorkspace(1);
            },
            SwitchToLastWindow() {
                ext.switchToLastWindow();
            },
            get AppExposeVisible() {
                return ext._view !== null;
            },
            get Version() {
                return VERSION;
            },
            get ShellVersion() {
                return Config.PACKAGE_VERSION;
            },
        });
        this._dbus.export(Gio.DBus.session, SHELL_OBJECT_PATH);
        this._nameId = Gio.bus_own_name_on_connection(Gio.DBus.session, SHELL_BUS_NAME,
            Gio.BusNameOwnerFlags.NONE, null, null);
    }

    disable() {
        if (this._nameId) {
            Gio.bus_unown_name(this._nameId);
            this._nameId = 0;
        }
        this._dbus?.unexport();
        this._dbus = null;

        if (this._capturedId) {
            global.stage.disconnect(this._capturedId);
            this._capturedId = 0;
        }
        Main.wm.removeKeybinding('efg-toggle-app-expose');
        Main.wm.removeKeybinding('efg-toggle-overview');

        this._view?.close(null, true);
        this._view = null;
        this._settings = null;
        this._switcherSettings = null;
    }

    _sessionBlocked() {
        return Main.sessionMode.isLocked || Main.sessionMode.isGreeter;
    }

    _emitVisibleChanged() {
        this._dbus?.emit_property_changed('AppExposeVisible',
            new GLib.Variant('b', this._view !== null));
    }

    toggleAppExpose() {
        if (this._view) {
            this._view.close(null);
            return;
        }
        if (this._sessionBlocked())
            return;
        if (Main.modalCount > 0 && !Main.overview.visible)
            return;
        if (Main.overview.visible)
            Main.overview.hide();

        const view = new AppExposeView(this._settings, () => {
            if (this._view === view) {
                this._view = null;
                this._emitVisibleChanged();
            }
        });
        if (view.open()) {
            this._view = view;
            this._emitVisibleChanged();
        }
    }

    toggleOverview() {
        if (this._sessionBlocked())
            return;
        this._view?.close(null, true);
        Main.overview.toggle();
    }

    toggleAppGrid() {
        if (this._sessionBlocked())
            return;
        this._view?.close(null, true);
        if (Main.overview.visible)
            Main.overview.hide();
        else
            Main.overview.showApps();
    }

    moveWorkspace(delta) {
        if (this._sessionBlocked())
            return;
        const wm = global.workspace_manager;
        const index = wm.get_active_workspace_index() + delta;
        if (index < 0 || index >= wm.n_workspaces)
            return;
        Main.wm.actionMoveWorkspace(wm.get_workspace_by_index(index));
    }

    switchToLastWindow() {
        if (this._sessionBlocked())
            return;
        this._view?.close(null, true);
        if (Main.overview.visible)
            Main.overview.hide();
        else if (Main.modalCount > 0)
            return;

        const ws = this._switcherSettings.get_boolean('current-workspace-only')
            ? global.workspace_manager.get_active_workspace() : null;
        const focus = global.display.focus_window;
        const target = global.display.get_tab_list(Meta.TabList.NORMAL, ws)
            .find(w => w !== focus && !w.skip_taskbar);
        if (target)
            Main.activateWindow(target);
    }

    // While the overview is open the shell holds the pointer grab (on the
    // stage), so the mapper daemon never sees the side buttons; handle them here.
    _onCapturedEvent(_stage, event) {
        const type = event.type();
        if (type !== Clutter.EventType.BUTTON_PRESS && type !== Clutter.EventType.BUTTON_RELEASE)
            return Clutter.EVENT_PROPAGATE;
        if (event.get_button() < 8 || !this._settings.get_boolean('dismiss-with-extra-buttons'))
            return Clutter.EVENT_PROPAGATE;

        if (type === Clutter.EventType.BUTTON_PRESS) {
            this._pendingExtraButton = Main.overview.visible;
            return this._pendingExtraButton ? Clutter.EVENT_STOP : Clutter.EVENT_PROPAGATE;
        }

        if (!this._pendingExtraButton)
            return Clutter.EVENT_PROPAGATE;
        this._pendingExtraButton = false;
        if (Main.overview.visible)
            Main.overview.hide();
        return Clutter.EVENT_STOP;
    }
}
