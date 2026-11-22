#!/usr/bin/env python3
"""Touch control panel for niri. Serves web UI, virtual trackpad/keyboard, and live niri IPC."""
import base64, glob, hashlib, http.cookies, json, os, re, struct, subprocess, sys, threading, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from auth import AUTH, PIN_FILE, PK, WEBAUTHN, b64u, set_pin, wa_login, wa_register
from uinput import INPUT

ACTIONS = {
    # Apps & system
    "launcher": ["spawn", "--", "sh", "-c", "if command -v dms >/dev/null; then dms ipc call spotlight toggle; elif command -v fuzzel >/dev/null; then fuzzel; else wofi --show drun; fi"],
    "terminal": ["spawn", "--", "sh", "-c", "for t in kitty foot alacritty ghostty wezterm; do command -v $t >/dev/null && exec $t; done"],
    "overview": ["toggle-overview"],
    "power-off": ["power-off-monitors"],
    "power-on": ["power-on-monitors"],
    "screenshot": ["screenshot-screen"],
    # Focus navigation
    "col-left": ["focus-column-left"],
    "col-right": ["focus-column-right"],
    "col-first": ["focus-column-first"],
    "col-last": ["focus-column-last"],
    "win-up": ["focus-window-up"],
    "win-down": ["focus-window-down"],
    "ws-up": ["focus-workspace-up"],
    "ws-down": ["focus-workspace-down"],
    "mon-next": ["focus-monitor-next"],
    # Move window / column
    "mv-col-left": ["move-column-left"],
    "mv-col-right": ["move-column-right"],
    "mv-win-up": ["move-window-up"],
    "mv-win-down": ["move-window-down"],
    "mv-ws-up": ["move-column-to-workspace-up"],
    "mv-ws-down": ["move-column-to-workspace-down"],
    "mv-mon-next": ["move-column-to-monitor-next"],
    "expel-left": ["consume-or-expel-window-left"],
    "expel-right": ["consume-or-expel-window-right"],
    # Column & window layout
    "width-cycle": ["switch-preset-column-width"],
    "center": ["center-column"],
    "tabbed": ["toggle-column-tabbed-display"],
    "expand": ["expand-column-to-available-width"],
    "maximize": ["maximize-column"],
    "fullscreen": ["fullscreen-window"],
    "float": ["toggle-window-floating"],
    "close": ["close-window"],
    # Audio & media
    "vol-up": ["spawn", "--", "sh", "-c", "if command -v dms >/dev/null; then dms ipc call audio increment 5; else wpctl set-volume -l 1.5 @DEFAULT_AUDIO_SINK@ 5%+; fi"],
    "vol-down": ["spawn", "--", "sh", "-c", "if command -v dms >/dev/null; then dms ipc call audio decrement 5; else wpctl set-volume @DEFAULT_AUDIO_SINK@ 5%-; fi"],
    "vol-mute": ["spawn", "--", "sh", "-c", "if command -v dms >/dev/null; then dms ipc call audio mute; else wpctl set-mute @DEFAULT_AUDIO_SINK@ toggle; fi"],
    "media-play": ["spawn", "--", "sh", "-c", "if command -v dms >/dev/null; then dms ipc call mpris playPause; elif command -v playerctl >/dev/null; then playerctl play-pause; fi"],
    "media-prev": ["spawn", "--", "sh", "-c", "if command -v dms >/dev/null; then dms ipc call mpris previous; elif command -v playerctl >/dev/null; then playerctl previous; fi"],
    "media-next": ["spawn", "--", "sh", "-c", "if command -v dms >/dev/null; then dms ipc call mpris next; elif command -v playerctl >/dev/null; then playerctl next; fi"],
}
for _n in range(1, 10):
    ACTIONS[f"ws-{_n}"] = ["focus-workspace", str(_n)]

TITLE = os.environ.get("PANEL_TITLE", "niri")
NAME = os.environ.get("PANEL_NAME", TITLE)
PORT = int(os.environ.get("PANEL_PORT", "8765"))
FEATURES = {f.strip() for f in os.environ.get("PANEL_FEATURES", "scale").split(",") if f.strip()}

HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST = json.dumps({
    "name": NAME, "short_name": TITLE, "start_url": "/", "scope": "/",
    "display": "standalone", "background_color": "#111111", "theme_color": "#111111",
    "icons": [{"src": f"/icon-{n}.png", "sizes": f"{n}x{n}", "type": "image/png", "purpose": "any maskable"} for n in (192, 512)],
})

SCALE_HTML = """<h5>Screen scale <span id=scl></span></h5>
<div class=g id=scout style="margin-bottom:8px;display:none"></div>
<div class=g id=scg></div>
<div class=step style="margin-top:8px"><button id=scminus>&minus;</button><span>step 0.25</span><button id=scplus>+</button></div>"""


def _load_template(rel_path):
    with open(os.path.join(HERE, rel_path), encoding="utf-8") as f:
        return f.read()


PAGE = (
    _load_template("static/index.html")
    .replace("@@TITLE@@", TITLE)
    .replace("@@SCALE@@", SCALE_HTML if "scale" in FEATURES else "")
)
LOGIN_PAGE = _load_template("static/login.html").replace("@@TITLE@@", TITLE)


def niri_env():
    env = dict(os.environ)
    if "NIRI_SOCKET" not in env or not os.path.exists(env["NIRI_SOCKET"]):
        socks = sorted(glob.glob(f"/run/user/{os.getuid()}/niri*.sock"), key=os.path.getmtime, reverse=True)
        if socks:
            env["NIRI_SOCKET"] = socks[0]
    return env


def niri_json(*args):
    out = subprocess.run(["niri", "msg", "--json", *args], env=niri_env(), capture_output=True, text=True, timeout=3)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "niri msg failed")
    return json.loads(out.stdout)


