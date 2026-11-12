"""Logitech HID++ 2.0: enumerate reprogrammable controls (feature 0x1B04,
REPROG_CONTROLS_V4) and divert firmware-handled buttons (e.g. a DPI switch or
gesture button that sends nothing to the OS) so the mapper can bind them.

Works with directly connected devices (Bluetooth, USB cable; device index
0xFF) and with devices paired to a Unifying/Bolt receiver (index 1..6).
Needs read/write access to the device's /dev/hidraw node (one-time udev rule,
see scripts/setup-permissions.sh). Diversion is undone on exit and is reset by
the device itself when it power-cycles, so it is re-checked periodically.
"""
import glob
import os
import select
import threading
import time

from . import ids

REPORT_SHORT = 0x10
REPORT_LONG = 0x11
SHORT_LEN = 7
LONG_LEN = 20
DIRECT = 0xFF
RECEIVER_SLOTS = range(1, 7)
SW_ID_DAEMON = 0x0B
SW_ID_TOOL = 0x0C
LOGITECH = 0x046D

FEATURE_ROOT = 0x0000
FEATURE_DEVICE_NAME = 0x0005
FEATURE_REPROG_V4 = 0x1B04

FN_GET_COUNT = 0
FN_GET_CID_INFO = 1
FN_GET_CID_REPORTING = 2
FN_SET_CID_REPORTING = 3

FLAG_MOUSE = 0x01
FLAG_FKEY = 0x02
FLAG_HOTKEY = 0x04
FLAG_REPROG = 0x10
FLAG_DIVERTABLE = 0x20
FLAG_PERSIST = 0x40
FLAG_VIRTUAL = 0x80
REPORT_DIVERT = 0x01
REPORT_DIVERT_VALID = 0x02

RECHECK_SECONDS = 30
RESCAN_SECONDS = 10

CONTROL_NAMES = {
    0x0050: 'Left button', 0x0051: 'Right button', 0x0052: 'Middle button',
    0x0053: 'Back button', 0x0056: 'Forward button', 0x005B: 'Wheel tilt left',
    0x005D: 'Wheel tilt right', 0x00C3: 'Gesture button', 0x00C4: 'Smart Shift (wheel mode)',
    0x00FD: 'DPI switch',
}
# Controls that already produce ordinary button events: never auto-diverted for learn mode.
OS_VISIBLE = {0x0050, 0x0051, 0x0052, 0x0053, 0x0056, 0x005B, 0x005D}


def control_name(cid):
    return CONTROL_NAMES.get(cid, f'Control {cid:#06x}')


class HidppError(Exception):
    def __init__(self, code, msg=None):
        super().__init__(msg or f'HID++ error {code:#04x}')
        self.code = code


def find_hidraw(sysfs='/sys'):
    """Yield (/dev/hidrawN, HID name, bus, product id) for Logitech hidraw nodes."""
    for sys_path in sorted(glob.glob(os.path.join(sysfs, 'class/hidraw/hidraw*'))):
        try:
            with open(os.path.join(sys_path, 'device', 'uevent')) as f:
                info = dict(line.strip().split('=', 1) for line in f if '=' in line)
            bus, vendor, product = (int(x, 16) for x in info.get('HID_ID', '').split(':'))
        except (OSError, ValueError):
            continue
        if vendor == LOGITECH:
            yield '/dev/' + os.path.basename(sys_path), info.get('HID_NAME', ''), bus, product


class HidrawConn:
    """One open /dev/hidraw node; requests are matched to replies by index/feature/sw id."""

    def __init__(self, path, sw_id=SW_ID_DAEMON):
        self.path = path
        self.sw_id = sw_id
        self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        self.pending = []

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def read(self, timeout):
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return None
        return os.read(self.fd, 64)

    def request(self, index, feature_index, function, params=b'', timeout=2.0):
        func_sw = (function << 4) | self.sw_id
        msg = bytes([REPORT_LONG, index, feature_index, func_sw]) + bytes(params)
        os.write(self.fd, msg.ljust(LONG_LEN, b'\0'))
        deadline = time.monotonic() + timeout
        while (left := deadline - time.monotonic()) > 0:
            data = self.read(left)
            if not data or data[0] not in (REPORT_SHORT, REPORT_LONG) or len(data) < 6:
                continue
            if data[1] == index:
                if data[2] == 0xFF and data[3] == feature_index and data[4] == func_sw:
                    raise HidppError(data[5])
                if data[2] == 0x8F and data[3] == feature_index and data[4] == func_sw:
                    raise HidppError(data[5], f'HID++ 1.0 error {data[5]:#04x}')
                if data[2] == feature_index and data[3] == func_sw:
                    return data[4:]
            self.pending.append(data)
        raise TimeoutError(f'no HID++ reply from {self.path} index {index:#x} (asleep?)')


