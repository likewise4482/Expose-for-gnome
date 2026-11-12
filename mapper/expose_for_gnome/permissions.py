"""What the one-time root setup grants, and whether it is in place.

The X11 backend needs nothing. The evdev backend (Wayland, or learn mode with
device identity on Wayland) needs read access to /dev/input/event* and write
access to /dev/uinput. Logitech firmware buttons need /dev/hidraw access.
scripts/setup-permissions.sh grants these with udev "uaccess" rules, i.e. only
to the user of the active local session, effective immediately.
"""
import os
import shutil
from pathlib import Path

from . import devices, hidpp, ids

RULES_DIR = Path('/etc/udev/rules.d')
EVDEV_RULE = RULES_DIR / f'{ids.UDEV_PREFIX}-input.rules'
HIDRAW_RULE = RULES_DIR / f'{ids.UDEV_PREFIX}-logitech-hidraw.rules'


def session_type():
    st = os.environ.get('XDG_SESSION_TYPE', '')
    if st:
        return st
    if os.environ.get('WAYLAND_DISPLAY'):
        return 'wayland'
    return 'x11' if os.environ.get('DISPLAY') else 'unknown'


def evdev_module():
    try:
        import evdev  # noqa: F401
        return True
    except ImportError:
        return False


def evdev_missing():
    """Human readable list of what the evdev backend lacks, or ''."""
    missing = []
    if not devices.can_read_events():
        missing.append('read access to /dev/input')
    if not devices.can_write_uinput():
        missing.append('write access to /dev/uinput')
    return ', '.join(missing)


def hidraw_status():
    nodes = list(hidpp.find_hidraw())
    ok = [n for n in nodes if os.access(n[0], os.R_OK | os.W_OK)]
    return {'nodes': len(nodes), 'accessible': len(ok),
            'names': sorted({n[1] for n in nodes})}


def check():
    hid = hidraw_status()
    evdev_needed = session_type() != 'x11'
    missing = evdev_missing()
    return {
        'session_type': session_type(),
        'evdev_module': evdev_module(),
        'evdev_missing': missing,
        'evdev_needed': evdev_needed,
        'evdev_rule': EVDEV_RULE.exists(),
        'hidraw_rule': HIDRAW_RULE.exists(),
        'hidraw': hid,
        'pkexec': bool(shutil.which('pkexec')),
        'needs_setup': bool((evdev_needed and missing) or hid['accessible'] < hid['nodes']),
    }


def setup_script():
    """Installed location first, then the source tree."""
    here = Path(__file__).resolve().parent
    for candidate in (here.parent.parent / 'scripts' / 'setup-permissions.sh',
                      here.parent / 'scripts' / 'setup-permissions.sh'):
        if candidate.exists():
            return candidate
    return None


def setup_command(evdev=True, hidraw=True, remove=False):
    script = setup_script()
    if not script:
        raise FileNotFoundError('setup-permissions.sh not found')
    args = ['pkexec', str(script)]
    if remove:
        args.append('--remove')
    else:
        if evdev:
            args.append('--input')
        if hidraw:
            args.append('--hidraw')
    return args
