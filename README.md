# Expose for GNOME

macOS-style **App Exposé** for GNOME Shell, plus an optional **mouse-button mapper**
so the extra buttons on your mouse (or a macro pad, or a gaming mouse that sends
keystrokes) can open it.

- **App Exposé**: shows every window of the focused app, spread out on top of a
  dimmed desktop. Click one to focus it; press Esc or click the background to close.
  Default shortcut: <kbd>Ctrl</kbd>+<kbd>Super</kbd>+<kbd>↓</kbd> (change it in the
  extension's settings).
- **Button mapper** (optional companion app): assign any button or key on any input
  device to App Exposé, the Activities overview, the app grid, workspace switching,
  "switch to last window", a keyboard shortcut or a command. Press a button to assign
  it. Mappings can apply to one device or to all of them.
- **Logitech HID++**: buttons the mouse firmware handles itself (DPI switch,
  gesture button, …) are listed and can be mapped too, for devices that support
  HID++ 2.0 control re-programming (`REPROG_CONTROLS_V4`), wired, Bluetooth or
  through a Unifying/Bolt receiver.

Works on GNOME Shell 45 to 51, on X11 and Wayland. (GNOME 49 and later are Wayland-only on most distributions.)

Homepage: <https://github.com/likewise4482/Expose-for-gnome>

## Install

Requirements: GNOME Shell 45–51, Python 3 with PyGObject, GTK 4 and libadwaita
(already present on most GNOME desktops). `python3-evdev` is needed for the mapper on
Wayland and for device-specific mappings without X11.

| Distribution | Packages |
| --- | --- |
| Ubuntu, Debian, Zorin, Pop!_OS | `sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 python3-evdev` |
| Fedora | `sudo dnf install python3-gobject gtk4 libadwaita python3-evdev` |
| Arch | `sudo pacman -S python-gobject gtk4 libadwaita python-evdev` |
| openSUSE | `sudo zypper install python3-gobject gtk4 libadwaita python3-evdev` |

Then, as your normal user (no `sudo`):

```sh
git clone https://github.com/likewise4482/Expose-for-gnome.git
cd Expose-for-gnome
./install.sh
```

This installs, for your user only:

- the extension into `~/.local/share/gnome-shell/extensions/expose-for-gnome@likewise4482.github.io`
  and enables it;
- the app into `~/.local/share/expose-for-gnome`, launchers in `~/.local/bin`, an
  "Expose for GNOME" entry in the app grid;
- a systemd user service `expose-for-gnome.service` that runs the mapper in the
  background.

Then load the extension: on **X11** press <kbd>Alt</kbd>+<kbd>F2</kbd>, type `r`, press
Enter; on **Wayland** log out and back in.

Only want App Exposé with the keyboard shortcut? `./install.sh --extension-only`.
The extension alone is also what is published on extensions.gnome.org.

## First run and permissions

Open **Expose for GNOME** from the app grid. The *Setup* page shows what is working
and what is missing:

- **X11 sessions** need no setup for basic mapping: the mapper grabs buttons through
  the X server, per device or for all devices.
- **Wayland sessions** (and the evdev backend on X11) need to read input devices and
  create a virtual one. *Set up permissions* runs a small script with `pkexec` (you
  are asked for your password once) that installs udev rules.
- **Logitech firmware buttons** (HID++) need access to Logitech `hidraw` devices; the
  same setup step covers that. Solaar uses the same kind of rule.

The rules (`/etc/udev/rules.d/70-expose-for-gnome-*.rules`) use the udev `uaccess`
tag: access goes to whoever is logged in at the active local session, the same way
webcams and sound cards work. It takes effect immediately, no logout needed.

**Security note:** with the input rule in place, any program you run in your desktop
session can read keyboard and mouse events and create virtual input devices. On
X11 that is already true for every program; on Wayland it removes an isolation
layer. If that matters to you, use the X11 backend, or skip the input rule and
use only the keyboard shortcut. You can remove the rules at any time from the
Setup page or with `pkexec ~/.local/share/expose-for-gnome/scripts/setup-permissions.sh --remove`.
You can also run the script yourself with `sudo` instead of `pkexec`:
`--input` (input devices and uinput), `--hidraw` (Logitech HID++), `--remove`.

## Mapping buttons

On the *Buttons* page:

1. Pick **Use suggested** to map the usual thumb buttons (Back → App Exposé,
   Forward → Activities overview) on all mice, or
2. Click **Press…** and press the button or key you want to map. The app records
   which device it came from; choose whether the mapping applies to that device only
   or to every device, then choose the action. Esc cancels.

Each device lists the buttons it says it has, so you can also assign without
pressing. Mapped buttons are swallowed: the application under the pointer no longer
sees them. Choose **Do nothing (block)** to disable a button entirely.

Settings live in `~/.config/expose-for-gnome/config.json`. The command-line tool
`expose-for-gnome-ctl` can trigger every action (handy for custom shortcuts) and
shows status: `expose-for-gnome-ctl status`, `… devices`, `… hidpp`, `… learn`,
`… app-expose`.

### Backends

| Backend | Sessions | Setup | Notes |
| --- | --- | --- | --- |
| X11 grab | X11 | none | Per-device grabs via XInput 2. X11 cannot deliver key codes above 247 (for example `KEY_MACRO1`); the app tells you when a mapping needs the evdev backend. |
| evdev + uinput | X11, Wayland | one-time permission setup | Grabs only the devices that have a mapped button and passes everything else through a virtual clone. |

*Automatic* picks X11 on X11 sessions and evdev otherwise.

### Other mouse tools

libratbag/Piper and OpenRazer configure buttons in the mouse's own memory. They are
not required and not integrated; if you remap a button there to send a key (for
example F13), you can map that key here. If Solaar or logiops is already diverting a
Logitech button, let only one tool manage that button.

## Uninstall

From the cloned folder (or `~/.local/share/expose-for-gnome/uninstall.sh`):

```sh
./uninstall.sh          # removes extension, app and service; keeps your button config
./uninstall.sh --purge  # also deletes the config and the extension's settings
```

If the permission rules are installed, the script offers to remove them first (it
asks for your password).

## Troubleshooting

- *Service status* on the Setup page, or `journalctl --user -u expose-for-gnome`.
- Extension errors: `journalctl --user -b | grep -i expose` (Wayland) or
  `journalctl -b /usr/bin/gnome-shell`.
- The mapper talks to the extension over D-Bus at `io.github.likewise4482.ExposeForGnome.Shell`;
  `expose-for-gnome-ctl app-expose` tests that path without any mouse involved.

## Licence

GPL-2.0-or-later. See [LICENSE](LICENSE). Contributions: see
[CONTRIBUTING.md](CONTRIBUTING.md).

This project is not affiliated with or endorsed by the GNOME Foundation. "GNOME" is a
trademark of the GNOME Foundation and is used here only to say what the software
works with.
