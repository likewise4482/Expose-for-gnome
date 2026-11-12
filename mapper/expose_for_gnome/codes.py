"""Linux input event code names (EV_KEY), without requiring python-evdev.

Binding keys in the config are EV_KEY code names such as "BTN_SIDE" or
"KEY_F13", "EV_KEY:0x2c0" for codes without a name, or "HIDPP:0x00fd" for
Logitech controls diverted over HID++.
"""
import re
from pathlib import Path

EV_KEY = 0x01
EV_REL = 0x02
EV_ABS = 0x03

BTN_MISC = 0x100
BTN_MOUSE = 0x110
BTN_LEFT = 0x110
BTN_RIGHT = 0x111
BTN_MIDDLE = 0x112
BTN_SIDE = 0x113
BTN_EXTRA = 0x114
BTN_FORWARD = 0x115
BTN_BACK = 0x116
BTN_TASK = 0x117
BTN_JOYSTICK = 0x120
BTN_DIGI = 0x140
BTN_TOOL_FINGER = 0x145
BTN_TRIGGER_HAPPY = 0x2c0
REL_X = 0x00
KEY_ESC = 1

MODIFIER_NAMES = {
    'KEY_LEFTCTRL': 'ctrl', 'KEY_RIGHTCTRL': 'ctrl', 'KEY_LEFTSHIFT': 'shift',
    'KEY_RIGHTSHIFT': 'shift', 'KEY_LEFTALT': 'alt', 'KEY_RIGHTALT': 'alt',
    'KEY_LEFTMETA': 'super', 'KEY_RIGHTMETA': 'super',
}

