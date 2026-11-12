"""X11 backend: XInput2 passive grabs via libX11/libXi (ctypes, no extra packages).

* Per-device bindings grab the button/key on that device's XI2 slave device,
  so the same button on another mouse keeps working normally.
* "Any device" bindings grab on the master pointer/keyboard (like a core grab).
* Learn mode listens to XI2 raw events on the root window, which carry the
  source device.

Grabbed buttons are delivered only to us, so e.g. a browser no longer sees a
mapped "back" button; unmapped buttons are not grabbed. No root needed.
"""
import ctypes
import ctypes.util
import select
from ctypes import (CFUNCTYPE, POINTER, Structure, Union, byref, c_char_p, c_int, c_long,
                    c_ubyte, c_uint, c_ulong, c_void_p)

from . import codes, devices, ids

GenericEvent = 35
XI_KeyPress, XI_KeyRelease, XI_ButtonPress, XI_ButtonRelease = 2, 3, 4, 5
XI_HierarchyChanged = 11
XI_RawKeyPress, XI_RawButtonPress = 13, 15
XI_LASTEVENT = 26
XIAllDevices = 0
XIMasterPointer, XIMasterKeyboard, XISlavePointer, XISlaveKeyboard = 1, 2, 3, 4
XIAnyModifier = 1 << 31
GrabModeAsync = 1
BadAccess = 10


class XIEventMask(Structure):
    _fields_ = [('deviceid', c_int), ('mask_len', c_int), ('mask', POINTER(c_ubyte))]


class XIGrabModifiers(Structure):
    _fields_ = [('modifiers', c_int), ('status', c_int)]


class XIDeviceInfo(Structure):
    _fields_ = [('deviceid', c_int), ('name', c_char_p), ('use', c_int), ('attachment', c_int),
                ('enabled', c_int), ('num_classes', c_int), ('classes', c_void_p)]


class XGenericEventCookie(Structure):
    _fields_ = [('type', c_int), ('serial', c_ulong), ('send_event', c_int),
                ('display', c_void_p), ('extension', c_int), ('evtype', c_int),
                ('cookie', c_uint), ('data', c_void_p)]


class XIEventHead(Structure):
    """Common prefix of XIDeviceEvent and XIRawEvent."""
    _fields_ = [('type', c_int), ('serial', c_ulong), ('send_event', c_int),
                ('display', c_void_p), ('extension', c_int), ('evtype', c_int),
                ('time', c_ulong), ('deviceid', c_int), ('sourceid', c_int), ('detail', c_int)]


class XEvent(Union):
    _fields_ = [('type', c_int), ('xcookie', XGenericEventCookie), ('pad', c_long * 24)]


class XErrorEvent(Structure):
    _fields_ = [('type', c_int), ('display', c_void_p), ('resourceid', c_ulong),
                ('serial', c_ulong), ('error_code', c_ubyte), ('request_code', c_ubyte),
                ('minor_code', c_ubyte)]


ERROR_HANDLER = CFUNCTYPE(c_int, c_void_p, POINTER(XErrorEvent))


def _load(name, soname):
    return ctypes.CDLL(ctypes.util.find_library(name) or soname)


