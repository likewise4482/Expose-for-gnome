"""Simulated Logitech HID++ 2.0 devices behind a socketpair that stands in for
/dev/hidrawN: a directly connected mouse (index 0xFF) or a receiver (HID++ 1.0
at 0xFF) with paired devices at indices 1..6."""
import socket
import threading

REPROG_INDEX = 0x09
NAME_INDEX = 0x03


class FakeDevice:
    def __init__(self, name, controls):
        self.name = name
        self.controls = controls                  # [(cid, flags)]
        self.reporting = {cid: 0 for cid, _ in controls}


MX_VERTICAL = [(0x0050, 0x31), (0x0051, 0x31), (0x0052, 0x31), (0x0053, 0x31),
               (0x0056, 0x31), (0x00FD, 0x30)]
GESTURE_MOUSE = [(0x0050, 0x31), (0x0051, 0x31), (0x0053, 0x31), (0x0056, 0x31),
                 (0x00C3, 0x30), (0x00C4, 0x30), (0x00D7, 0xB0), (0x0090, 0x00)]


class FakeHidraw(threading.Thread):
    """devices: {index: FakeDevice}. index 0xFF -> direct device; otherwise a receiver."""

    def __init__(self, devices, noise=True):
        super().__init__(daemon=True)
        self.devices = devices
        self.noise = noise
        self.host, self.sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.log = []
        self.start()

    def host_fd(self):
        return self.host.detach()

    def _reply(self, req, params):
        self.sock.send(bytes(req[:4]) + bytes(params).ljust(16, b'\0'))

    def _error20(self, req, code):
        self.sock.send(bytes([0x11, req[1], 0xFF, req[2], req[3], code]).ljust(20, b'\0'))

    def _error10(self, req, code):
        self.sock.send(bytes([0x10, req[1], 0x8F, req[2], req[3], code, 0]))

    def run(self):
        while True:
            try:
                req = self.sock.recv(64)
            except OSError:
                return
            if not req:
                return
            self.log.append(bytes(req[:9]))
            if self.noise:
                # Unrelated mouse input report the reader must skip.
                self.sock.send(bytes([0x02, 0, 0, 1, 0, 0, 0, 0]))
            index, feat, fn = req[1], req[2], req[3] >> 4
            dev = self.devices.get(index)
            if dev is None:
                if index == 0xFF:
                    self._error10(req, 0x01)     # receiver: HID++ 1.0, invalid sub id
                else:
                    self._error10(req, 0x09)     # unknown device (empty slot)
                continue
            if feat == 0 and fn == 0:
                fid = (req[4] << 8) | req[5]
                self._reply(req, [{0x1B04: REPROG_INDEX, 0x0005: NAME_INDEX}.get(fid, 0)])
            elif feat == 0 and fn == 1:
                self._reply(req, [4, 2, req[6]])
            elif feat == NAME_INDEX and fn == 0:
                self._reply(req, [len(dev.name)])
            elif feat == NAME_INDEX and fn == 1:
                start = req[4]
                self._reply(req, dev.name.encode()[start:start + 16])
            elif feat == REPROG_INDEX and fn == 0:
                self._reply(req, [len(dev.controls)])
            elif feat == REPROG_INDEX and fn == 1:
                cid, flags = dev.controls[req[4]]
                self._reply(req, [cid >> 8, cid & 0xFF, 0, cid & 0xFF, flags, 0, 0, 0, 0])
            elif feat == REPROG_INDEX and fn == 2:
                cid = (req[4] << 8) | req[5]
                self._reply(req, [req[4], req[5], dev.reporting[cid]])
            elif feat == REPROG_INDEX and fn == 3:
                cid = (req[4] << 8) | req[5]
                if dict(dev.controls)[cid] & 0x20 == 0:
                    self._error20(req, 0x02)
                    continue
                if req[6] & 0x02:
                    dev.reporting[cid] = req[6] & 0x01
                self._reply(req, req[4:9])
            else:
                self._error20(req, 0x02)

    def press(self, index, *cids):
        payload = b''.join(bytes([c >> 8, c & 0xFF]) for c in cids)
        self.sock.send((bytes([0x11, index, REPROG_INDEX, 0x00]) + payload).ljust(20, b'\0'))

    def release(self, index):
        self.press(index)
