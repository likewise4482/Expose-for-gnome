"""evdev backend (Wayland and X11): exclusively grab only the devices that have
a mapped button, swallow the mapped buttons and re-emit everything else through
a uinput clone of each device.

Needs read access to /dev/input/event* and write access to /dev/uinput
(one-time setup: scripts/setup-permissions.sh, run from the GUI via pkexec).
"""
import errno
import os
import select
import time

import evdev
from evdev import UInput, ecodes

from . import codes, devices, ids

MODIFIER_CODES = {'ctrl': ecodes.KEY_LEFTCTRL, 'shift': ecodes.KEY_LEFTSHIFT,
                  'alt': ecodes.KEY_LEFTALT, 'super': ecodes.KEY_LEFTMETA}
KEY_ALIASES = {'page_up': 'PAGEUP', 'page_down': 'PAGEDOWN', 'prior': 'PAGEUP',
               'next': 'PAGEDOWN', 'return': 'ENTER', 'escape': 'ESC', 'bracketleft':
               'LEFTBRACE', 'bracketright': 'RIGHTBRACE', 'minus': 'MINUS', 'equal': 'EQUAL',
               'plus': 'EQUAL', 'super_l': 'LEFTMETA', 'print': 'SYSRQ', 'del': 'DELETE',
               'ins': 'INSERT', 'pgup': 'PAGEUP', 'pgdn': 'PAGEDOWN'}
RESCAN_SECONDS = 2


def key_code(name):
    n = KEY_ALIASES.get(name.lower(), name.upper())
    code = ecodes.ecodes.get(f'KEY_{n}')
    if code is None:
        raise ValueError(f'unknown key {name!r}')
    return code


class Tracked:
    def __init__(self, info, dev, ui=None):
        self.info = info      # devices.InputDevice
        self.dev = dev        # evdev.InputDevice
        self.ui = ui          # uinput clone when grabbed
        self.swallow = set()  # codes whose release must be swallowed too

    @property
    def grabbed(self):
        return self.ui is not None

    def close(self):
        try:
            if self.ui:
                self.dev.ungrab()
        except OSError:
            pass
        for obj in (self.dev, self.ui):
            try:
                if obj:
                    obj.close()
            except OSError:
                pass


class EvdevBackend:
    name = 'evdev'

    def __init__(self, runner, learner=None):
        self.runner = runner
        self.learner = learner
        self.mapping = None
        self.tracked = {}     # path -> Tracked
        self.kbd = None
        self.problem = None
        self._last_scan = 0

    def apply(self, mapping):
        self.mapping = mapping
        self._last_scan = 0
        for dkey, bindings in [('*', mapping.any), *mapping.per_device.items()]:
            for bkey, b in bindings.items():
                if codes.code(bkey) is not None:
                    print(f'{ids.SLUG}: {dkey} {bkey} -> {b.get("action")}', flush=True)

    def status(self):
        return {'backend': self.name, 'problem': self.problem,
                'grabbed': sorted(t.info.name for t in self.tracked.values() if t.grabbed)}

    def _want(self, info, learning):
        """'grab', 'listen' or None for a device."""
        if info.is_own or self.mapping is None:
            return None
        if self.mapping.event_codes_for(info.key) & info.keys:
            return 'grab'
        if learning and info.relevant:
            return 'listen'
        return None

    def _rescan(self):
        self._last_scan = time.monotonic()
        learning = bool(self.learner and self.learner.active)
        infos = {d.path: d for d in devices.list_devices(include_all=True)}
        denied = False
        for path in list(self.tracked):
            t = self.tracked[path]
            want = self._want(infos[path], learning) if path in infos else None
            if want != ('grab' if t.grabbed else 'listen'):
                t.close()
                del self.tracked[path]
                if t.grabbed:
                    print(f'{ids.SLUG}: released {path} ({t.info.name})', flush=True)
        for path, info in infos.items():
            want = self._want(info, learning)
            if not want or path in self.tracked:
                continue
            try:
                dev = evdev.InputDevice(path)
            except PermissionError:
                denied = True
                continue
            except OSError:
                continue
            ui = None
            if want == 'grab':
                try:
                    ui = UInput.from_device(dev, name=f'{dev.name} ({ids.SLUG})')
                except (OSError, evdev.uinput.UInputError) as e:
                    self.problem = f'cannot open /dev/uinput ({e}); run the permission setup'
                    print(f'{ids.SLUG}: {self.problem}', flush=True)
                    dev.close()
                    continue
                try:
                    dev.grab()
                except OSError as e:
                    print(f'{ids.SLUG}: cannot grab {path} ({e}); another program '
                          'has it grabbed?', flush=True)
                    ui.close()
                    dev.close()
                    continue
                print(f'{ids.SLUG}: grabbed {path} ({info.name}, {info.key})', flush=True)
            self.tracked[path] = Tracked(info, dev, ui)
        if denied:
            self.problem = 'no permission to read /dev/input; run the permission setup'
        elif self.problem and 'read /dev/input' in self.problem:
            self.problem = None

    def _handle(self, t, ev):
        if ev.type == ecodes.EV_KEY:
            if ev.value == 1 and self.learner and \
                    self.learner.offer_event(t.info.key, t.info.name, ev.code, self.name):
                t.swallow.add(ev.code)
                self._last_scan = 0
                return
            if ev.code in t.swallow:
                if ev.value == 0:
                    t.swallow.discard(ev.code)
                return
            binding = self.mapping.lookup(t.info.key, codes.name(ev.code)) if t.grabbed else None
            if binding:
                if ev.value == 0 and binding.get('action') != 'block':
                    self.runner.run(binding)
                return
        if t.grabbed:
            t.ui.write_event(ev)

    def run(self, poll_reload, stop):
        was_learning = False
        while not stop.is_set():
            poll_reload()
            learning = bool(self.learner and self.learner.active)
            if learning != was_learning or time.monotonic() - self._last_scan > RESCAN_SECONDS:
                self._rescan()
                was_learning = learning
            fds = {t.dev.fd: t for t in self.tracked.values()}
            if not fds:
                time.sleep(0.5)
                continue
            r, _, _ = select.select(list(fds), [], [], 0.5)
            for fd in r:
                t = fds[fd]
                try:
                    for ev in t.dev.read():
                        self._handle(t, ev)
                except OSError as e:
                    if e.errno in (errno.ENODEV, errno.EIO, errno.EBADF):
                        print(f'{ids.SLUG}: {t.info.name} disconnected', flush=True)
                        t.close()
                        self.tracked.pop(t.info.path, None)
                    else:
                        raise

    def close(self):
        for t in self.tracked.values():
            t.close()
        self.tracked = {}
        if self.kbd:
            self.kbd.close()
            self.kbd = None

    def send(self, mods, key):
        codes_ = [MODIFIER_CODES[m] for m in mods] + [key_code(key)]
        if not self.kbd:
            self.kbd = UInput({ecodes.EV_KEY: list(range(1, 249))},
                              name=f'{ids.SLUG} keyboard')
            time.sleep(0.2)
        for c in codes_:
            self.kbd.write(ecodes.EV_KEY, c, 1)
        self.kbd.syn()
        for c in reversed(codes_):
            self.kbd.write(ecodes.EV_KEY, c, 0)
        self.kbd.syn()


def available():
    return os.access('/dev/uinput', os.W_OK)
