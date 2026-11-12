"""Input device discovery through sysfs (world readable: no permissions needed).

Opening the /dev/input/event* nodes is only needed to *read* events (learn
mode, evdev backend); listing devices and their advertised buttons is not.
"""
import os
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

from . import codes

SYSFS = Path(os.environ.get('EFG_SYSFS_ROOT', '/sys'))
WORD_BITS = struct.calcsize('L') * 8
OWN_DEVICE_MARKERS = ('expose-for-gnome', 'expose-mapper')

BUS_NAMES = {0x03: 'USB', 0x05: 'Bluetooth', 0x06: 'virtual', 0x11: 'PS/2', 0x18: 'I2C',
             0x19: 'host', 0x1C: 'SPI'}

LETTER_KEYS = {codes.NAMES.get(f'KEY_{c}') for c in 'QWERTYASDFZXCV'}
# Keys of a plain 105-key keyboard; anything beyond these is an "extra key".
STANDARD_KEYS = set(range(1, 89)) | set(range(96, 128))

# Marketing fluff stripped for the settings UI ("Logitech MX Vertical").
_NAME_FLUFF = (
    ' Advanced Ergonomic Mouse', ' Advanced Wireless Mouse', ' Wireless Mouse',
    ' Ergonomic Mouse', ' Performance Mouse', ' Laser Mouse',
)
_KIND_RANK = {'mouse': 0, 'trackball': 1, 'keyboard': 2}


def parse_bitmap(text):
    """sysfs capability bitmap ('1f 0 ffff...') -> set of set bit numbers."""
    bits = set()
    words = text.split()
    for i, word in enumerate(reversed(words)):
        value = int(word, 16)
        base = i * WORD_BITS
        while value:
            low = value & -value
            bits.add(base + low.bit_length() - 1)
            value ^= low
    return bits


def device_key(vendor, product, name):
    return f'{vendor:04x}:{product:04x}:{name}'


def display_name(name):
    """Short, human name for the settings UI."""
    if not name:
        return 'Unknown device'
    if re.search(r'\bMX Vertical\b', name, re.I):
        return 'Logitech MX Vertical'
    out = name
    for fluff in _NAME_FLUFF:
        if out.endswith(fluff):
            out = out[:-len(fluff)].rstrip()
            break
    return out or name


@dataclass
class InputDevice:
    path: str              # /dev/input/eventN
    name: str
    bus: int
    vendor: int
    product: int
    phys: str = ''
    uniq: str = ''
    keys: set = field(default_factory=set)
    rel: set = field(default_factory=set)
    abs: set = field(default_factory=set)

    @property
    def key(self):
        return device_key(self.vendor, self.product, self.name)

    @property
    def kind(self):
        if codes.BTN_TOOL_FINGER in self.keys or (self.abs and codes.BTN_LEFT in self.keys
                                                   and not self.rel):
            return 'touchpad'
        if codes.BTN_LEFT in self.keys and codes.REL_X in self.rel:
            return 'trackball' if 'trackball' in self.name.lower() else 'mouse'
        if self.keys & LETTER_KEYS:
            return 'keyboard'
        if self.keys and any(codes.is_button(k) for k in self.keys) and \
                not any(codes.BTN_JOYSTICK <= k < codes.BTN_DIGI for k in self.keys):
            return 'buttons'
        if self.extra_keys:
            return 'keys'
        return 'other'

    @property
    def is_own(self):
        return any(m in self.name for m in OWN_DEVICE_MARKERS)

    @property
    def mouse_buttons(self):
        return sorted(k for k in self.keys if codes.BTN_MOUSE <= k < codes.BTN_JOYSTICK)

    @property
    def extra_keys(self):
        """Keys/buttons beyond a standard keyboard and the left/right/middle buttons."""
        return sorted(k for k in self.keys
                      if k not in STANDARD_KEYS and k not in
                      (codes.BTN_LEFT, codes.BTN_RIGHT, codes.BTN_MIDDLE)
                      and not (codes.BTN_DIGI <= k < 0x160))

    @property
    def bus_name(self):
        return BUS_NAMES.get(self.bus, f'bus {self.bus:#x}')

    @property
    def relevant(self):
        """Worth showing by default: mice, trackballs and keyboards only."""
        return not self.is_own and self.kind in ('mouse', 'trackball', 'keyboard')

    @property
    def display(self):
        return display_name(self.name)

    def to_dict(self):
        return {
            'path': self.path, 'key': self.key, 'name': self.name, 'display': self.display,
            'kind': self.kind, 'bus': self.bus_name, 'vendor': f'{self.vendor:04x}',
            'product': f'{self.product:04x}', 'phys': self.phys, 'uniq': self.uniq,
            'mouse_buttons': [codes.name(c) for c in self.mouse_buttons],
            'extra_keys': [codes.name(c) for c in self.extra_keys],
            'key_count': len(self.keys),
        }


