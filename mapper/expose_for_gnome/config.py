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
import re
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


def _split_key(dkey):
    parts = dkey.split(':', 2)
    return parts if len(parts) == 3 else None


def _name_tokens(name):
    return {t for t in re.findall(r'[a-z0-9]+', name.lower()) if len(t) > 1}


# Distinctive model tokens: Bluetooth vs Unifying can change product id / name suffix.
_MODEL_TOKENS = {'vertical', 'master', 'anywhere', 'lift', 'ergo', 'pebble', 'pop'}


class Mapping:
    """Resolved view of a config used by the backends."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.any = _active(cfg['devices'].get(ANY, {}).get('bindings', {}))
        self.per_device = {k: _active(d.get('bindings', {}))
                           for k, d in cfg['devices'].items() if k != ANY}

    def matching_keys(self, dkey):
        """Config device keys that should apply to a live device key.

        Exact match first; then same vendor:product; then same vendor with a
        shared model token (so a dock/Unifying reconnect keeps bindings).
        """
        if dkey in self.per_device:
            return [dkey]
        live = _split_key(dkey)
        if not live:
            return []
        vid, pid, name = live
        same_vp, same_model = [], []
        live_tokens = _name_tokens(name)
        for k in self.per_device:
            parts = _split_key(k)
            if not parts or parts[0] != vid:
                continue
            if parts[1] == pid:
                same_vp.append(k)
                continue
            if live_tokens & _MODEL_TOKENS & _name_tokens(parts[2]):
                same_model.append(k)
        return same_vp or same_model

    def lookup(self, dkey, bkey):
        for key in self.matching_keys(dkey):
            b = self.per_device.get(key, {}).get(bkey)
            if b is not None:
                return b
        return self.any.get(bkey)

    def event_codes_for(self, dkey):
        """EV_KEY codes bound for this device (own bindings plus "*")."""
        keys = set(self.any)
        for key in self.matching_keys(dkey):
            keys |= set(self.per_device.get(key, {}))
        return {c for c in (codes.code(k) for k in keys) if c is not None}

    def bindings_for(self, dkey):
        """{evdev code: binding} for this device."""
        out = {}
        sources = [self.any] + [self.per_device[k] for k in self.matching_keys(dkey)
                                if k in self.per_device]
        for source in sources:
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
