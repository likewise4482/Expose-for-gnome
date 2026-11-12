# Contributing to Expose for GNOME

## Layout

| Path | What |
| --- | --- |
| `extension/` | The GNOME Shell extension (GJS, ES modules, GNOME 45+). This folder is what goes to extensions.gnome.org. |
| `mapper/expose_for_gnome/` | The companion app: background service (`daemon.py`), settings app (`gui.py`), CLI (`ctl.py`), X11 and evdev backends, Logitech HID++ (`hidpp.py`). Python 3 + PyGObject. |
| `bin/` | Launchers that run the package from `../mapper`. |
| `scripts/setup-permissions.sh` | The only code that runs as root (udev rules), started through `pkexec`. |
| `data/` | systemd user unit, desktop file, icon (templates). |
| `project.env`, `tools/` | Single source of truth for names and IDs, and the generator for files that embed them. |
| `tests/`, `mapper/tests/` | Unit, repository and integration tests. |

## Names and IDs

Every name, ID and URL (product name, extension UUID, GSettings schema, D-Bus names,
app ID, systemd unit, udev rule prefix, supported shell versions, version) lives in
`project.env`. After editing it:

```sh
make sync-ids   # regenerates metadata.json, ids.js, ids.py and the schema, and updates docs/scripts
make check      # fails if anything is out of sync (also run by `make test` and `make zip`)
```

Keybinding names are global inside mutter: two extensions that register the same
key name clash. That is why the schema keys start with `efg-`. Keep that prefix
for new keybindings.

## Running from the source tree

```sh
./install.sh --dev        # symlinks the extension and the app to this checkout
systemctl --user restart expose-for-gnome
journalctl --user -fu expose-for-gnome
```

On X11, reload the shell with Alt+F2 `r` after changing `extension/`. On Wayland,
test in a nested shell (`dbus-run-session gnome-shell --nested --wayland` on GNOME
45–48, `gnome-shell --devkit` on 49+) or use the headless integration test below.

## Tests

```sh
make test     # unit + repository tests (no display, no devices needed)
```

- `mapper/tests/test_hidpp.py` runs the HID++ code against a simulated Logitech
  device and receiver (`mapper/tests/fake_hidpp.py`): discovery, diverting, button
  events, re-diverting after a power cycle, learn mode.
- `tests/test_repo.py` checks that generated files are in sync, that no personal
  identifiers are present, and the metadata rules listed below.

Integration tests (need `gnome-shell`, `Xvfb`, PyGObject, GTK 4):

```sh
make zip
python3 tests/integration/shell_test.py --mode headless   # Wayland, no display needed
python3 tests/integration/shell_test.py --mode nested     # Wayland inside Xvfb (GNOME ≤ 48)
python3 tests/integration/shell_test.py --mode x11        # X11 session on Xvfb (GNOME ≤ 48)
python3 tests/integration/shell_test.py --also other.zip  # side by side with another extension
```

Each run starts a private shell with a private D-Bus session and home directory,
opens three test windows, drives App Exposé over D-Bus, and saves screenshots to
`test-output/`.

Mapper tests:

```sh
# X11 backend, events injected with XTEST (Xvfb is started for you)
python3 tests/integration/mapper_test.py --backend x11 --input xtest
# evdev backend with virtual uinput devices (needs /dev/uinput, python3-evdev, root or uaccess)
python3 tests/integration/mapper_test.py --backend evdev --input uinput --shell real
# X11 backend with virtual devices seen by a real Xorg + libinput (DISPLAY :5 running Xorg)
python3 tests/integration/mapper_test.py --backend x11 --input uinput --display :5 --shell real
```

`--shell fake` replaces GNOME Shell with a stub D-Bus service that records calls;
`--shell real` starts a headless shell with the extension. The virtual devices
(`tests/integration/virtual_devices.py`) include two mice with the same buttons
(per-device mappings), a gaming mouse that sends keystrokes, and a macro pad.
Containers and some CI hosts have no `uinput`; a small VM works.

Several GNOME versions in containers (podman):

```sh
tests/containers/run.sh                    # Fedora 39, 41, 42, 43, 44 and Debian trixie
tests/containers/run.sh fedora:45          # selected targets
```

A target whose shell version is not in `project.env` runs with version validation
off and is reported as a probe. Add a version to `SHELL_VERSIONS` only after it
passes.

## Releasing the extension on extensions.gnome.org

```sh
make test && make zip      # dist/expose-for-gnome@likewise4482.github.io.shell-extension.zip
```

Upload that zip at <https://extensions.gnome.org/upload/>. Only the extension is
uploaded; the companion app is distributed from the GitHub repository.

### Review rules this project follows

The [review guidelines](https://gjs.guide/extensions/review-guidelines/review-guidelines.html)
are the authority; these are the points that matter for this code base.

- **Name and trademarks.** The name is "Expose for GNOME", not "GNOME Expose":
  the GNOME Foundation's trademark guidance does not allow names that start with
  "GNOME" or imply an official GNOME product; "for GNOME" only says what it works
  with. "Exposé" is also another vendor's feature name, so the metadata description
  says what the extension does instead of using it. Keep artwork, screenshots and
  the description free of other brands' logos and names.
- **UUID** is `expose-for-gnome@likewise4482.github.io`: an account domain the
  project controls, never `gnome.org`.
- **metadata.json**: no `version` key (extensions.gnome.org sets it; ours is in
  `version-name`), no `session-modes` (user mode only), `shell-version` lists only
  stable releases we have tested, `url` points at the repository.
- **GSettings**: the schema ID and path are under `org.gnome.shell.extensions`; the
  XML is in the zip; compiled `gschemas.compiled` is not (GNOME 44+ compiles it).
  `.gitignore` excludes it so `--dev` installs don't leak it into commits.
- **Lifecycle**: nothing is created in the constructor. `enable()` creates the
  keybindings, D-Bus export and signal handlers; `disable()` removes all of them,
  including every pending `GLib.timeout_add` source and an open App Exposé view.
  The integration test disables the extension while App Exposé is open and checks
  that the D-Bus name is released.
- **No GTK in the shell, no Clutter/St/Meta/Shell in prefs.**
- **No scripts or binaries in the zip.** The extension never spawns processes. The
  companion app (Python) and the root helper live outside the extension; the
  helper is run with `pkexec` from the installed copy, not from the extension.
- **D-Bus**: the extension exports a small session-bus interface
  (`io.github.likewise4482.ExposeForGnome.Shell`) with window-management actions only
  (show/hide App Exposé, overview, app grid, workspace switch, last window). It
  does not read window contents or input. Mention it in the review notes.
- **Logging**: only errors are logged.
- **Licence**: GPL-2.0-or-later, same as GNOME Shell.
- **Authorship**: reviewers reject submissions that look machine-generated or that
  the submitter cannot explain. Whoever submits should have read and understood
  `extension/` and be ready to answer questions about it.

## Pull requests

- Run `make test`, and `shell_test.py --mode headless` if you touched `extension/`.
- Keep the root helper (`scripts/setup-permissions.sh`) small and readable; changes
  to it need a clear reason in the PR.
- New optional integrations (libratbag, OpenRazer, …) must stay optional: import
  them lazily and degrade quietly when they are missing.
