#!/usr/bin/env bash
# Remove the per-user install.
#   ./uninstall.sh           remove extension, app, service (keeps your button config)
#   ./uninstall.sh --purge   also delete the config and the extension's settings
# The optional udev rules (one-time permission setup) are root-owned; this script
# offers to remove them with pkexec/sudo if they exist.
set -euo pipefail

SRC="$(cd "$(dirname "$(realpath "$0")")" && pwd)"
# shellcheck source=project.env
. "$SRC/project.env"

DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
purge=0
[ "${1:-}" = --purge ] && purge=1

if ls /etc/udev/rules.d/"$UDEV_PREFIX"-*.rules >/dev/null 2>&1; then
    echo "Permission rules are installed in /etc/udev/rules.d ($UDEV_PREFIX-*.rules)."
    printf 'Remove them now (needs your password)? [y/N] '
    read -r answer || answer=n
    if [ "$answer" = y ] || [ "$answer" = Y ]; then
        if command -v pkexec >/dev/null; then
            pkexec "$SRC/scripts/setup-permissions.sh" --remove
        else
            sudo "$SRC/scripts/setup-permissions.sh" --remove
        fi
    fi
fi
systemctl --user disable --now "$SYSTEMD_UNIT" 2>/dev/null || true
gnome-extensions disable "$EXTENSION_UUID" 2>/dev/null || true
gnome-extensions uninstall "$EXTENSION_UUID" 2>/dev/null || true

rm -rf "$DATA_HOME/gnome-shell/extensions/$EXTENSION_UUID" "$DATA_HOME/$SLUG"
rm -f "$HOME/.local/bin/$SLUG" "$HOME/.local/bin/$SLUG-daemon" "$HOME/.local/bin/$SLUG-ctl" \
      "$CONFIG_HOME/systemd/user/$SYSTEMD_UNIT" \
      "$DATA_HOME/applications/$APP_ID.desktop" \
      "$DATA_HOME/icons/hicolor/scalable/apps/$APP_ID.svg"
systemctl --user daemon-reload 2>/dev/null || true
update-desktop-database "$DATA_HOME/applications" 2>/dev/null || true

if [ "$purge" = 1 ]; then
    rm -rf "${CONFIG_HOME:?}/$SLUG"
    dconf reset -f "$SCHEMA_PATH" 2>/dev/null || true
fi

echo "Uninstalled. The extension is unloaded at the next shell restart / login."