@dataclass
class PhysicalDevice:
    """One card in the UI: merged event nodes that share vendor, product and name."""

    vendor: int
    product: int
    name: str
    kind: str
    nodes: list
    keys: set = field(default_factory=set)
    buses: set = field(default_factory=set)
    uniq: str = ''

    @property
    def key(self):
        return device_key(self.vendor, self.product, self.name)

    @property
    def display(self):
        return display_name(self.name)

    @property
    def bus_name(self):
        names = sorted({BUS_NAMES.get(b, f'bus {b:#x}') for b in self.buses})
        return ' / '.join(names) if names else ''

    @property
    def mouse_buttons(self):
        return sorted(k for k in self.keys if codes.BTN_MOUSE <= k < codes.BTN_JOYSTICK)

    @property
    def extra_keys(self):
        return sorted(k for k in self.keys
                      if k not in STANDARD_KEYS and k not in
                      (codes.BTN_LEFT, codes.BTN_RIGHT, codes.BTN_MIDDLE)
                      and not (codes.BTN_DIGI <= k < 0x160))

    @property
    def identity(self):
        """Stable id for matching across reconnects (uniq when real, else key)."""
        u = (self.uniq or '').strip()
        if u and u not in ('0', '00000000', '00:00:00:00:00:00'):
            return f'{self.vendor:04x}:uniq:{u.lower()}'
        return self.key

    def matches_hidpp(self, hid_name):
        if self.vendor != 0x046d or not hid_name:
            return False
        a, b = self.name.lower(), hid_name.lower()
        return a == b or a.endswith(b) or b in a or display_name(self.name).lower() == \
            display_name(hid_name).lower()


def _read(path, default=''):
    try:
        return path.read_text().strip()
    except OSError:
        return default


def from_sysfs(event_dir):
    dev = Path(event_dir) / 'device'
    if not (dev / 'name').exists():
        return None

    def hexval(p):
        try:
            return int(_read(dev / 'id' / p, '0'), 16)
        except ValueError:
            return 0

    return InputDevice(
        path='/dev/input/' + Path(event_dir).name,
        name=_read(dev / 'name'),
        bus=hexval('bustype'), vendor=hexval('vendor'), product=hexval('product'),
        phys=_read(dev / 'phys'), uniq=_read(dev / 'uniq'),
        keys=parse_bitmap(_read(dev / 'capabilities' / 'key', '0')),
        rel=parse_bitmap(_read(dev / 'capabilities' / 'rel', '0')),
        abs=parse_bitmap(_read(dev / 'capabilities' / 'abs', '0')),
    )


def list_devices(include_all=False):
    base = SYSFS / 'class' / 'input'
    found = []
    for entry in sorted(base.glob('event*'), key=lambda p: int(p.name[5:] or 0)):
        d = from_sysfs(entry)
        if d and (include_all or d.relevant):
            found.append(d)
    return found


def list_physical_devices(include_all=False):
    """Mice/trackballs/keyboards, one entry per vendor+product+name (merged nodes)."""
    nodes = list_devices(include_all=include_all)
    if include_all:
        nodes = [d for d in nodes if not d.is_own and
                 d.kind in ('mouse', 'trackball', 'keyboard')]
    groups = {}
    for d in nodes:
        groups.setdefault((d.vendor, d.product, d.name), []).append(d)
    out = []
    for (vendor, product, name), members in groups.items():
        members = sorted(members, key=lambda m: m.path)
        kind = min((_KIND_RANK.get(m.kind, 9), m.kind) for m in members)[1]
        keys = set()
        buses = set()
        uniq = ''
        for m in members:
            keys |= m.keys
            buses.add(m.bus)
            if not uniq and m.uniq and m.uniq not in ('0', '00000000'):
                uniq = m.uniq
        out.append(PhysicalDevice(vendor=vendor, product=product, name=name, kind=kind,
                                  nodes=members, keys=keys, buses=buses, uniq=uniq))
    out.sort(key=lambda p: (_KIND_RANK.get(p.kind, 9), p.display.lower(), p.key))
    return out


def by_path(path):
    return from_sysfs(SYSFS / 'class' / 'input' / os.path.basename(path))


def can_read_events():
    devs = list(Path('/dev/input').glob('event*')) if Path('/dev/input').exists() else []
    return bool(devs) and all(os.access(p, os.R_OK) for p in devs)


def can_write_uinput():
    return os.access('/dev/uinput', os.W_OK)
