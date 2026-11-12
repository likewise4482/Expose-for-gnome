"""Config, device discovery (fake sysfs), code mapping and learn mode."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from expose_for_gnome import codes, config, devices  # noqa: E402
from expose_for_gnome.learn import Learner  # noqa: E402


def bitmap(bits):
    """Inverse of devices.parse_bitmap, in sysfs format."""
    words = [0] * (max(bits) // devices.WORD_BITS + 1)
    for b in bits:
        words[b // devices.WORD_BITS] |= 1 << (b % devices.WORD_BITS)
    return ' '.join(f'{w:x}' for w in reversed(words))


def fake_input(root, n, name, vendor, product, keys, rel=(), abs_=(), bus=3):
    dev = Path(root) / 'class' / 'input' / f'event{n}' / 'device'
    (dev / 'id').mkdir(parents=True)
    (dev / 'capabilities').mkdir()
    (dev / 'name').write_text(name + '\n')
    for k, v in (('bustype', bus), ('vendor', vendor), ('product', product), ('version', 1)):
        (dev / 'id' / k).write_text(f'{v:04x}\n')
    (dev / 'capabilities' / 'key').write_text(bitmap(keys or [0]) + '\n')
    (dev / 'capabilities' / 'rel').write_text(bitmap(rel or [0]) + '\n')
    (dev / 'capabilities' / 'abs').write_text(bitmap(abs_ or [0]) + '\n')


MOUSE_BTNS = [0x110, 0x111, 0x112, 0x113, 0x114, 0x115, 0x116, 0x117]
KEYBOARD = list(range(1, 89)) + list(range(96, 128))


class DevicesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        fake_input(root, 0, 'Power Button', 0, 1, [116], bus=0x19)
        fake_input(root, 3, 'Logitech MX Vertical', 0x046d, 0x4082, MOUSE_BTNS, rel=[0, 1, 8])
        fake_input(root, 4, 'Razer Naga Keyboard', 0x1532, 0x0040,
                   KEYBOARD + [183, 184, 185])
        fake_input(root, 5, 'SynPS/2 Touchpad', 2, 7, [0x110, 0x145], abs_=[0, 1], bus=0x11)
        fake_input(root, 6, 'Gaming Mouse (expose-for-gnome)', 1, 2, MOUSE_BTNS, rel=[0, 1])
        fake_input(root, 7, 'Macro Pad', 0x1234, 0x5678, [0x290, 0x291, 0x2c0])
        self.orig = devices.SYSFS
        devices.SYSFS = Path(root)

    def tearDown(self):
        devices.SYSFS = self.orig
        self.tmp.cleanup()

    def test_bitmap_roundtrip(self):
        bits = {0, 1, 63, 64, 275, 704}
        self.assertEqual(devices.parse_bitmap(bitmap(bits)), bits)

    def test_list_and_classify(self):
        devs = {d.name: d for d in devices.list_devices()}
        self.assertEqual(set(devs), {'Logitech MX Vertical', 'Razer Naga Keyboard', 'Macro Pad'})
        mouse = devs['Logitech MX Vertical']
        self.assertEqual(mouse.kind, 'mouse')
        self.assertEqual(mouse.key, '046d:4082:Logitech MX Vertical')
        self.assertEqual([codes.name(c) for c in mouse.mouse_buttons],
                         ['BTN_LEFT', 'BTN_RIGHT', 'BTN_MIDDLE', 'BTN_SIDE', 'BTN_EXTRA',
                          'BTN_FORWARD', 'BTN_BACK', 'BTN_TASK'])
        kbd = devs['Razer Naga Keyboard']
        self.assertEqual(kbd.kind, 'keyboard')
        self.assertEqual([codes.name(c) for c in kbd.extra_keys], ['KEY_F13', 'KEY_F14', 'KEY_F15'])
        self.assertEqual(devs['Macro Pad'].kind, 'buttons')
        all_devs = {d.name: d for d in devices.list_devices(include_all=True)}
        self.assertEqual(all_devs['SynPS/2 Touchpad'].kind, 'touchpad')
        self.assertTrue(all_devs['Gaming Mouse (expose-for-gnome)'].is_own)
        self.assertEqual(devices.by_path('/dev/input/event3').name, 'Logitech MX Vertical')


class ConfigTest(unittest.TestCase):
    def test_roundtrip_and_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'c.json'
            cfg = config.load(path)
            self.assertEqual(cfg['devices'], {'*': {'name': 'Any device', 'bindings': {}}})
            config.set_binding(cfg, '*', 'Any device', 'BTN_SIDE', {'action': 'app-expose'})
            config.set_binding(cfg, '046d:4082:M', 'M', 'KEY_F13', {'action': 'keys',
                                                                   'keys': 'ctrl+w'})
            config.set_binding(cfg, '046d:hidpp:M', 'M', 'HIDPP:0x00fd', {'action': 'last-window'})
            cfg['devices']['x'] = {'bindings': {'NOT_A_KEY': {'action': 'app-expose'},
                                                'BTN_EXTRA': {'action': 'bogus'}}}
            config.save(cfg, path)
            again = config.load(path)
            self.assertEqual(again['devices']['*']['bindings'],
                             {'BTN_SIDE': {'action': 'app-expose'}})
            self.assertEqual(again['devices']['x']['bindings'], {})
            config.set_binding(again, '046d:4082:M', 'M', 'KEY_F13', None)
            self.assertNotIn('046d:4082:M', again['devices'])

    def test_mapping_precedence(self):
        cfg = config.normalize({'devices': {
            '*': {'bindings': {'BTN_SIDE': {'action': 'app-expose'},
                               'BTN_EXTRA': {'action': 'mission-control'}}},
            'a': {'bindings': {'BTN_SIDE': {'action': 'last-window'},
                               'HIDPP:0x00fd': {'action': 'app-grid'},
                               'EV_KEY:0x2c5': {'action': 'block'}}},
        }})
        m = config.Mapping(cfg)
        self.assertEqual(m.lookup('a', 'BTN_SIDE')['action'], 'last-window')
        self.assertEqual(m.lookup('a', 'BTN_EXTRA')['action'], 'mission-control')
        self.assertEqual(m.lookup('b', 'BTN_SIDE')['action'], 'app-expose')
        self.assertIsNone(m.lookup('b', 'BTN_MIDDLE'))
        self.assertEqual(m.event_codes_for('a'), {codes.BTN_SIDE, codes.BTN_EXTRA, 0x2c5})
        self.assertEqual(m.event_codes_for('b'), {codes.BTN_SIDE, codes.BTN_EXTRA})
        self.assertEqual(m.hidpp_bindings(), {'a': {0xFD: {'action': 'app-grid'}}})


class CodesTest(unittest.TestCase):
    def test_names(self):
        self.assertEqual(codes.name(275), 'BTN_SIDE')
        self.assertEqual(codes.code('KEY_F13'), 183)
        self.assertEqual(codes.name(0x2ff), 'EV_KEY:0x2ff')
        self.assertEqual(codes.code('EV_KEY:0x2ff'), 0x2ff)
        self.assertEqual(codes.label('BTN_SIDE'), 'Back (thumb) button')
        self.assertEqual(codes.label('HIDPP:0x00fd'), 'DPI switch')

    def test_x11_mapping(self):
        for code in (codes.BTN_LEFT, codes.BTN_MIDDLE, codes.BTN_RIGHT, codes.BTN_SIDE,
                     codes.BTN_EXTRA, codes.BTN_TASK, 0x118):
            self.assertEqual(codes.from_x_button(codes.x_button(code)), code)
        self.assertEqual(codes.x_button(codes.BTN_SIDE), 8)
        self.assertEqual(codes.x_button(codes.BTN_EXTRA), 9)
        self.assertIsNone(codes.from_x_button(4))
        self.assertEqual(codes.x_keycode(183), 191)


class LearnTest(unittest.TestCase):
    def test_learn_flow(self):
        out = []
        learner = Learner()
        self.assertFalse(learner.offer_event('k', 'n', 183, 'evdev'))
        learner.start(5, out.append)
        self.assertFalse(learner.offer_event('k', 'n', 29, 'evdev'), 'modifiers are skipped')
        self.assertTrue(learner.offer_event('k', 'Razer', 183, 'evdev'))
        self.assertEqual(out[0]['binding_key'], 'KEY_F13')
        self.assertFalse(learner.active)

    def test_escape_and_timeout(self):
        out = []
        learner = Learner()
        learner.start(5, out.append)
        learner.offer_event('k', 'n', codes.KEY_ESC, 'x11')
        learner.start(0.01, out.append)
        import time
        time.sleep(0.05)
        self.assertFalse(learner.active)
        self.assertEqual(out, [{'cancelled': True}, {'timeout': True}])


if __name__ == '__main__':
    unittest.main()
