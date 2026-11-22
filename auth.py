"""PIN and WebAuthn (fingerprint / face unlock) authentication for niri-remote."""
import base64, hashlib, hmac, json, os, re, secrets, sys, threading, time

PIN_FILE = os.environ.get("PANEL_PIN_FILE", os.path.expanduser("~/.config/niri-remote/pin"))
IDLE = int(os.environ.get("PANEL_PIN_TIMEOUT", "600"))   # seconds without use before the PIN is asked again
MAX_TRIES = 5
LOCKS = [600, 3600, 21600, 86400]                         # lockout after 5 failures: 10m, 1h, 6h, then 24h

PASSKEY_FILE = os.environ.get("PANEL_PASSKEY_FILE", os.path.expanduser("~/.config/niri-remote/passkeys.json"))
try:
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import hashes
    WEBAUTHN = True
except ImportError:
    WEBAUTHN = False


class Auth:
    def __init__(self):
        self.lock = threading.Lock()
        self.sessions = {}      # session id -> monotonic time of last use
        self.fails = 0
        self.level = 0
        self.locked_until = 0.0

    def enabled(self):
        return os.path.exists(PIN_FILE)

    def check(self, sid):
        if not self.enabled():
            return True
        now = time.monotonic()
        with self.lock:
            t = self.sessions.get(sid)
            if t is None or now - t > IDLE:
                self.sessions.pop(sid, None)
                return False
            self.sessions[sid] = now
            return True

    def peek(self, sid):
        with self.lock:
            t = self.sessions.get(sid)
            return t is not None and time.monotonic() - t <= IDLE

    def remaining(self):
        return max(0, int(self.locked_until - time.monotonic()) + 1) if time.monotonic() < self.locked_until else 0

    def _verify(self, pin):
        try:
            with open(PIN_FILE) as f:
                algo, it, salt, want = f.read().strip().split("$")
            got = hashlib.pbkdf2_hmac("sha256", pin.encode(), base64.b64decode(salt), int(it))
            return hmac.compare_digest(got, base64.b64decode(want))
        except Exception:
            return False

    def login(self, pin):
        now = time.monotonic()
        with self.lock:
            if now < self.locked_until:
                return 429, {"locked": int(self.locked_until - now) + 1}, None
            if self._verify(pin):
                self.fails = self.level = 0
                for k in [k for k, t in self.sessions.items() if now - t > IDLE]:
                    del self.sessions[k]
                sid = secrets.token_urlsafe(32)
                self.sessions[sid] = now
                return 200, {}, sid
            self.fails += 1
            if self.fails >= MAX_TRIES:
                dur = LOCKS[min(self.level, len(LOCKS) - 1)]
                self.level += 1
                self.fails = 0
                self.locked_until = now + dur
                return 429, {"locked": dur}, None
            return 401, {"left": MAX_TRIES - self.fails}, None

    def create_session(self):
        with self.lock:
            sid = secrets.token_urlsafe(32)
            self.sessions[sid] = time.monotonic()
            return sid

    def logout(self, sid):
        with self.lock:
            self.sessions.pop(sid, None)


AUTH = Auth()


