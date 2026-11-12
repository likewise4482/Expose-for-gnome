"""Virtual input devices (python-evdev / uinput) that mimic real hardware."""
import time
from pathlib import Path

from evdev import AbsInfo, UInput, ecodes as e  # noqa: F401

NAMES = {
    'mouse_a': 'EFG Test Mouse A',
    'mouse_b': 'EFG Test Mouse B',
    'mouse_plain': 'EFG Plain Mouse',
    'gaming_kbd': 'EFG Gaming Mouse Keyboard',
    'macro_pad': 'EFG Macro Pad',
}
IDS = {'mouse_a': (0x1234, 0x0a01), 'mouse_b': (0x1234, 0x0b02), 'mouse_plain': (0x1234, 0x0c03),
       'gaming_kbd': (0x1532, 0x00aa), 'macro_pad': (0x1234, 0x0d04)}
MOUSE_REL = [e.REL_X, e.REL_Y, e.REL_WHEEL, e.REL_HWHEEL]


def caps(name):
    if name == 'mouse_a':
        return {e.EV_KEY: [e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE, e.BTN_SIDE, e.BTN_EXTRA,
                           e.BTN_FORWARD, e.BTN_BACK, e.BTN_TASK], e.EV_REL: MOUSE_REL}
    if name in ('mouse_b', 'mouse_plain'):
        return {e.EV_KEY: [e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE, e.BTN_SIDE, e.BTN_EXTRA,
                           e.BTN_FORWARD, e.BTN_BACK, e.BTN_TASK], e.EV_REL: MOUSE_REL}
    if name == 'gaming_kbd':
        # Gaming mice expose a full keyboard interface even if they only send a few keys.
        return {e.EV_KEY: list(range(1, 249))}
    if name == 'macro_pad':
        return {e.EV_KEY: [e.KEY_MACRO1, e.KEY_MACRO2, e.KEY_MACRO3, e.KEY_MACRO4,
                           e.BTN_TRIGGER_HAPPY1]}
    raise KeyError(name)


def create(name):
    vendor, product = IDS[name]
    return UInput(caps(name), name=NAMES[name], vendor=vendor, product=product, bustype=e.BUS_USB)


def create_all():
    return {n: create(n) for n in NAMES}


def device_key(ui):
    """The mapper's device key for a UInput device (via sysfs, like the daemon)."""
    for _ in range(40):
        for ev in Path('/sys/class/input').glob('event*'):
            try:
                if (ev / 'device' / 'name').read_text().strip() == ui.name:
                    vid = int((ev / 'device/id/vendor').read_text(), 16)
                    pid = int((ev / 'device/id/product').read_text(), 16)
                    return f'{vid:04x}:{pid:04x}:{ui.name}'
            except OSError:
                continue
        time.sleep(0.05)
    return None


def click(ui, code):
    ui.write(e.EV_KEY, code, 1)
    ui.syn()
    time.sleep(0.03)
    ui.write(e.EV_KEY, code, 0)
    ui.syn()


tap = click


def move(ui, dx, dy):
    ui.write(e.EV_REL, e.REL_X, dx)
    ui.write(e.EV_REL, e.REL_Y, dy)
    ui.syn()