class Endpoint:
    """A HID++ 2.0 device reachable through a connection at a device index."""

    def __init__(self, conn, index, hid_name=''):
        self.conn = conn
        self.index = index
        self.name = hid_name
        self.features = {}
        self.controls = {}   # cid -> dict
        self.diverted = set()
        self.pressed = set()
        self.last_check = 0

    @property
    def key(self):
        return f'{LOGITECH:04x}:hidpp:{self.name}'

    def request(self, feature_id, function, params=b'', timeout=2.0):
        return self.conn.request(self.index, self.feature_index(feature_id), function,
                                 params, timeout)

    def feature_index(self, feature_id):
        if feature_id == FEATURE_ROOT:
            return 0
        if feature_id not in self.features:
            r = self.conn.request(self.index, 0, 0, bytes([feature_id >> 8, feature_id & 0xFF]))
            self.features[feature_id] = r[0]
        if not self.features[feature_id]:
            raise HidppError(0x02, f'feature {feature_id:#06x} not supported')
        return self.features[feature_id]

    def ping(self, timeout=1.0):
        """Protocol version (major, minor), or raise for HID++ 1.0 / no device."""
        r = self.conn.request(self.index, 0, 1, bytes([0, 0, 0x5A]), timeout)
        return r[0], r[1]

    def read_name(self):
        try:
            length = self.request(FEATURE_DEVICE_NAME, 0)[0]
            name = b''
            while len(name) < length:
                chunk = self.request(FEATURE_DEVICE_NAME, 1, bytes([len(name)]))
                name += bytes(chunk[:length - len(name)])
            self.name = name.rstrip(b'\0').decode('utf-8', 'replace') or self.name
        except (HidppError, TimeoutError):
            pass
        return self.name

    def read_controls(self):
        count = self.request(FEATURE_REPROG_V4, FN_GET_COUNT)[0]
        controls = {}
        for i in range(count):
            r = self.request(FEATURE_REPROG_V4, FN_GET_CID_INFO, bytes([i]))
            cid = (r[0] << 8) | r[1]
            flags = r[4]
            controls[cid] = {
                'cid': cid, 'name': control_name(cid), 'tid': (r[2] << 8) | r[3],
                'flags': flags, 'divertable': bool(flags & FLAG_DIVERTABLE),
                'mouse': bool(flags & FLAG_MOUSE), 'virtual': bool(flags & FLAG_VIRTUAL),
                'os_visible': cid in OS_VISIBLE,
            }
        self.controls = controls
        return controls

    def set_divert(self, cid, on):
        flags = REPORT_DIVERT_VALID | (REPORT_DIVERT if on else 0)
        self.request(FEATURE_REPROG_V4, FN_SET_CID_REPORTING,
                     bytes([cid >> 8, cid & 0xFF, flags, 0, 0]))

    def is_diverted(self, cid):
        r = self.request(FEATURE_REPROG_V4, FN_GET_CID_REPORTING, bytes([cid >> 8, cid & 0xFF]))
        return bool(r[2] & REPORT_DIVERT)

    def to_dict(self):
        return {'key': self.key, 'name': self.name, 'path': self.conn.path,
                'index': self.index, 'controls': sorted(self.controls.values(),
                                                        key=lambda c: c['cid'])}


def probe(conn, hid_name='', timeout=0.5):
    """Endpoints behind one hidraw node: the device itself, or a receiver's paired devices."""
    direct = Endpoint(conn, DIRECT, hid_name)
    try:
        if direct.ping(timeout)[0] >= 2:
            return [direct]
    except OSError:
        # Non-HID++ Logitech nodes (e.g. a StreamCam) reject the write with EPIPE/EINVAL.
        return []
    except (HidppError, TimeoutError):
        pass
    found = []
    for slot in RECEIVER_SLOTS:
        ep = Endpoint(conn, slot, f'{hid_name} #{slot}')
        try:
            if ep.ping(timeout)[0] >= 2:
                found.append(ep)
        except OSError:
            return found
        except (HidppError, TimeoutError):
            continue
    return found


