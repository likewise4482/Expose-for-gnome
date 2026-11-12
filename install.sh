#!/usr/bin/env bash
# Per-user install (no root): GNOME Shell extension + button mapper app.
#   ./install.sh                 install / update everything
#   ./install.sh --extension-only
#   ./install.sh --mapper-only
#   ./install.sh --dev           symlink the source tree instead of copying (for hacking)
set -euo pipefail

SRC="$(cd "$(dirname "$(realpath "$0")")" && pwd)"
# shellcheck source=project.env
. "$SRC/project.env"

DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
APP_HOME="$DATA_HOME/$SLUG"
BIN_DIR="$HOME/.local/bin"
UNIT_DIR="$CONFIG_HOME/systemd/user"
APPS_DIR="$DATA_HOME/applications"
ICON_DIR="$DATA_HOME/icons/hicolor/scalable/apps"
EXT_DIR="$DATA_HOME/gnome-shell/extensions/$EXTENSION_UUID"

do_ext=1 do_mapper=1 dev=0
for arg in "$@"; do
    case "$arg" in
        --extension-only) do_mapper=0 ;;
        --mapper-only) do_ext=0 ;;
        --dev) dev=1 ;;
        -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

say() { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }

pkg_hint() {
    if command -v apt-get >/dev/null; then echo "sudo apt install $1"
    elif command -v dnf >/dev/null; then echo "sudo dnf install $2"
    elif command -v pacman >/dev/null; then echo "sudo pacman -S $3"
    elif command -v zypper >/dev/null; then echo "sudo zypper install $2"
    else echo "install: $1"; fi
}

# --- checks -----------------------------------------------------------------
shell_major=""
if command -v gnome-shell >/dev/null; then
    shell_major="$(gnome-shell --version 2>/dev/null | sed -n 's/.* \([0-9][0-9]*\).*/\1/p')"
fi
if [ -n "$shell_major" ] && ! printf ' %s ' "$SHELL_VERSIONS" | grep -q " $shell_major "; then
    warn "GNOME Shell $shell_major is not in the tested range ($SHELL_VERSIONS); the extension may not load."
fi

if [ "$do_mapper" = 1 ]; then
    python3 -c 'import gi' 2>/dev/null || {
        echo "python3-gi is required: $(pkg_hint python3-gi python3-gobject python-gobject)" >&2; exit 1; }
    python3 -c 'import gi; gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")' 2>/dev/null ||
        warn "GTK4/libadwaita bindings missing (needed for the settings app): $(pkg_hint 'gir1.2-gtk-4.0 gir1.2-adw-1' 'gtk4 libadwaita' 'gtk4 libadwaita')"
    python3 -c 'import evdev' 2>/dev/null ||
        warn "python3-evdev missing (needed on Wayland, optional on X11): $(pkg_hint python3-evdev python3-evdev python-evdev)"
fi

# --- extension --------------------------------------------------------------
if [ "$do_ext" = 1 ]; then
    say "Installing the GNOME Shell extension ($EXTENSION_UUID)"
    mkdir -p "$(dirname "$EXT_DIR")"
    if [ "$dev" = 1 ]; then
        glib-compile-schemas "$SRC/extension/schemas"
        rm -rf "$EXT_DIR"
        ln -sfn "$SRC/extension" "$EXT_DIR"
    elif command -v gnome-extensions >/dev/null; then
        build="$(mktemp -d)"
        trap 'rm -rf "$build"' EXIT
        gnome-extensions pack --force --extra-source=ids.js --out-dir="$build" "$SRC/extension" >/dev/null
        gnome-extensions install --force "$build/$EXTENSION_UUID.shell-extension.zip"
        # GNOME 44+ compiles schemas on load, older tools leave that to us; harmless either way.
        [ -d "$EXT_DIR/schemas" ] && glib-compile-schemas "$EXT_DIR/schemas" || true
    else
        rm -rf "$EXT_DIR"
        mkdir -p "$EXT_DIR"
        cp -r "$SRC/extension/." "$EXT_DIR/"
        glib-compile-schemas "$EXT_DIR/schemas"
    fi

    if ! gnome-extensions enable "$EXTENSION_UUID" 2>/dev/null; then
        # The running shell has not discovered the extension yet: add it to the list
        # so it is enabled on the next shell start.
        python3 - "$EXTENSION_UUID" <<'EOF' || warn "could not enable the extension; enable it in the Extensions app"