def run_niri_action(*action_args):
    def _exec():
        try:
            subprocess.run(
                ["niri", "msg", "action", *action_args],
                env=niri_env(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except Exception:
            pass
    threading.Thread(target=_exec, daemon=True).start()


def scale_info(preferred=None):
    """Returns active scale target and all enabled outputs."""
    raw = niri_json("outputs")
    outs = {k: v for k, v in raw.items() if v.get("logical")}
    if not outs:
        raise RuntimeError("no enabled outputs")
    if preferred and preferred in outs:
        name = preferred
    elif len(outs) == 1:
        name = next(iter(outs))
    else:
        foc = (niri_json("focused-output") or {}).get("name")
        name = foc if foc in outs else next(iter(outs))
    all_list = [{"name": k, "scale": v["logical"]["scale"]} for k, v in sorted(outs.items())]
    return {"output": name, "scale": outs[name]["logical"]["scale"], "outputs": all_list}


def set_scale(v, preferred=None):
    info = scale_info(preferred)
    name = info["output"]
    subprocess.run(
        ["niri", "msg", "output", name, "scale", str(v)],
        env=niri_env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=5,
    )
    return scale_info(name)


def niri_state():
    ws = niri_json("workspaces") or []
    wins = niri_json("windows") or []
    foc = niri_json("focused-window")
    ws.sort(key=lambda w: (w.get("output") or "", w.get("idx") or 0))
    wins.sort(key=lambda w: (not w.get("is_focused", False), w.get("workspace_id") or 0, w.get("id") or 0))
    return {
        "focused_window": (
            {"id": foc.get("id"), "title": foc.get("title") or "", "app_id": foc.get("app_id") or ""}
            if isinstance(foc, dict) else None
        ),
        "workspaces": [
            {
                "id": w.get("id"),
                "idx": w.get("idx"),
                "name": w.get("name"),
                "output": w.get("output"),
                "is_focused": bool(w.get("is_focused")),
                "is_active": bool(w.get("is_active")),
            }
            for w in ws
        ],
        "windows": [
            {
                "id": w.get("id"),
                "title": w.get("title") or "",
                "app_id": w.get("app_id") or "",
                "workspace_id": w.get("workspace_id"),
                "is_focused": bool(w.get("is_focused")),
            }
            for w in wins
        ],
    }


def handle_input_payload(d):
    """Dispatches mouse or keyboard events received via WebSocket or HTTP POST."""
    if "key" in d:
        mods = d.get("mods") if isinstance(d.get("mods"), list) else ()
        INPUT.key(d["key"], mods)
        return
    if "text" in d:
        INPUT.type_text(d["text"])
        return
    c = lambda k: max(-200, min(200, int(d.get(k, 0))))
    INPUT.send(c("dx"), c("dy"), c("sx"), c("sy"), d.get("btn"), d.get("down"))


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def handle(self):
        try:
            super().handle()
        except ConnectionError:
            pass

    def _sid(self):
        try:
            c = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
            return c["sid"].value if "sid" in c else ""
        except http.cookies.CookieError:
            return ""

    def _rp(self):
        host = self.headers.get("Host", "").strip().lower()
        rp = host.rsplit(":", 1)[0] if re.search(r":\d+$", host) else host
        return (rp, "https://" + host) if re.fullmatch(r"[a-z0-9.-]+", rp) else (None, None)

    def _session_ok(self, sid):
        data = b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Set-Cookie", self._cookie(sid, 30 * 86400))
        self.end_headers()
        self.wfile.write(data)

    def _empty(self, code):
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _cookie(self, sid, max_age):
        flags = "; Secure" if self.headers.get("X-Forwarded-Proto", "") == "https" else ""
        return "sid=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax%s" % (sid, max_age, flags)

    def _json(self, fn, code=200):
        try:
            data = json.dumps(fn()).encode()
        except Exception:
            data, code = b"{}", 500
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == "/manifest.webmanifest":
            data = MANIFEST.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/manifest+json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            return self.wfile.write(data)
        if path in ("/icon-192.png", "/icon-512.png"):
            icon_path = os.path.join(HERE, "icons", path[1:])
            if not os.path.exists(icon_path):
                icon_path = os.path.join(HERE, path[1:])
            with open(icon_path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            return self.wfile.write(data)
        if path == "/auth":
            return self._empty(204 if not AUTH.enabled() or AUTH.peek(self._sid()) else 401)

        authed = AUTH.check(self._sid())
        if path == "/ws" and self.headers.get("Upgrade", "").lower() == "websocket":
            return self._ws() if authed else self._empty(401)
        if path == "/webauthn/list":
            if not authed:
                return self._empty(401)
            return self._json(lambda: {"items": [{k: c[k] for k in ("id", "name", "created")} for c in PK.load()]})
        if path == "/state":
            if not authed:
                return self._empty(401)
            return self._json(niri_state)
        if path == "/scale" and "scale" in FEATURES:
            if not authed:
                return self._empty(401)
            out_arg = (qs.get("output") or [None])[0]
            return self._json(lambda: scale_info(out_arg))

        if authed:
            fp = (
                '<h5>Fingerprint unlock</h5><div id=fp></div><div id=fpm style="color:#889;font-size:13px;margin:6px 2px"></div>'
                if AUTH.enabled() and WEBAUTHN else ""
            )
            lockbtn = fp + (
                '<h5>Session</h5><div class=g style="grid-template-columns:1fr"><button class=x id=lockbtn>Lock now</button></div>'
                if AUTH.enabled() else ""
            )
            data = PAGE.replace("@@LOCKBTN@@", lockbtn).encode()
        else:
            rp = self._rp()[0]
            wa_opts = PK.auth_options(rp) if rp else None
            data = (
                LOGIN_PAGE
                .replace("@@LOCK@@", str(AUTH.remaining()))
                .replace("@@WA@@", json.dumps(wa_opts).replace("<", "\\u003c"))
            ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _ws(self):
        sid = self._sid()
        key = self.headers.get("Sec-WebSocket-Key", "")
        acc = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", acc)
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True
        r = self.rfile

        def rd(n):
            b = r.read(n)
            if len(b) < n:
                raise EOFError
            return b

        try:
            while True:
                b0, b1 = rd(2)
                op, ln = b0 & 15, b1 & 127
                if ln == 126:
                    ln = struct.unpack(">H", rd(2))[0]
                elif ln == 127:
                    ln = struct.unpack(">Q", rd(8))[0]
                if ln > 4096:
                    break
                mask = rd(4) if b1 & 128 else b"\0\0\0\0"
                data = bytes(c ^ mask[i % 4] for i, c in enumerate(rd(ln)))
                if op == 8:
                    break
                if op == 9:
                    self.wfile.write(bytes([0x8A, len(data)]) + data)
                    self.wfile.flush()
                elif op == 1:
                    if not AUTH.check(sid):
                        break
                    try:
                        handle_input_payload(json.loads(data))
                    except Exception:
                        pass
        except (EOFError, OSError):
            pass
        INPUT.release_all()

    def do_POST(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        n = int(self.headers.get("Content-Length") or 0)
        if n > 65536:
            self.close_connection = True
            return self._empty(413)
        body = self.rfile.read(n) if n else b""

        if path == "/login":
            try:
                pin = str(json.loads(body).get("pin", ""))
            except Exception:
                pin = ""
            if not re.fullmatch(r"\d{6}", pin):
                return self._empty(400)
            code, payload, sid = AUTH.login(pin)
            data = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            if sid:
                self.send_header("Set-Cookie", self._cookie(sid, 30 * 86400))
            self.end_headers()
            return self.wfile.write(data)

        if path == "/logout":
            AUTH.logout(self._sid())
            self.send_response(204)
            self.send_header("Set-Cookie", self._cookie("", 0))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        rp, origin = self._rp()
        if path.startswith("/webauthn/auth/") and WEBAUTHN and rp:
            if path.endswith("/begin"):
                opts = PK.auth_options(rp)
                return self._json(lambda: opts) if opts else self._empty(404)
            try:
                wa_login(json.loads(body), rp, origin)
            except Exception:
                return self._empty(401)
            return self._session_ok(AUTH.create_session())

        if not AUTH.check(self._sid()):
            return self._empty(401)

        if path.startswith("/webauthn/") and WEBAUTHN and rp:
            if path == "/webauthn/reg/begin":
                return self._json(lambda: {
                    "challenge": PK.challenge("reg"), "timeout": 60000, "attestation": "none",
                    "rp": {"id": rp, "name": TITLE},
                    "user": {"id": b64u(b"niri-remote"), "name": "owner", "displayName": TITLE},
                    "pubKeyCredParams": [{"type": "public-key", "alg": -7}],
                    "authenticatorSelection": {"authenticatorAttachment": "platform", "userVerification": "required", "residentKey": "preferred"},
                    "excludeCredentials": [{"id": c["id"]} for c in PK.load()],
                })
            try:
                if path == "/webauthn/reg/finish":
                    wa_register(json.loads(body), rp, origin)
                elif path == "/webauthn/remove":
                    cid = json.loads(body).get("id")
                    with PK.lock:
                        PK.save([c for c in PK.load() if c["id"] != cid])
                else:
                    return self._empty(404)
            except Exception:
                return self._empty(400)
            return self._empty(204)

        if path in ("/m", "/key"):
            try:
                handle_input_payload(json.loads(body))
                code = 204
            except Exception:
                code = 400
            return self._empty(code)

        if path.startswith("/scale/") and "scale" in FEATURES:
            try:
                v = round(float(path.removeprefix("/scale/")), 2)
                if not 0.5 <= v <= 4:
                    raise ValueError
            except ValueError:
                return self._empty(400)
            out_arg = (qs.get("output") or [None])[0]
            return self._json(lambda: set_scale(v, out_arg))

        if path.startswith("/focus-window/"):
            wid = path.removeprefix("/focus-window/")
            if not re.fullmatch(r"\d+", wid):
                return self._empty(400)
            run_niri_action("focus-window", "--id", wid)
            return self._empty(204)

        if path.startswith("/focus-workspace-idx/"):
            idx = path.removeprefix("/focus-workspace-idx/")
            if not re.fullmatch(r"\d+", idx):
                return self._empty(400)
            run_niri_action("focus-workspace", idx)
            return self._empty(204)

        name = path.removeprefix("/do/")
        if name in ACTIONS:
            run_niri_action(*ACTIONS[name])
            return self._empty(204)
        return self._empty(404)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    if "--set-pin" in sys.argv:
        sys.exit(set_pin())
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    host = args[0] if args else os.environ.get("PANEL_BIND", "127.0.0.1")
    port = int(args[1]) if len(args) > 1 else PORT
    if not os.path.exists(PIN_FILE):
        print("warning: no PIN set, the panel is open to the network. Run: panel.py --set-pin", file=sys.stderr, flush=True)
    ThreadingHTTPServer((host, port), H).serve_forever()