def discover(sw_id=SW_ID_TOOL, sysfs='/sys', with_controls=True):
    """One-shot enumeration (for tools/tests). Returns (endpoints, errors)."""
    endpoints, errors, seen = [], [], set()
    for path, hid_name, _bus, _product in find_hidraw(sysfs):
        try:
            conn = HidrawConn(path, sw_id)
        except OSError as e:
            errors.append({'path': path, 'name': hid_name, 'error': e.strerror or str(e),
                           'permission': isinstance(e, PermissionError)})
            continue
        try:
            eps = probe(conn, hid_name)
            kept = []
            for ep in eps:
                ep.read_name()
                if ep.key in seen:
                    continue
                seen.add(ep.key)
                if with_controls:
                    ep.read_controls()
                kept.append(ep)
                endpoints.append(ep)
        except (OSError, HidppError, TimeoutError) as e:
            errors.append({'path': path, 'name': hid_name, 'error': str(e)})
            conn.close()
            continue
        if not kept:
            conn.close()
    return endpoints, errors


class HidppSource(threading.Thread):
    """Keeps configured controls diverted on all matching devices and runs their bindings."""

    def __init__(self, runner, learner=None, sysfs='/sys'):
        super().__init__(daemon=True, name='hidpp')
        self.runner = runner
        self.learner = learner
        self.sysfs = sysfs
        self.lock = threading.Lock()
        self.bindings = {}         # device key -> {cid: binding}
        self.generation = 0
        self.conns = {}            # path -> HidrawConn
        self.endpoints = []
        self.errors = []
        self.last_scan = 0
        self._requests = []        # callables run on this thread
        self._stopping = threading.Event()
        self._warned = set()

    # --- called from other threads ---------------------------------------
    def configure(self, bindings):
        with self.lock:
            self.bindings = {k: dict(v) for k, v in bindings.items()}
            self.generation += 1
        for dkey, cids in bindings.items():
            for cid, b in cids.items():
                print(f'{ids.SLUG}: HID++ {dkey} {control_name(cid)} -> {b.get("action")}',
                      flush=True)

    def call(self, func, timeout=15):
        """Run func() on the HID++ thread and return its result."""
        done = threading.Event()
        box = {}

        def wrapper():
            try:
                box['result'] = func()
            except Exception as e:  # noqa: BLE001 - reported to the caller
                box['error'] = e
            done.set()
        with self.lock:
            self._requests.append(wrapper)
        if not done.wait(timeout):
            raise TimeoutError('HID++ thread busy')
        if 'error' in box:
            raise box['error']
        return box['result']

    def list_devices(self):
        def scan():
            self._scan(force=True)
            return {'devices': [ep.to_dict() for ep in self.endpoints], 'errors': self.errors}
        return self.call(scan)

    def stop(self):
        """Undo diversion so the buttons get their firmware function back."""
        self._stopping.set()
        if self.is_alive():
            self.join(timeout=5)

    # --- HID++ thread ----------------------------------------------------
    def _warn_once(self, key, text):
        if key not in self._warned:
            self._warned.add(key)
            print(f'{ids.SLUG}: {text}', flush=True)

    def _drop(self, path):
        conn = self.conns.pop(path, None)
        self.endpoints = [ep for ep in self.endpoints if ep.conn is not conn]
        if conn:
            conn.close()

    def _scan(self, force=False):
        self.last_scan = time.monotonic()
        nodes = list(find_hidraw(self.sysfs))
        present = {n[0] for n in nodes}
        for path in list(self.conns):
            if path not in present:
                self._drop(path)
        errors = []
        seen = {ep.key for ep in self.endpoints}
        for path, hid_name, _bus, _product in nodes:
            if path in self.conns and not force:
                continue
            if path in self.conns:
                # Dropping the old connection removes its endpoints; clear those keys
                # from seen so a force-rescan can re-add them (ListHidppDevices / GUI).
                for ep in self.endpoints:
                    if ep.conn is self.conns[path]:
                        seen.discard(ep.key)
                self._drop(path)
            try:
                conn = HidrawConn(path)
            except OSError as e:
                errors.append({'path': path, 'name': hid_name, 'error': e.strerror or str(e),
                               'permission': isinstance(e, PermissionError)})
                if isinstance(e, PermissionError):
                    self._warn_once(('perm', path),
                                    f'HID++: no access to {path} ({hid_name}); run the '
                                    'one-time permission setup to use firmware buttons')
                continue
            try:
                eps = probe(conn, hid_name)
                for ep in eps:
                    ep.read_name()
                    ep.read_controls()
            except (OSError, HidppError, TimeoutError) as e:
                errors.append({'path': path, 'name': hid_name, 'error': str(e)})
                conn.close()
                continue
            eps = [ep for ep in eps if ep.key not in seen]
            if not eps:
                conn.close()
                continue
            self.conns[path] = conn
            for ep in eps:
                seen.add(ep.key)
                self.endpoints.append(ep)
                self._warn_once(('found', ep.key, path), f'HID++: {ep.name} on {path} '
                                f'index {ep.index:#x}: ' + ', '.join(
                                    f'{c:#06x}{"" if i["divertable"] else " (fixed)"}'
                                    for c, i in sorted(ep.controls.items())))
        self.errors = errors

    def _wanted(self, ep, bindings, learning):
        wanted = {c for c in bindings.get(ep.key, {}) if ep.controls.get(c, {}).get('divertable')}
        if learning:
            wanted |= {c for c, i in ep.controls.items()
                       if i['divertable'] and not i['os_visible']}
        return wanted

    def _sync(self, ep, wanted):
        for cid in ep.diverted - wanted:
            ep.set_divert(cid, False)
        for cid in wanted:
            if cid not in ep.diverted or not ep.is_diverted(cid):
                ep.set_divert(cid, True)
                print(f'{ids.SLUG}: HID++ diverted {control_name(cid)} on {ep.name}', flush=True)
        ep.diverted = set(wanted)
        ep.last_check = time.monotonic()

    def _handle(self, conn, msg, bindings):
        # divertedButtonsEvent: feature REPROG_V4, function 0, sw id 0, up to 4 pressed CIDs.
        if len(msg) < 12 or msg[0] != REPORT_LONG or msg[3] != 0:
            return
        for ep in self.endpoints:
            if ep.conn is not conn or ep.index != msg[1] or \
                    ep.features.get(FEATURE_REPROG_V4) != msg[2]:
                continue
            now = {(msg[i] << 8) | msg[i + 1] for i in range(4, 12, 2)} - {0}
            for cid in now - ep.pressed:
                if self.learner and self.learner.offer_hidpp(ep, cid):
                    ep.pressed = now
                    return
            for cid in ep.pressed - now:
                binding = bindings.get(ep.key, {}).get(cid)
                if binding:
                    self.runner.run(binding)
            ep.pressed = now

    def run(self):
        generation = -1
        was_learning = False
        while not self._stopping.is_set():
            with self.lock:
                requests, self._requests = self._requests, []
                bindings = self.bindings
                changed = generation != self.generation
                generation = self.generation
            for req in requests:
                req()
            learning = bool(self.learner and self.learner.active)
            need = bool(bindings) or learning or any(ep.diverted for ep in self.endpoints)
            if not need:
                time.sleep(0.5)
                continue
            missing = set(bindings) - {ep.key for ep in self.endpoints}
            if (missing or learning) and time.monotonic() - self.last_scan > RESCAN_SECONDS:
                self._scan()
            for ep in list(self.endpoints):
                try:
                    if changed or learning != was_learning or \
                            time.monotonic() - ep.last_check > RECHECK_SECONDS:
                        self._sync(ep, self._wanted(ep, bindings, learning))
                except (OSError, HidppError, TimeoutError) as e:
                    print(f'{ids.SLUG}: HID++ {ep.name} lost ({e}); retrying', flush=True)
                    self._drop(ep.conn.path)
                    self.last_scan = 0
            was_learning = learning
            fds = {c.fd: c for c in self.conns.values()}
            for conn in fds.values():
                pending, conn.pending = conn.pending, []
                for msg in pending:
                    self._handle(conn, msg, bindings)
            if not fds:
                time.sleep(1)
                continue
            r, _, _ = select.select(list(fds), [], [], 0.5)
            for fd in r:
                conn = fds[fd]
                try:
                    data = os.read(fd, 64)
                except OSError as e:
                    print(f'{ids.SLUG}: HID++ {conn.path} lost ({e})', flush=True)
                    self._drop(conn.path)
                    continue
                self._handle(conn, data, bindings)
        self._undivert_all()

    def _undivert_all(self):
        for ep in self.endpoints:
            for cid in list(ep.diverted):
                try:
                    ep.set_divert(cid, False)
                except (OSError, HidppError, TimeoutError):
                    pass
            if ep.diverted:
                print(f'{ids.SLUG}: HID++ diversion removed on {ep.name}', flush=True)
            ep.diverted = set()
        for path in list(self.conns):
            self._drop(path)
