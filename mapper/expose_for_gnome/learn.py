"""Press to assign: the next button/key press from any device is reported
instead of being acted on. Backends call offer_*(); one waiter at a time."""
import threading
import time

from . import codes


class Learner:
    def __init__(self):
        self._lock = threading.Lock()
        self._callback = None
        self._deadline = 0

    @property
    def active(self):
        with self._lock:
            if self._callback and time.monotonic() > self._deadline:
                cb, self._callback = self._callback, None
            else:
                cb = None
        if cb:
            cb({'timeout': True})
        with self._lock:
            return self._callback is not None

    def start(self, timeout_s, callback):
        """callback(result_dict) is called exactly once, from a backend thread."""
        with self._lock:
            old, self._callback = self._callback, callback
            self._deadline = time.monotonic() + timeout_s
        if old:
            old({'cancelled': True})

    def cancel(self):
        self._finish({'cancelled': True})

    def _finish(self, result):
        with self._lock:
            cb, self._callback = self._callback, None
        if cb:
            cb(result)
            return True
        return False

    def offer_event(self, device_key, device_name, code, source):
        """An EV_KEY press (value 1). Returns True if consumed by learn mode."""
        if not self.active or codes.is_modifier(code):
            return False
        if code == codes.KEY_ESC:
            return self._finish({'cancelled': True})
        key = codes.name(code)
        return self._finish({'device_key': device_key, 'device_name': device_name,
                             'binding_key': key, 'label': codes.label(key), 'source': source})

    def offer_hidpp(self, endpoint, cid):
        if not self.active:
            return False
        key = codes.hidpp_key(cid)
        return self._finish({'device_key': endpoint.key, 'device_name': endpoint.name,
                             'binding_key': key, 'label': codes.label(key), 'source': 'hidpp'})
