#!/bin/sh
# One-time root setup for Expose for GNOME's button mapper.
#
#   --input    read /dev/input/event* and write /dev/uinput (evdev backend: Wayland,
#              or device-specific learn mode); not needed for the X11 backend
#   --hidraw   talk HID++ to Logitech devices (firmware buttons such as a DPI switch)
#   --remove   undo everything this script installed
#
# Access is granted with udev "uaccess" rules: only to the user of the active local
# session (like a webcam or sound card), effective immediately, no logout needed.
# Run from the app (it uses pkexec) or: sudo ./scripts/setup-permissions.sh --input --hidraw
set -eu

PREFIX=70-expose-for-gnome
RULES=/etc/udev/rules.d
INPUT_RULE="$RULES/$PREFIX-input.rules"
HIDRAW_RULE="$RULES/$PREFIX-logitech-hidraw.rules"
MODULES_CONF=/etc/modules-load.d/expose-for-gnome-uinput.conf

if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root: pkexec $0 $* (or sudo)" >&2
    exit 1
fi
[ $# -gt 0 ] || set -- --input --hidraw

input=0 hidraw=0 remove=0
for arg in "$@"; do
    case "$arg" in
        --input|--evdev) input=1 ;;
        --hidraw) hidraw=1 ;;
        --remove|--uninstall) remove=1 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

reload() {
    udevadm control --reload-rules
    udevadm trigger --subsystem-match=misc --sysname-match=uinput --action=change || true
    udevadm trigger --subsystem-match=input --action=change || true
    udevadm trigger --subsystem-match=hidraw --action=change || true
    udevadm settle --timeout=5 || true
}

if [ "$remove" = 1 ]; then
    rm -f "$INPUT_RULE" "$HIDRAW_RULE" "$MODULES_CONF"
    reload
    echo "Removed $PREFIX udev rules."
    exit 0
fi

if [ "$input" = 1 ]; then
    cat > "$INPUT_RULE" <<'EOF'
# Expose for GNOME: let the active local session read input devices and create
# virtual ones (evdev button mapper backend). Remove with setup-permissions.sh --remove.
KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", OPTIONS+="static_node=uinput"
SUBSYSTEM=="input", KERNEL=="event*", TAG+="uaccess"
EOF
    echo uinput > "$MODULES_CONF"
    modprobe uinput 2>/dev/null || true
    echo "Installed $INPUT_RULE"
fi

if [ "$hidraw" = 1 ]; then
    cat > "$HIDRAW_RULE" <<'EOF'
# Expose for GNOME: let the active local session talk HID++ to Logitech devices
# (USB, receivers, Bluetooth). Same idea as Solaar's rule.
SUBSYSTEM=="hidraw", KERNELS=="*:046D:*", TAG+="uaccess"
EOF
    echo "Installed $HIDRAW_RULE"
fi

reload
echo "Done. No logout needed."
