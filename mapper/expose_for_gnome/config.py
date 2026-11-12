"""Per-device button mappings.

    {
      "version": 2,
      "backend": "auto",
      "devices": {
        "*": {"name": "Any device", "bindings": {"BTN_SIDE": {"action": "app-expose"}}},
        "046d:b020:MX Vertical": {
          "name": "MX Vertical",
          "bindings": {"BTN_EXTRA": {"action": "mission-control"},
                       "HIDPP:0x00fd": {"action": "last-window"}}
        }
      }
    }

Device keys are "vendor:product:name" (see devices.device_key); "*" applies to
every device unless that device has its own binding for the same button.
"""
import copy
import json
import os
from pathlib import Path

from . import codes, ids

CONFIG_DIR = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / ids.SLUG
CONFIG_PATH = CONFIG_DIR / 'config.json'
ANY = '*'

ACTIONS = {
    'none': 'Default (unchanged)',
    'app-expose': 'App Exposé',
    'mission-control': 'Activities overview',
    'last-window': 'Switch to last window',
    'app-grid': 'Show applications',
    'workspace-prev': 'Previous workspace',
    'workspace-next': 'Next workspace',
    'keys': 'Keyboard shortcut…',
    'command': 'Run command…',
    'block': 'Do nothing (block)',
}

BACKENDS = {
    'auto': 'Automatic',
    'x11': 'X11 grab (X11 sessions, no setup needed)',
    'evdev': 'evdev + uinput (X11 and Wayland, one-time setup)',
}

DEFAULT_CONFIG = {
    'version': 2,
    'backend': 'auto',
    'devices': {ANY: {'name': 'Any device', 'bindings': {}}},
}

SUGGESTED = {
    'BTN_SIDE': {'action': 'app-expose'},
    'BTN_EXTRA': {'action': 'mission-control'},
}


def config_path():
    return CONFIG_PATH


def _valid_binding_key(key):
    return isinstance(key, str) and (codes.code(key) is not None or
                                     codes.hidpp_cid(key) is not None)


def normalize(data):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if not isinstance(data, dict):
        return cfg
    if data.get('backend') in BACKENDS:
        cfg['backend'] = data['backend']
    for dkey, dev in (data.get('devices') or {}).items():
        if not isinstance(dev, dict):
            continue
        entry = cfg['devices'].setdefault(dkey, {'name': dev.get('name') or dkey,
                                                 'bindings': {}})
        if isinstance(dev.get('name'), str):
            entry['name'] = dev['name']
        for bkey, b in (dev.get('bindings') or {}).items():
            try:
                ok = _valid_binding_key(bkey)
            except ValueError:
                ok = False
            if ok and isinstance(b, dict) and b.get('action') in ACTIONS:
                entry['bindings'][bkey] = dict(b)
    return cfg


def load(path=None):
    path = Path(path or CONFIG_PATH)
    try:
        with open(path) as f:
            return normalize(json.load(f))
    except FileNotFoundError:
        return copy.deepcopy(DEFAULT_CONFIG)
    except (OSError, ValueError) as e:
        print(f'{ids.SLUG}: ignoring unreadable config {path}: {e}', flush=True)
        return copy.deepcopy(DEFAULT_CONFIG)


def save(cfg, path=None):
    path = Path(path or CONFIG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.json.tmp')
    with open(tmp, 'w') as f:
        json.dump(cfg, f, indent=2)
        f.write('\n')
    os.replace(tmp, path)


def mtime(path=None):
    try:
        return Path(path or CONFIG_PATH).stat().st_mtime_ns
    except FileNotFoundError:
        return 0


def set_binding(cfg, dkey, dname, bkey, binding):
    dev = cfg['devices'].setdefault(dkey, {'name': dname or dkey, 'bindings': {}})
    if binding is None or binding.get('action', 'none') == 'none':
        dev['bindings'].pop(bkey, None)
        if dkey != ANY and not dev['bindings']:
            del cfg['devices'][dkey]
    else:
        dev['bindings'][bkey] = binding


def _active(bindings):
    return {k: b for k, b in bindings.items() if b.get('action', 'none') != 'none'}


class Mapping:
    """Resolved view of a config used by the backends."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.any = _active(cfg['devices'].get(ANY, {}).get('bindings', {}))
        self.per_device = {k: _active(d.get('bindings', {}))
                           for k, d in cfg['devices'].items() if k != ANY}

    def lookup(self, dkey, bkey):
        b = self.per_device.get(dkey, {}).get(bkey)
        return b if b is not None else self.any.get(bkey)

    def event_codes_for(self, dkey):
        """EV_KEY codes bound for this device (own bindings plus "*")."""
        keys = set(self.any) | set(self.per_device.get(dkey, {}))
        return {c for c in (codes.code(k) for k in keys) if c is not None}

    def bindings_for(self, dkey):
        """{evdev code: binding} for this device."""
        out = {}
        for source in (self.any, self.per_device.get(dkey, {})):
            for k, b in source.items():
                c = codes.code(k)
                if c is not None:
                    out[c] = b
        return out

    def hidpp_bindings(self):
        """{device key: {cid: binding}} for HID++ controls (never on "*")."""
        out = {}
        for dkey, bindings in self.per_device.items():
            cids = {codes.hidpp_cid(k): b for k, b in bindings.items()
                    if codes.hidpp_cid(k) is not None}
            if cids:
                out[dkey] = cids
        return out

    def has_event_bindings(self):
        return any(codes.code(k) is not None
                   for b in [self.any, *self.per_device.values()] for k in b)