def _mask(*events):
    buf = (c_ubyte * ((XI_LASTEVENT + 8) // 8))()
    for e in events:
        buf[e >> 3] |= 1 << (e & 7)
    return buf


class XDevice:
    def __init__(self, xid, name, use, key, path=None):
        self.xid, self.name, self.use, self.key, self.path = xid, name, use, key, path

    @property
    def is_pointer(self):
        return self.use in (XIMasterPointer, XISlavePointer)

    @property
    def is_master(self):
        return self.use in (XIMasterPointer, XIMasterKeyboard)


class X11Backend:
    name = 'x11'

    def __init__(self, runner, learner=None, display=None):
        self.runner = runner
        self.learner = learner
        x = self.x = _load('X11', 'libX11.so.6')
        xi = self.xi = _load('Xi', 'libXi.so.6')
        x.XInitThreads()
        x.XOpenDisplay.restype = c_void_p
        x.XOpenDisplay.argtypes = [c_char_p]
        x.XDefaultRootWindow.restype = c_ulong
        x.XDefaultRootWindow.argtypes = [c_void_p]
        x.XConnectionNumber.argtypes = [c_void_p]
        x.XPending.argtypes = [c_void_p]
        x.XNextEvent.argtypes = [c_void_p, POINTER(XEvent)]
        x.XSync.argtypes = [c_void_p, c_int]
        x.XFlush.argtypes = [c_void_p]
        x.XFree.argtypes = [c_void_p]
        x.XQueryExtension.argtypes = [c_void_p, c_char_p, POINTER(c_int), POINTER(c_int),
                                      POINTER(c_int)]
        x.XGetEventData.argtypes = [c_void_p, POINTER(XGenericEventCookie)]
        x.XFreeEventData.argtypes = [c_void_p, POINTER(XGenericEventCookie)]
        x.XInternAtom.restype = c_ulong
        x.XInternAtom.argtypes = [c_void_p, c_char_p, c_int]
        x.XStringToKeysym.restype = c_ulong
        x.XStringToKeysym.argtypes = [c_char_p]
        x.XKeysymToKeycode.restype = c_ubyte
        x.XKeysymToKeycode.argtypes = [c_void_p, c_ulong]
        x.XSetErrorHandler.argtypes = [ERROR_HANDLER]
        x.XSetErrorHandler.restype = c_void_p
        xi.XIQueryVersion.argtypes = [c_void_p, POINTER(c_int), POINTER(c_int)]
        xi.XIQueryDevice.restype = POINTER(XIDeviceInfo)
        xi.XIQueryDevice.argtypes = [c_void_p, c_int, POINTER(c_int)]
        xi.XIFreeDeviceInfo.argtypes = [POINTER(XIDeviceInfo)]
        xi.XISelectEvents.argtypes = [c_void_p, c_ulong, POINTER(XIEventMask), c_int]
        grab_args = [c_void_p, c_int, c_int, c_ulong]
        xi.XIGrabButton.argtypes = grab_args[:3] + [c_ulong, c_ulong, c_int, c_int, c_int,
                                                    POINTER(XIEventMask), c_int,
                                                    POINTER(XIGrabModifiers)]
        xi.XIUngrabButton.argtypes = grab_args + [c_int, POINTER(XIGrabModifiers)]
        xi.XIGrabKeycode.argtypes = grab_args + [c_int, c_int, c_int, POINTER(XIEventMask),
                                                 c_int, POINTER(XIGrabModifiers)]
        xi.XIUngrabKeycode.argtypes = grab_args + [c_int, POINTER(XIGrabModifiers)]
        xi.XIGetProperty.argtypes = [c_void_p, c_int, c_ulong, c_long, c_long, c_int, c_ulong,
                                     POINTER(c_ulong), POINTER(c_int), POINTER(c_ulong),
                                     POINTER(c_ulong), POINTER(POINTER(c_ubyte))]

        self._last_error = 0

        def on_error(_dpy, err):
            self._last_error = err.contents.error_code
            return 0
        self._error_handler = ERROR_HANDLER(on_error)
        x.XSetErrorHandler(self._error_handler)

        self.dpy = x.XOpenDisplay(display.encode() if display else None)
        if not self.dpy:
            raise RuntimeError('cannot open X display (is DISPLAY set?)')
        self.root = x.XDefaultRootWindow(self.dpy)
        opcode, ev, err = c_int(), c_int(), c_int()
        if not x.XQueryExtension(self.dpy, b'XInputExtension', byref(opcode), byref(ev),
                                 byref(err)):
            raise RuntimeError('X server has no XInput extension')
        self.xi_opcode = opcode.value
        major, minor = c_int(2), c_int(2)
        if xi.XIQueryVersion(self.dpy, byref(major), byref(minor)) != 0:
            raise RuntimeError('X server does not support XInput 2.2')
        self.device_node_atom = x.XInternAtom(self.dpy, b'Device Node', False)

        self.mapping = None
        self.devices = {}       # xid -> XDevice
        self.grabs = []         # (kind, xid, detail)
        self._learning = False
        self._learn_swallow = set()
        self.problem = None
        self.warnings = set()
        self._select_root(learn=False)

        try:
            self.xtst = _load('Xtst', 'libXtst.so.6')
            self.xtst.XTestFakeKeyEvent.argtypes = [c_void_p, c_uint, c_int, c_ulong]
        except OSError:
            self.xtst = None

    # --- devices ---------------------------------------------------------
    def _device_node(self, xid):
        type_ret, fmt, n, after = c_ulong(), c_int(), c_ulong(), c_ulong()
        data = POINTER(c_ubyte)()
        if self.xi.XIGetProperty(self.dpy, xid, self.device_node_atom, 0, 1024, False, 0,
                                 byref(type_ret), byref(fmt), byref(n), byref(after),
                                 byref(data)) != 0 or not data:
            return None
        try:
            return ctypes.string_at(data, n.value).decode(errors='replace') if n.value else None
        finally:
            self.x.XFree(data)

    def query_devices(self):
        count = c_int()
        info = self.xi.XIQueryDevice(self.dpy, XIAllDevices, byref(count))
        found = {}
        for i in range(count.value):
            d = info[i]
            if d.use not in (XIMasterPointer, XIMasterKeyboard, XISlavePointer, XISlaveKeyboard):
                continue
            name = d.name.decode(errors='replace')
            path = self._device_node(d.deviceid) if d.use >= XISlavePointer else None
            sysinfo = devices.by_path(path) if path else None
            key = sysinfo.key if sysinfo else devices.device_key(0, 0, name)
            found[d.deviceid] = XDevice(d.deviceid, name, d.use, key, path)
        self.xi.XIFreeDeviceInfo(info)
        return found

    def _select_root(self, learn):
        events = [XI_HierarchyChanged] + ([XI_RawButtonPress, XI_RawKeyPress] if learn else [])
        buf = _mask(*events)
        mask = XIEventMask(XIAllDevices, len(buf), ctypes.cast(buf, POINTER(c_ubyte)))
        self.xi.XISelectEvents(self.dpy, self.root, byref(mask), 1)
        self.x.XFlush(self.dpy)
        self._learning = learn

    # --- grabs -----------------------------------------------------------
    def _ungrab_all(self):
        mods = XIGrabModifiers(XIAnyModifier, 0)
        for kind, xid, detail in self.grabs:
            if kind == 'button':
                self.xi.XIUngrabButton(self.dpy, xid, detail, self.root, 1, byref(mods))
            else:
                self.xi.XIUngrabKeycode(self.dpy, xid, detail, self.root, 1, byref(mods))
        self.grabs = []
        self.x.XSync(self.dpy, False)

    def _grab(self, dev, code, bkey):
        mods = XIGrabModifiers(XIAnyModifier, 0)
        button = codes.x_button(code) if codes.is_button(code) else None
        self._last_error = 0
        if button:
            if not dev.is_pointer:
                return
            buf = _mask(XI_ButtonPress, XI_ButtonRelease)
            mask = XIEventMask(dev.xid, len(buf), ctypes.cast(buf, POINTER(c_ubyte)))
            failed = self.xi.XIGrabButton(self.dpy, dev.xid, button, self.root, 0,
                                          GrabModeAsync, GrabModeAsync, False, byref(mask),
                                          1, byref(mods))
            kind, detail = 'button', button
        elif not codes.is_button(code):
            if dev.is_pointer:
                return
            if codes.x_keycode(code) > 255:
                msg = (f'{bkey} cannot be seen on X11 (X keycodes stop at 255); '
                       'use the evdev backend for it')
                if msg not in self.warnings:
                    print(f'{ids.SLUG}: {msg}', flush=True)
                self.warnings.add(msg)
                return
            buf = _mask(XI_KeyPress, XI_KeyRelease)
            mask = XIEventMask(dev.xid, len(buf), ctypes.cast(buf, POINTER(c_ubyte)))
            detail = codes.x_keycode(code)
            failed = self.xi.XIGrabKeycode(self.dpy, dev.xid, detail, self.root,
                                           GrabModeAsync, GrabModeAsync, False, byref(mask),
                                           1, byref(mods))
            kind = 'key'
        else:
            return
        self.x.XSync(self.dpy, False)
        if failed or self._last_error:
            why = 'already grabbed by another client' if (failed or
                                                          self._last_error == BadAccess) \
                else f'X error {self._last_error}'
            print(f'{ids.SLUG}: cannot grab {bkey} on {dev.name}: {why}', flush=True)
            self.problem = f'{bkey} on {dev.name}: {why}'
            return
        self.grabs.append((kind, dev.xid, detail))
        print(f'{ids.SLUG}: grabbed {bkey} ({kind} {detail}) on {dev.name} [{dev.key}]',
              flush=True)

    def _regrab(self):
        self._ungrab_all()
        self.problem = None
        self.warnings = set()
        self.devices = self.query_devices()
        if not self.mapping:
            return
        for dev in self.devices.values():
            if dev.is_master:
                bindings = self.mapping.any
            else:
                bindings = self.mapping.per_device.get(dev.key, {})
            for bkey in sorted(bindings):
                code = codes.code(bkey)
                if code is not None:
                    self._grab(dev, code, bkey)

    def apply(self, mapping):
        self.mapping = mapping
        self._regrab()

    def status(self):
        return {'backend': self.name, 'problem': self.problem,
                'warnings': sorted(self.warnings),
                'grabbed': sorted({self.devices[x].name for _, x, _ in self.grabs
                                   if x in self.devices})}

    # --- events ----------------------------------------------------------
    def _handle_xi(self, evtype, head):
        source = self.devices.get(head.sourceid)
        if evtype == XI_HierarchyChanged:
            self._regrab()
            return
        if evtype in (XI_RawButtonPress, XI_RawKeyPress):
            if evtype == XI_RawButtonPress:
                code = codes.from_x_button(head.detail)
            else:
                code = codes.from_x_keycode(head.detail)
            if code is None or source is None:
                return
            if self.learner and self.learner.offer_event(source.key, source.name, code, self.name):
                kind = 'button' if evtype == XI_RawButtonPress else 'key'
                self._learn_swallow.add((kind, head.detail))
            return
        is_button = evtype in (XI_ButtonPress, XI_ButtonRelease)
        kind = 'button' if is_button else 'key'
        released = evtype in (XI_ButtonRelease, XI_KeyRelease)
        if (kind, head.detail) in self._learn_swallow:
            if released:
                self._learn_swallow.discard((kind, head.detail))
            return
        if not released or self._learning:
            return
        code = codes.from_x_button(head.detail) if is_button else codes.from_x_keycode(head.detail)
        dkey = source.key if source else '*'
        binding = self.mapping.lookup(dkey, codes.name(code)) if self.mapping else None
        # Act on release: during the press the device is still grabbed by us, which
        # would make the shell's own modal grab fail.
        if binding and binding.get('action') != 'block':
            self.runner.run(binding)

    def run(self, poll_reload, stop):
        fd = self.x.XConnectionNumber(self.dpy)
        event = XEvent()
        while not stop.is_set():
            learning = bool(self.learner and self.learner.active)
            if learning != self._learning:
                self._select_root(learning)
            while self.x.XPending(self.dpy):
                self.x.XNextEvent(self.dpy, byref(event))
                if event.type != GenericEvent or event.xcookie.extension != self.xi_opcode:
                    continue
                cookie = event.xcookie
                if not self.x.XGetEventData(self.dpy, byref(cookie)):
                    continue
                try:
                    head = ctypes.cast(cookie.data, POINTER(XIEventHead)).contents
                    self._handle_xi(cookie.evtype, head)
                finally:
                    self.x.XFreeEventData(self.dpy, byref(cookie))
            select.select([fd], [], [], 0.25)
            poll_reload()

    def close(self):
        if self.dpy:
            self._ungrab_all()

    # --- key output via XTest ---------------------------------------------
    MODIFIER_KEYSYMS = {'ctrl': 'Control_L', 'shift': 'Shift_L', 'alt': 'Alt_L',
                        'super': 'Super_L'}

    def send(self, mods, key):
        if not self.xtst:
            raise ValueError('libXtst not available')
        keycodes = []
        for name in [self.MODIFIER_KEYSYMS[m] for m in mods] + [key]:
            sym = self.x.XStringToKeysym(name.encode())
            if not sym and len(name) == 1:
                sym = self.x.XStringToKeysym(name.lower().encode())
            code = self.x.XKeysymToKeycode(self.dpy, sym) if sym else 0
            if not code:
                raise ValueError(f'unknown key {name!r}')
            keycodes.append(code)
        for code in keycodes:
            self.xtst.XTestFakeKeyEvent(self.dpy, code, True, 0)
        for code in reversed(keycodes):
            self.xtst.XTestFakeKeyEvent(self.dpy, code, False, 0)
        self.x.XFlush(self.dpy)
