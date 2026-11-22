"""Virtual relative mouse and keyboard via /dev/uinput (stdlib-only)."""
import fcntl, os, struct, threading, time

EV_SYN, EV_KEY, EV_REL = 0, 1, 2
REL_X, REL_Y, REL_HWHEEL, REL_WHEEL = 0, 1, 6, 8
BTN = {"l": 0x110, "r": 0x111, "m": 0x112}

MOD_KEYS = {
    "ctrl": 29,    # KEY_LEFTCTRL
    "shift": 42,   # KEY_LEFTSHIFT
    "alt": 56,     # KEY_LEFTALT
    "super": 125,  # KEY_LEFTMETA
}

NAMED_KEYS = {
    **MOD_KEYS,
    "esc": 1, "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8, "8": 9, "9": 10, "0": 11,
    "-": 12, "=": 13, "backspace": 14, "tab": 15,
    "q": 16, "w": 17, "e": 18, "r": 19, "t": 20, "y": 21, "u": 22, "i": 23, "o": 24, "p": 25,
    "[": 26, "]": 27, "enter": 28,
    "a": 30, "s": 31, "d": 32, "f": 33, "g": 34, "h": 35, "j": 36, "k": 37, "l": 38,
    ";": 39, "'": 40, "`": 41, "\\": 43,
    "z": 44, "x": 45, "c": 46, "v": 47, "b": 48, "n": 49, "m": 50,
    ",": 51, ".": 52, "/": 53, "space": 57,
    "f1": 59, "f2": 60, "f3": 61, "f4": 62, "f5": 63, "f6": 64,
    "f7": 65, "f8": 66, "f9": 67, "f10": 68, "f11": 87, "f12": 88,
    "home": 102, "up": 103, "pgup": 104, "left": 105, "right": 106, "end": 107, "down": 108, "pgdn": 109,
    "delete": 111, "mute": 113, "voldown": 114, "volup": 115,
    "next": 163, "play": 164, "prev": 165,
}

SHIFT_CHARS = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8", "(": "9", ")": "0",
    "_": "-", "+": "=", "{": "[", "}": "]", "|": "\\", ":": ";", '"': "'", "~": "`", "<": ",", ">": ".", "?": "/",
}

CHAR_MAP = {" ": (57, False), "\n": (28, False), "\t": (15, False)}
for _ch in "abcdefghijklmnopqrstuvwxyz0123456789-=[];'\\,./`":
    CHAR_MAP[_ch] = (NAMED_KEYS[_ch], False)
for _ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    CHAR_MAP[_ch] = (NAMED_KEYS[_ch.lower()], True)
for _ch, _base in SHIFT_CHARS.items():
    CHAR_MAP[_ch] = (NAMED_KEYS[_base], True)

ALL_KEYCODES = sorted(set(BTN.values()) | set(NAMED_KEYS.values()))


class VirtualInput:
    """Virtual relative mouse and keyboard via /dev/uinput."""
    def __init__(self):
        self.fd = None
        self.lock = threading.Lock()

    def _open(self):
        fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        reqs = [
            (0x40045564, EV_KEY),
            (0x40045564, EV_REL),
            *((0x40045565, k) for k in ALL_KEYCODES),
            *((0x40045566, r) for r in (REL_X, REL_Y, REL_HWHEEL, REL_WHEEL)),
        ]
        for req, val in reqs:
            fcntl.ioctl(fd, req, val)
        os.write(fd, struct.pack("80sHHHHi" + "i" * 256, b"niri-remote input", 3, 0x1234, 0x5678, 1, 0, *([0] * 256)))
        fcntl.ioctl(fd, 0x5501)  # UI_DEV_CREATE
        time.sleep(0.3)  # let niri/libinput pick up the new device
        self.fd = fd

    def _ensure(self):
        if self.fd is None:
            self._open()

    def _emit(self, *events):
        t = time.time()
        sec, usec = int(t), int(t % 1 * 1e6)
        buf = b"".join(struct.pack("llHHi", sec, usec, ty, c, v) for ty, c, v in events)
        os.write(self.fd, buf + struct.pack("llHHi", sec, usec, EV_SYN, 0, 0))

    def send(self, dx=0, dy=0, sx=0, sy=0, btn=None, down=None):
        with self.lock:
            self._ensure()
            ev = []
            if dx: ev.append((EV_REL, REL_X, dx))
            if dy: ev.append((EV_REL, REL_Y, dy))
            if sy: ev.append((EV_REL, REL_WHEEL, sy))
            if sx: ev.append((EV_REL, REL_HWHEEL, sx))
            if btn in BTN and down in (0, 1): ev.append((EV_KEY, BTN[btn], down))
            if ev: self._emit(*ev)

    def key(self, name, mods=()):
        k = NAMED_KEYS.get(str(name).lower())
        if k is None:
            return False
        m_codes = [MOD_KEYS[m.lower()] for m in mods if str(m).lower() in MOD_KEYS and MOD_KEYS[m.lower()] != k]
        with self.lock:
            self._ensure()
            for mc in m_codes:
                self._emit((EV_KEY, mc, 1))
            self._emit((EV_KEY, k, 1))
            self._emit((EV_KEY, k, 0))
            for mc in reversed(m_codes):
                self._emit((EV_KEY, mc, 0))
        return True

    def type_text(self, text):
        text = str(text)[:512]
        if not text:
            return False
        with self.lock:
            self._ensure()
            shift_code = MOD_KEYS["shift"]
            for ch in text:
                entry = CHAR_MAP.get(ch)
                if not entry:
                    continue
                code, shift = entry
                if shift:
                    self._emit((EV_KEY, shift_code, 1))
                self._emit((EV_KEY, code, 1))
                self._emit((EV_KEY, code, 0))
                if shift:
                    self._emit((EV_KEY, shift_code, 0))
                time.sleep(0.002)
        return True

    def release_all(self):
        with self.lock:
            if self.fd is None:
                return
            for code in (*BTN.values(), *MOD_KEYS.values()):
                try:
                    self._emit((EV_KEY, code, 0))
                except Exception:
                    pass


INPUT = VirtualInput()
MOUSE = INPUT