def set_pin():
    import getpass
    a = getpass.getpass("New 6-digit PIN: ")
    if not re.fullmatch(r"\d{6}", a) or a != getpass.getpass("Repeat PIN: "):
        sys.exit("PIN must be exactly 6 digits and match.")
    salt = os.urandom(16)
    h = hashlib.pbkdf2_hmac("sha256", a.encode(), salt, 200_000)
    os.makedirs(os.path.dirname(PIN_FILE), mode=0o700, exist_ok=True)
    fd = os.open(PIN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("pbkdf2_sha256$200000$%s$%s\n" % (base64.b64encode(salt).decode(), base64.b64encode(h).decode()))
    print("PIN saved to", PIN_FILE, "(takes effect immediately)")


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def cbor(b, i=0):
    """Minimal CBOR decoder (definite lengths only): enough for WebAuthn attestation data."""
    mt, ai = b[i] >> 5, b[i] & 31
    i += 1
    if ai < 24:
        n = ai
    elif ai in (24, 25, 26, 27):
        w = 1 << (ai - 24)
        n = int.from_bytes(b[i:i + w], "big")
        i += w
    else:
        raise ValueError("unsupported cbor")
    if mt == 0:
        return n, i
    if mt == 1:
        return -1 - n, i
    if mt in (2, 3):
        if i + n > len(b):
            raise ValueError("short cbor")
        v = bytes(b[i:i + n])
        return (v if mt == 2 else v.decode()), i + n
    if mt == 4:
        out = []
        for _ in range(n):
            v, i = cbor(b, i)
            out.append(v)
        return out, i
    if mt == 5:
        out = {}
        for _ in range(n):
            k, i = cbor(b, i)
            v, i = cbor(b, i)
            out[k] = v
        return out, i
    raise ValueError("unsupported cbor")


class Passkeys:
    def __init__(self):
        self.lock = threading.Lock()
        self.challenges = {}   # challenge -> (expiry, kind)

    def load(self):
        try:
            with open(PASSKEY_FILE) as f:
                return json.load(f)
        except Exception:
            return []

    def save(self, items):
        os.makedirs(os.path.dirname(PASSKEY_FILE), mode=0o700, exist_ok=True)
        tmp = PASSKEY_FILE + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(items, f)
        os.replace(tmp, PASSKEY_FILE)

    def challenge(self, kind):
        now = time.monotonic()
        with self.lock:
            for k in [k for k, (e, _) in self.challenges.items() if e < now]:
                del self.challenges[k]
            while len(self.challenges) >= 100:
                del self.challenges[next(iter(self.challenges))]
            ch = b64u(secrets.token_bytes(32))
            self.challenges[ch] = (now + 120, kind)
            return ch

    def take(self, ch, kind):
        with self.lock:
            e, k = self.challenges.pop(ch, (0, None))
            return k == kind and e >= time.monotonic()

    def auth_options(self, rp):
        items = self.load()
        if not (WEBAUTHN and items and AUTH.enabled()):
            return None
        return {
            "challenge": self.challenge("auth"),
            "rpId": rp,
            "timeout": 60000,
            "allowCredentials": [{"id": c["id"]} for c in items],
        }


PK = Passkeys()


def wa_client_data(raw, want_type, kind, origin):
    cd = json.loads(raw)
    if cd.get("type") != want_type or cd.get("origin") != origin or not PK.take(cd.get("challenge", ""), kind):
        raise ValueError("bad client data")


def wa_pubkey(x, y):
    return ec.EllipticCurvePublicNumbers(int.from_bytes(x, "big"), int.from_bytes(y, "big"), ec.SECP256R1()).public_key()


def wa_register(d, rp, origin):
    r = d["response"]
    wa_client_data(unb64u(r["clientDataJSON"]), "webauthn.create", "reg", origin)
    ad = cbor(unb64u(r["attestationObject"]))[0]["authData"]
    if ad[:32] != hashlib.sha256(rp.encode()).digest() or ad[32] & 0x45 != 0x45:
        raise ValueError("bad authData")
    n = int.from_bytes(ad[53:55], "big")
    cid = ad[55:55 + n]
    key = cbor(ad, 55 + n)[0]
    if key.get(1) != 2 or key.get(3) != -7 or key.get(-1) != 1 or len(key.get(-2, b"")) != 32 or len(key.get(-3, b"")) != 32:
        raise ValueError("unsupported key")
    wa_pubkey(key[-2], key[-3])
    if d.get("id") != b64u(cid):
        raise ValueError("id mismatch")
    name = re.sub(r"[^\w .-]", "", str(d.get("name", "device")))[:40] or "device"
    with PK.lock:
        items = [c for c in PK.load() if c["id"] != b64u(cid)]
        items.append({
            "id": b64u(cid),
            "x": b64u(key[-2]),
            "y": b64u(key[-3]),
            "count": int.from_bytes(ad[33:37], "big"),
            "name": name,
            "created": int(time.time()),
        })
        PK.save(items)


def wa_login(d, rp, origin):
    r = d["response"]
    with PK.lock:
        items = PK.load()
    cred = next((c for c in items if c["id"] == d.get("id")), None)
    if not cred:
        raise ValueError("unknown credential")
    raw = unb64u(r["clientDataJSON"])
    wa_client_data(raw, "webauthn.get", "auth", origin)
    ad = unb64u(r["authenticatorData"])
    if ad[:32] != hashlib.sha256(rp.encode()).digest() or ad[32] & 0x05 != 0x05:
        raise ValueError("bad authData")
    wa_pubkey(unb64u(cred["x"]), unb64u(cred["y"])).verify(
        unb64u(r["signature"]), ad + hashlib.sha256(raw).digest(), ec.ECDSA(hashes.SHA256())
    )
    n = int.from_bytes(ad[33:37], "big")
    if n or cred["count"]:
        if n <= cred["count"]:
            raise ValueError("replayed or cloned authenticator")
        with PK.lock:
            items = PK.load()
            for c in items:
                if c["id"] == cred["id"]:
                    c["count"] = n
            PK.save(items)
