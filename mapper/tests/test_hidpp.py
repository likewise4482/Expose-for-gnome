"""HID++ enumeration, diversion and button events against simulated devices."""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.dirname(__file__))

from expose_for_gnome import hidpp  # noqa: E402
from expose_for_gnome.learn import Learner  # noqa: E402
from fake_hidpp import GESTURE_MOUSE, MX_VERTICAL, FakeDevice, FakeHidraw  # noqa: E402


class Runner:
    def __init__(self):
        self.ran = []

    def run(self, binding):
        self.ran.append(binding['action'])


def wait(cond, timeout=4):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class HidppTestBase(unittest.TestCase):
    nodes = {}

    def setUp(self):
        self.fakes = {path: FakeHidraw(devs) for path, devs in self.nodes.items()}
        fds = {path: fake.host_fd() for path, fake in self.fakes.items()}
        names = {path: name for path, name in self.names.items()}

        class Conn(hidpp.HidrawConn):
            def __init__(self, path, sw_id=hidpp.SW_ID_DAEMON):
                self.path = path
                self.sw_id = sw_id
                self.fd = os.dup(fds[path])
                self.pending = []

        self._orig = (hidpp.HidrawConn, hidpp.find_hidraw)
        hidpp.HidrawConn = Conn
        hidpp.find_hidraw = lambda sysfs='/sys': iter(
            [(p, names[p], 5, 0xB020) for p in sorted(fds)])

    def tearDown(self):
        hidpp.HidrawConn, hidpp.find_hidraw = self._orig


class DirectDeviceTest(HidppTestBase):
    nodes = {'/dev/hidraw3': {0xFF: FakeDevice('MX Vertical', MX_VERTICAL)}}
    names = {'/dev/hidraw3': 'MX Vertical'}

    def test_discover_lists_controls(self):
        eps, errors = hidpp.discover()
        self.assertEqual(errors, [])
        self.assertEqual(len(eps), 1)
        ep = eps[0]
        self.assertEqual((ep.index, ep.name, ep.key), (0xFF, 'MX Vertical', '046d:hidpp:MX Vertical'))
        self.assertEqual(sorted(ep.controls), [c for c, _ in MX_VERTICAL])
        dpi = ep.controls[0x00FD]
        self.assertTrue(dpi['divertable'])
        self.assertFalse(dpi['os_visible'])
        self.assertEqual(dpi['name'], 'DPI switch')
        self.assertTrue(ep.controls[0x0053]['os_visible'])

    def test_divert_press_and_restore(self):
        mouse = self.fakes['/dev/hidraw3'].devices[0xFF]
        runner = Runner()
        src = hidpp.HidppSource(runner)
        key = '046d:hidpp:MX Vertical'
        src.configure({key: {0x00FD: {'action': 'last-window'}}})
        src.start()
        self.assertTrue(wait(lambda: mouse.reporting[0x00FD] == 1), 'not diverted')
        self.assertEqual(mouse.reporting[0x0053], 0)

        self.fakes['/dev/hidraw3'].press(0xFF, 0x00FD)
        time.sleep(0.15)
        self.assertEqual(runner.ran, [], 'must fire on release, not press')
        self.fakes['/dev/hidraw3'].release(0xFF)
        self.assertTrue(wait(lambda: runner.ran == ['last-window']))

        src.configure({})
        self.assertTrue(wait(lambda: mouse.reporting[0x00FD] == 0), 'not undiverted')
        src.configure({key: {0x00FD: {'action': 'app-expose'}}})
        self.assertTrue(wait(lambda: mouse.reporting[0x00FD] == 1))
        src.stop()
        self.assertEqual(mouse.reporting[0x00FD], 0, 'stop() must restore the firmware function')

    def test_reapplies_diversion_after_power_cycle(self):
        mouse = self.fakes['/dev/hidraw3'].devices[0xFF]
        src = hidpp.HidppSource(Runner())
        src.configure({'046d:hidpp:MX Vertical': {0x00FD: {'action': 'app-expose'}}})
        old = hidpp.RECHECK_SECONDS
        hidpp.RECHECK_SECONDS = 0.3
        try:
            src.start()
            self.assertTrue(wait(lambda: mouse.reporting[0x00FD] == 1))
            mouse.reporting[0x00FD] = 0      # the mouse forgets diversion when it sleeps
            self.assertTrue(wait(lambda: mouse.reporting[0x00FD] == 1), 'not re-diverted')
        finally:
            hidpp.RECHECK_SECONDS = old
            src.stop()

    def test_list_devices_via_thread(self):
        src = hidpp.HidppSource(Runner())
        src.start()
        try:
            out = src.list_devices()
            self.assertEqual([d['name'] for d in out['devices']], ['MX Vertical'])
            cids = [c['cid'] for c in out['devices'][0]['controls']]
            self.assertIn(0x00FD, cids)
        finally:
            src.stop()