# Fallback when neither python-evdev nor the kernel header is available.
_BUILTIN = {
    'KEY_ESC': 1, 'KEY_1': 2, 'KEY_2': 3, 'KEY_3': 4, 'KEY_4': 5, 'KEY_5': 6, 'KEY_6': 7,
    'KEY_7': 8, 'KEY_8': 9, 'KEY_9': 10, 'KEY_0': 11, 'KEY_MINUS': 12, 'KEY_EQUAL': 13,
    'KEY_BACKSPACE': 14, 'KEY_TAB': 15, 'KEY_Q': 16, 'KEY_W': 17, 'KEY_E': 18, 'KEY_R': 19,
    'KEY_T': 20, 'KEY_Y': 21, 'KEY_U': 22, 'KEY_I': 23, 'KEY_O': 24, 'KEY_P': 25,
    'KEY_LEFTBRACE': 26, 'KEY_RIGHTBRACE': 27, 'KEY_ENTER': 28, 'KEY_LEFTCTRL': 29,
    'KEY_A': 30, 'KEY_S': 31, 'KEY_D': 32, 'KEY_F': 33, 'KEY_G': 34, 'KEY_H': 35,
    'KEY_J': 36, 'KEY_K': 37, 'KEY_L': 38, 'KEY_SEMICOLON': 39, 'KEY_APOSTROPHE': 40,
    'KEY_GRAVE': 41, 'KEY_LEFTSHIFT': 42, 'KEY_BACKSLASH': 43, 'KEY_Z': 44, 'KEY_X': 45,
    'KEY_C': 46, 'KEY_V': 47, 'KEY_B': 48, 'KEY_N': 49, 'KEY_M': 50, 'KEY_COMMA': 51,
    'KEY_DOT': 52, 'KEY_SLASH': 53, 'KEY_RIGHTSHIFT': 54, 'KEY_KPASTERISK': 55,
    'KEY_LEFTALT': 56, 'KEY_SPACE': 57, 'KEY_CAPSLOCK': 58, 'KEY_F1': 59, 'KEY_F2': 60,
    'KEY_F3': 61, 'KEY_F4': 62, 'KEY_F5': 63, 'KEY_F6': 64, 'KEY_F7': 65, 'KEY_F8': 66,
    'KEY_F9': 67, 'KEY_F10': 68, 'KEY_NUMLOCK': 69, 'KEY_SCROLLLOCK': 70, 'KEY_F11': 87,
    'KEY_F12': 88, 'KEY_RIGHTCTRL': 97, 'KEY_SYSRQ': 99, 'KEY_RIGHTALT': 100,
    'KEY_HOME': 102, 'KEY_UP': 103, 'KEY_PAGEUP': 104, 'KEY_LEFT': 105, 'KEY_RIGHT': 106,
    'KEY_END': 107, 'KEY_DOWN': 108, 'KEY_PAGEDOWN': 109, 'KEY_INSERT': 110,
    'KEY_DELETE': 111, 'KEY_MUTE': 113, 'KEY_VOLUMEDOWN': 114, 'KEY_VOLUMEUP': 115,
    'KEY_POWER': 116, 'KEY_PAUSE': 119, 'KEY_LEFTMETA': 125, 'KEY_RIGHTMETA': 126,
    'KEY_COMPOSE': 127, 'KEY_STOP': 128, 'KEY_AGAIN': 129, 'KEY_UNDO': 131, 'KEY_COPY': 133,
    'KEY_OPEN': 134, 'KEY_PASTE': 135, 'KEY_FIND': 136, 'KEY_CUT': 137, 'KEY_HELP': 138,
    'KEY_MENU': 139, 'KEY_CALC': 140, 'KEY_SLEEP': 142, 'KEY_WWW': 150, 'KEY_MAIL': 155,
    'KEY_BOOKMARKS': 156, 'KEY_COMPUTER': 157, 'KEY_BACK': 158, 'KEY_FORWARD': 159,
    'KEY_NEXTSONG': 163, 'KEY_PLAYPAUSE': 164, 'KEY_PREVIOUSSONG': 165, 'KEY_STOPCD': 166,
    'KEY_HOMEPAGE': 172, 'KEY_REFRESH': 173, 'KEY_F13': 183, 'KEY_F14': 184,
    'KEY_F15': 185, 'KEY_F16': 186, 'KEY_F17': 187, 'KEY_F18': 188, 'KEY_F19': 189,
    'KEY_F20': 190, 'KEY_F21': 191, 'KEY_F22': 192, 'KEY_F23': 193, 'KEY_F24': 194,
    'KEY_PROG1': 148, 'KEY_PROG2': 149, 'KEY_PROG3': 202, 'KEY_PROG4': 203,
    'KEY_DASHBOARD': 204, 'KEY_SEARCH': 217, 'KEY_BRIGHTNESSDOWN': 224,
    'KEY_BRIGHTNESSUP': 225, 'KEY_MEDIA': 226,
    'BTN_0': 0x100, 'BTN_1': 0x101, 'BTN_2': 0x102, 'BTN_3': 0x103, 'BTN_4': 0x104,
    'BTN_5': 0x105, 'BTN_6': 0x106, 'BTN_7': 0x107, 'BTN_8': 0x108, 'BTN_9': 0x109,
    'BTN_LEFT': BTN_LEFT, 'BTN_RIGHT': BTN_RIGHT, 'BTN_MIDDLE': BTN_MIDDLE,
    'BTN_SIDE': BTN_SIDE, 'BTN_EXTRA': BTN_EXTRA, 'BTN_FORWARD': BTN_FORWARD,
    'BTN_BACK': BTN_BACK, 'BTN_TASK': BTN_TASK, 'BTN_TOOL_FINGER': BTN_TOOL_FINGER,
    'KEY_MACRO1': 0x290, 'KEY_MACRO2': 0x291, 'KEY_MACRO3': 0x292, 'KEY_MACRO4': 0x293,
    'KEY_MACRO5': 0x294, 'KEY_MACRO6': 0x295, 'KEY_MACRO7': 0x296, 'KEY_MACRO8': 0x297,
    'BTN_TRIGGER_HAPPY1': 0x2c0, 'BTN_TRIGGER_HAPPY2': 0x2c1, 'BTN_TRIGGER_HAPPY3': 0x2c2,
    'BTN_TRIGGER_HAPPY4': 0x2c3,
}