import sys
from gi.repository import Gio
uuid = sys.argv[1]
s = Gio.Settings.new('org.gnome.shell')
exts = s.get_strv('enabled-extensions')
if uuid not in exts:
    s.set_strv('enabled-extensions', exts + [uuid])
s.set_strv('disabled-extensions', [e for e in s.get_strv('disabled-extensions') if e != uuid])
Gio.Settings.sync()
EOF
    fi
fi

# --- mapper app ---------------------------------------------------------------
if [ "$do_mapper" = 1 ]; then
    say "Installing the button mapper app to $APP_HOME"
    mkdir -p "$BIN_DIR" "$UNIT_DIR" "$APPS_DIR" "$ICON_DIR"
    rm -rf "$APP_HOME"
    if [ "$dev" = 1 ]; then
        ln -sfn "$SRC" "$APP_HOME"
    else
        mkdir -p "$APP_HOME"
        cp -r "$SRC/bin" "$SRC/scripts" "$SRC/project.env" "$SRC/uninstall.sh" "$APP_HOME/"
        mkdir -p "$APP_HOME/mapper"
        cp -r "$SRC/mapper/expose_for_gnome" "$APP_HOME/mapper/"
        find "$APP_HOME" -name __pycache__ -prune -exec rm -rf {} +
    fi
    for f in "$SLUG" "$SLUG-daemon" "$SLUG-ctl"; do
        chmod +x "$APP_HOME/bin/$f"
        ln -sfn "$APP_HOME/bin/$f" "$BIN_DIR/$f"
    done

    subst() {
        sed -e "s|@BIN@|$BIN_DIR|g" -e "s|@SLUG@|$SLUG|g" -e "s|@APP_ID@|$APP_ID|g" \
            -e "s|@PRODUCT_NAME@|$PRODUCT_NAME|g" -e "s|@HOMEPAGE@|$HOMEPAGE|g" "$1"
    }
    subst "$SRC/data/daemon.service.in" > "$UNIT_DIR/$SYSTEMD_UNIT"
    subst "$SRC/data/app.desktop.in" > "$APPS_DIR/$APP_ID.desktop"
    cp "$SRC/data/icon.svg" "$ICON_DIR/$APP_ID.svg"
    update-desktop-database "$APPS_DIR" 2>/dev/null || true
    gtk-update-icon-cache -q -t "$DATA_HOME/icons/hicolor" 2>/dev/null || true

    if systemctl --user show-environment >/dev/null 2>&1; then
        systemctl --user daemon-reload
        systemctl --user enable "$SYSTEMD_UNIT" >/dev/null 2>&1 || true
        systemctl --user restart "$SYSTEMD_UNIT" || warn "could not start $SYSTEMD_UNIT (see: journalctl --user -u $SYSTEMD_UNIT)"
    else
        warn "no systemd user session; start $BIN_DIR/$SLUG-daemon from your session's autostart"
    fi
    case ":$PATH:" in *":$BIN_DIR:"*) ;; *) warn "$BIN_DIR is not in PATH (the app launcher still works)";; esac
fi

say "Installed."
if [ "$do_ext" = 1 ] && ! gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus \
        --method org.freedesktop.DBus.NameHasOwner "$SHELL_BUS_NAME" 2>/dev/null | grep -q true; then
    if [ "${XDG_SESSION_TYPE:-}" = x11 ]; then
        echo "Reload GNOME Shell to load the extension: press Alt+F2, type r, press Enter."
    else
        echo "Log out and back in to load the extension (Wayland cannot reload the shell)."
    fi
fi
[ "$do_mapper" = 1 ] && echo "Then open \"$PRODUCT_NAME\" from the app grid to map buttons (first run explains any setup)."
exit 0