class ReceiverTest(HidppTestBase):
    nodes = {'/dev/hidraw5': {1: FakeDevice('MX Master 3', GESTURE_MOUSE),
                              3: FakeDevice('MX Vertical', MX_VERTICAL)}}
    names = {'/dev/hidraw5': 'Logitech USB Receiver'}

    def test_discovers_paired_devices(self):
        eps, errors = hidpp.discover()
        self.assertEqual(errors, [])
        self.assertEqual([(e.index, e.name) for e in eps], [(1, 'MX Master 3'), (3, 'MX Vertical')])
        master = eps[0]
        self.assertFalse(master.controls[0x0090]['divertable'])
        self.assertTrue(master.controls[0x00D7]['virtual'])

    def test_events_routed_by_device_index(self):
        fake = self.fakes['/dev/hidraw5']
        runner = Runner()
        src = hidpp.HidppSource(runner)
        src.configure({'046d:hidpp:MX Master 3': {0x00C3: {'action': 'app-expose'}},
                       '046d:hidpp:MX Vertical': {0x00FD: {'action': 'mission-control'}}})
        src.start()
        try:
            self.assertTrue(wait(lambda: fake.devices[1].reporting[0x00C3] == 1))
            self.assertTrue(wait(lambda: fake.devices[3].reporting[0x00FD] == 1))
            fake.press(3, 0x00FD)
            fake.release(3)
            self.assertTrue(wait(lambda: runner.ran == ['mission-control']))
            fake.press(1, 0x00C3)
            fake.release(1)
            self.assertTrue(wait(lambda: runner.ran == ['mission-control', 'app-expose']))
            # A press reported for a CID that is not bound on that device does nothing.
            fake.press(1, 0x00FD)
            fake.release(1)
            time.sleep(0.2)
            self.assertEqual(len(runner.ran), 2)
        finally:
            src.stop()
        self.assertEqual(fake.devices[1].reporting[0x00C3], 0)
        self.assertEqual(fake.devices[3].reporting[0x00FD], 0)

    def test_learn_diverts_firmware_buttons_temporarily(self):
        fake = self.fakes['/dev/hidraw5']
        learner = Learner()
        results = []
        src = hidpp.HidppSource(Runner(), learner)
        src.start()
        try:
            learner.start(5, results.append)
            master = fake.devices[1]
            self.assertTrue(wait(lambda: master.reporting[0x00C3] == 1), 'gesture not diverted')
            self.assertEqual(master.reporting[0x0053], 0, 'OS-visible buttons stay normal')
            fake.press(1, 0x00C4)
            self.assertTrue(wait(lambda: results))
            self.assertEqual(results[0]['binding_key'], 'HIDPP:0x00c4')
            self.assertEqual(results[0]['device_key'], '046d:hidpp:MX Master 3')
            fake.release(1)
            self.assertTrue(wait(lambda: master.reporting[0x00C3] == 0),
                            'learn diversion not undone')
        finally:
            src.stop()


class PermissionTest(unittest.TestCase):
    def test_permission_error_reported(self):
        orig = (hidpp.HidrawConn, hidpp.find_hidraw)

        class Denied(hidpp.HidrawConn):
            def __init__(self, path, sw_id=0):
                raise PermissionError(13, 'Permission denied')
        hidpp.HidrawConn = Denied
        hidpp.find_hidraw = lambda sysfs='/sys': iter([('/dev/hidraw9', 'MX Vertical', 5, 1)])
        try:
            eps, errors = hidpp.discover()
        finally:
            hidpp.HidrawConn, hidpp.find_hidraw = orig
        self.assertEqual(eps, [])
        self.assertTrue(errors[0]['permission'])


if __name__ == '__main__':
    unittest.main()