# Names some codes share; prefer the more descriptive one for display.
_PREFERRED = {BTN_LEFT: 'BTN_LEFT', BTN_MISC: 'BTN_0', BTN_JOYSTICK: 'BTN_TRIGGER',
              BTN_DIGI: 'BTN_TOOL_PEN', 0x101: 'BTN_1', 0x130: 'BTN_SOUTH',
              0x131: 'BTN_EAST', 0x133: 'BTN_NORTH', 0x134: 'BTN_WEST'}

_LABELS = {
    'BTN_LEFT': 'Left button', 'BTN_RIGHT': 'Right button', 'BTN_MIDDLE': 'Middle / wheel click',
    'BTN_SIDE': 'Back (thumb) button', 'BTN_EXTRA': 'Forward (thumb) button',
    'BTN_FORWARD': 'Forward button', 'BTN_BACK': 'Back button', 'BTN_TASK': 'Task button',
}


def _load():
    try:
        from evdev import ecodes
        names = {k: v for k, v in ecodes.ecodes.items()
                 if k.startswith(('KEY_', 'BTN_')) and isinstance(v, int)}
        if names:
            return names
    except ImportError:
        pass
    header = Path('/usr/include/linux/input-event-codes.h')
    names = dict(_BUILTIN)
    if header.exists():
        pat = re.compile(r'#define\s+((?:KEY|BTN)_\w+)\s+(0x[0-9a-fA-F]+|\d+)\b')
        for m in pat.finditer(header.read_text()):
            names[m.group(1)] = int(m.group(2), 0)
    return names


NAMES = _load()
BY_CODE = {}
for _name, _code in sorted(NAMES.items()):
    if _name in ('KEY_MAX', 'KEY_CNT', 'BTN_MISC', 'BTN_MOUSE', 'BTN_JOYSTICK',
                 'BTN_GAMEPAD', 'BTN_DIGI', 'BTN_WHEEL', 'KEY_MIN_INTERESTING'):
        continue
    BY_CODE.setdefault(_code, _name)
BY_CODE.update({c: n for c, n in _PREFERRED.items() if n in NAMES})


def name(code):
    """EV_KEY code -> binding key, e.g. 275 -> 'BTN_SIDE'."""
    return BY_CODE.get(code) or f'EV_KEY:{code:#x}'


def code(key):
    """Binding key -> EV_KEY code, or None for HID++ / unknown keys."""
    if key.startswith('EV_KEY:'):
        return int(key[7:], 16)
    return NAMES.get(key)


def hidpp_key(cid):
    return f'HIDPP:{cid:#06x}'


def hidpp_cid(key):
    return int(key[6:], 16) if key.startswith('HIDPP:') else None


def label(key):
    """Human readable label for a binding key."""
    if key in _LABELS:
        return _LABELS[key]
    cid = hidpp_cid(key)
    if cid is not None:
        from .hidpp import control_name
        return control_name(cid)
    if key.startswith('KEY_'):
        return key[4:].replace('_', ' ').title().replace('Kp', 'Keypad ') + ' key'
    if key.startswith('BTN_'):
        return key[4:].replace('_', ' ').title() + ' button'
    return key


def is_button(code_):
    """BTN_* ranges; everything else with EV_KEY is a key (incl. KEY_MACRO*, KEY_OK...)."""
    return BTN_MISC <= code_ < 0x160 or BTN_TRIGGER_HAPPY <= code_ < 0x2e8


def is_modifier(code_):
    return name(code_) in MODIFIER_NAMES


# X11: xf86-input-libinput / evdev map BTN_SIDE.. to 8.. and keycodes are evdev + 8.
def x_button(code_):
    return {BTN_LEFT: 1, BTN_MIDDLE: 2, BTN_RIGHT: 3}.get(code_) or (
        8 + code_ - BTN_SIDE if code_ >= BTN_SIDE else None)


def from_x_button(button):
    if button in (1, 2, 3):
        return (BTN_LEFT, BTN_MIDDLE, BTN_RIGHT)[button - 1]
    if 4 <= button <= 7:
        return None  # scroll wheel
    return BTN_SIDE + button - 8


def x_keycode(code_):
    return code_ + 8


def from_x_keycode(keycode):
    return keycode - 8
