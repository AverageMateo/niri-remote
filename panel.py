#!/usr/bin/env python3
"""Touch control panel for niri. Serves buttons that run `niri msg action ...`."""
import base64, fcntl, glob, hashlib, hmac, http.cookies, re, secrets, json, os, struct, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ACTIONS = {
    "launcher": ["spawn", "--", "sh", "-c", "if command -v dms >/dev/null; then dms ipc call spotlight toggle; elif command -v fuzzel >/dev/null; then fuzzel; else wofi --show drun; fi"],
    "terminal": ["spawn", "--", "sh", "-c", "for t in kitty foot alacritty ghostty wezterm; do command -v $t >/dev/null && exec $t; done"],
    "overview": ["toggle-overview"],
    "col-left": ["focus-column-left"],
    "col-right": ["focus-column-right"],
    "win-up": ["focus-window-up"],
    "win-down": ["focus-window-down"],
    "ws-up": ["focus-workspace-up"],
    "ws-down": ["focus-workspace-down"],
    "mv-col-left": ["move-column-left"],
    "mv-col-right": ["move-column-right"],
    "mv-win-up": ["move-window-up"],
    "mv-win-down": ["move-window-down"],
    "mv-ws-up": ["move-column-to-workspace-up"],
    "mv-ws-down": ["move-column-to-workspace-down"],
    "expel-left": ["consume-or-expel-window-left"],
    "expel-right": ["consume-or-expel-window-right"],
    "maximize": ["maximize-column"],
    "fullscreen": ["fullscreen-window"],
    "float": ["toggle-window-floating"],
    "close": ["close-window"],
}
for n in range(1, 10):
    ACTIONS[f"ws-{n}"] = ["focus-workspace", str(n)]

class Mouse:
    """Virtual relative mouse via /dev/uinput (no daemon, no extra packages)."""
    EV_SYN, EV_KEY, EV_REL = 0, 1, 2
    REL_X, REL_Y, REL_HWHEEL, REL_WHEEL = 0, 1, 6, 8
    BTN = {"l": 0x110, "r": 0x111, "m": 0x112}
    def __init__(self):
        self.fd = None
        self.lock = threading.Lock()
    def _open(self):
        fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        for req, val in ((0x40045564, self.EV_KEY), (0x40045564, self.EV_REL),
                         *((0x40045565, b) for b in self.BTN.values()),
                         *((0x40045566, r) for r in (self.REL_X, self.REL_Y, self.REL_HWHEEL, self.REL_WHEEL))):
            fcntl.ioctl(fd, req, val)
        os.write(fd, struct.pack("80sHHHHi" + "i" * 256, b"niri-remote mouse", 3, 0x1234, 0x5678, 1, 0, *([0] * 256)))
        fcntl.ioctl(fd, 0x5501)  # UI_DEV_CREATE
        time.sleep(0.3)  # let niri/libinput pick the new device up
        self.fd = fd
    def _emit(self, *events):
        t = time.time()
        buf = b"".join(struct.pack("llHHi", int(t), int(t % 1 * 1e6), ty, c, v) for ty, c, v in events)
        os.write(self.fd, buf + struct.pack("llHHi", int(t), int(t % 1 * 1e6), self.EV_SYN, 0, 0))
    def send(self, dx=0, dy=0, sx=0, sy=0, btn=None, down=None):
        with self.lock:
            if self.fd is None:
                self._open()
            ev = []
            if dx: ev.append((self.EV_REL, self.REL_X, dx))
            if dy: ev.append((self.EV_REL, self.REL_Y, dy))
            if sy: ev.append((self.EV_REL, self.REL_WHEEL, sy))
            if sx: ev.append((self.EV_REL, self.REL_HWHEEL, sx))
            if btn in self.BTN and down in (0, 1): ev.append((self.EV_KEY, self.BTN[btn], down))
            if ev: self._emit(*ev)

MOUSE = Mouse()

TITLE = os.environ.get("PANEL_TITLE", "niri")
NAME = os.environ.get("PANEL_NAME", TITLE)
FEATURES = {f.strip() for f in os.environ.get("PANEL_FEATURES", "scale").split(",")}

SCALE_HTML = """<h5>Screen scale <span id=scl></span></h5><div class=g id=scg></div>
<div class=step style="margin-top:8px"><button id=scminus>&minus;</button><span>step 0.25</span><button id=scplus>+</button></div>"""

PAGE = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>@@TITLE@@</title><link rel=manifest href=/manifest.webmanifest><meta name=theme-color content="#111111"><link rel=icon href=/icon-192.png><style>
body{background:#111;color:#ddd;font:16px sans-serif;margin:8px}
h4{margin:14px 4px 4px;color:#889;font-size:12px}
.g{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
.g2{grid-template-columns:repeat(2,1fr)}
button{background:#2a2d33;color:#ddd;border:0;border-radius:8px;padding:18px 0;font-size:16px}
button:active{background:#4a6fa5}.x{background:#8b2a2a}
#tp{display:none;position:fixed;inset:0;background:#111;flex-direction:column;padding:8px;gap:8px;z-index:9}
#pad{flex:1;background:#1b1d22;border:1px solid #333;border-radius:12px;touch-action:none;display:flex;align-items:center;justify-content:center;color:#445;font-size:13px;text-align:center;user-select:none;-webkit-user-select:none}
#tp .row{display:flex;gap:8px}#tp .row button{flex:1;padding:22px 0}
#tp .top button{padding:12px 0}#tp .on{background:#4a6fa5}
#tp input{flex:1.4}
details{margin-top:14px}summary{color:#889;font-size:12px;margin:0 4px 6px;padding:8px 0}
.cfg{background:#1b1d22;border-radius:10px;padding:10px;margin-bottom:12px}.cfg h5{margin:8px 2px;color:#889;font-size:12px;font-weight:400}
.step{display:flex;gap:8px;align-items:center}.step button{flex:0 0 25vw;padding:14px 0}.step span{flex:1;text-align:center}
.on{background:#4a6fa5}
</style><body>
<div class=g style="grid-template-columns:1fr"><button id=tpopen>Trackpad</button></div>
<div id=tp><div class="row top"><button id=tpclose class=x>Close</button><button id=tpdrag>Drag lock</button><input id=tpsens type=range min=0.5 max=12 step=0.1 value=3></div>
<div id=pad>1 finger: move &middot; tap: click<br>2 fingers: scroll &middot; 2-finger tap: right click</div>
<div class=row><button id=bl>Left</button><button id=bm>Mid</button><button id=br>Right</button></div></div>
<h4>APPS</h4><div class=g>
<button data=launcher>Launcher</button><button data=terminal>Terminal</button><button data=overview>Overview</button></div>
<h4>MOVE</h4><div class="g g2">
<button data=col-left>&#9664; Column</button><button data=col-right>Column &#9654;</button>
<button data=win-up>&#9650; Window</button><button data=win-down>Window &#9660;</button>
<button data=ws-up>&#9650; Workspace</button><button data=ws-down>Workspace &#9660;</button>
<h4>MOVE WINDOW</h4><div class="g g2">
<button data=mv-col-left>&#9664; Column</button><button data=mv-col-right>Column &#9654;</button>
<button data=mv-win-up>&#9650; In column</button><button data=mv-win-down>In column &#9660;</button>
<button data=mv-ws-up>&#9650; To workspace</button><button data=mv-ws-down>To workspace &#9660;</button>
<button data=expel-left>&#9664; Join/split</button><button data=expel-right>Join/split &#9654;</button></div>
<h4>WORKSPACES</h4><div class=g id=wsg></div>
<h4>WINDOW</h4><div class=g>
<button data=maximize>Maximize</button><button data=fullscreen>Fullscreen</button><button data=float>Float</button></div>
<div class=g style="margin-top:8px;grid-template-columns:1fr"><button class=x data=close>Close</button></div>
<details><summary>CONFIGURATION</summary><div class=cfg>
<h5>Workspace buttons shown</h5><div class=step><button id=wsminus>&minus;</button><span id=wsn></span><button id=wsplus>+</button></div>
%s@@LOCKBTN@@</div></details>
<script>{const f=window.fetch;window.fetch=async(...a)=>{const r=await f(...a);if(r.status===401)location.reload();return r}}
const lb=document.getElementById('lockbtn');if(lb)lb.onclick=async()=>{await fetch('/logout',{method:'POST'});location.reload()};
document.querySelectorAll('button[data]').forEach(b=>b.onclick=()=>{navigator.vibrate&&navigator.vibrate(15);fetch('/do/'+b.getAttribute('data'),{method:'POST'})})</script>
<script>
(()=>{const $=i=>document.getElementById(i),tp=$('tp'),pad=$('pad');
let sens=+(localStorage.tpsens||3);$('tpsens').value=sens;$('tpsens').oninput=e=>{sens=+e.target.value;try{localStorage.tpsens=sens}catch(_){}};
let wl=null,ws=null,q=[];
function conn(){ws=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/ws');ws.onopen=()=>{q.forEach(m=>ws.send(m));q=[]};ws.onclose=()=>{ws=null;fetch('/auth').then(r=>{if(r.ok)setTimeout(()=>{if(tp.style.display==='flex')conn()},500)})}}
function post(o){const m=JSON.stringify(o);if(ws&&ws.readyState===1)ws.send(m);else{if(!ws)conn();if(o.btn)q.push(m)}}
let acc={dx:0,dy:0,sx:0,sy:0};
function flush(){const a=acc;if(!(a.dx||a.dy||a.sx||a.sy))return;acc={dx:0,dy:0,sx:0,sy:0};post(a)}
function btn(b,d){post({btn:b,down:d})}
function click(b){btn(b,1);setTimeout(()=>btn(b,0),30);navigator.vibrate&&navigator.vibrate(10)}
$('tpopen').onclick=async()=>{tp.style.display='flex';if(!ws)conn();try{await tp.requestFullscreen()}catch(_){}try{wl=await navigator.wakeLock.request('screen')}catch(_){}};
$('tpclose').onclick=()=>{if(drag){$('tpdrag').click()}tp.style.display='none';try{document.exitFullscreen()}catch(_){}try{wl&&wl.release()}catch(_){}};
let drag=false;$('tpdrag').onclick=e=>{drag=!drag;e.target.classList.toggle('on',drag);btn('l',drag?1:0)};
for(const [id,b] of [['bl','l'],['bm','m'],['br','r']]){const el=$(id);
 el.ontouchstart=e=>{e.preventDefault();btn(b,1);el.classList.add('on')};
 el.ontouchend=el.ontouchcancel=e=>{e.preventDefault();btn(b,0);el.classList.remove('on')}}
let t0=0,moved=0,maxN=0,last=null,rem={x:0,y:0};
const pt=e=>{const t=[...e.touches];return {n:t.length,x:t.reduce((s,a)=>s+a.clientX,0)/t.length,y:t.reduce((s,a)=>s+a.clientY,0)/t.length}};
pad.ontouchstart=e=>{e.preventDefault();const p=pt(e);if(e.touches.length===1){t0=Date.now();moved=0;maxN=1}maxN=Math.max(maxN,p.n);last=p};
pad.ontouchmove=e=>{e.preventDefault();const p=pt(e);if(!last||p.n!==last.n){last=p;return}
 const dx=p.x-last.x,dy=p.y-last.y;moved+=Math.abs(dx)+Math.abs(dy);last=p;
 if(p.n===1){const g=sens*(1+Math.min(Math.hypot(dx,dy),40)/20);rem.x+=dx*g;rem.y+=dy*g;
  const ix=Math.trunc(rem.x),iy=Math.trunc(rem.y);rem.x-=ix;rem.y-=iy;acc.dx+=ix;acc.dy+=iy}
 else{rem.x+=dx/12;rem.y+=dy/12;const ix=Math.trunc(rem.x),iy=Math.trunc(rem.y);rem.x-=ix;rem.y-=iy;acc.sx+=ix;acc.sy-=iy}
 flush()};
pad.ontouchend=pad.ontouchcancel=e=>{e.preventDefault();if(e.touches.length===0){
 if(Date.now()-t0<220&&moved<10){click(maxN>=2?'r':'l')}last=null}else last=pt(e)};
})();
</script>
<script>
(()=>{const $=i=>document.getElementById(i);
let n=5;try{n=Math.min(9,Math.max(1,+localStorage.wsn||5))}catch(_){}
function ws(){$('wsn').textContent=n;const g=$('wsg');g.innerHTML='';g.style.gridTemplateColumns='repeat('+(n<=5?n:n===6||n===9?3:4)+',1fr)';
 for(let i=1;i<=n;i++){const b=document.createElement('button');b.textContent=i;b.onclick=()=>{navigator.vibrate&&navigator.vibrate(15);fetch('/do/ws-'+i,{method:'POST'})};g.appendChild(b)}}
function setn(v){n=Math.min(9,Math.max(1,v));try{localStorage.wsn=n}catch(_){}ws()}
$('wsminus').onclick=()=>setn(n-1);$('wsplus').onclick=()=>setn(n+1);ws();
if(!$('scg'))return;
let cur=1;const P=[1,1.25,1.5,1.75,2,2.5,3];
function show(){$('scl').textContent='('+cur+')';const g=$('scg');g.innerHTML='';g.style.gridTemplateColumns='repeat(4,1fr)';
 for(const v of P){const b=document.createElement('button');b.textContent=v;if(Math.abs(v-cur)<0.01)b.className='on';b.onclick=()=>set(v);g.appendChild(b)}}
async function load(){try{const r=await fetch('/scale');if(r.ok){cur=(await r.json()).scale;show()}}catch(_){}}
async function set(v){v=Math.round(Math.min(4,Math.max(0.5,v))*100)/100;try{const r=await fetch('/scale/'+v,{method:'POST'});if(r.ok){cur=(await r.json()).scale}}catch(_){}show()}
$('scminus').onclick=()=>set(cur-0.25);$('scplus').onclick=()=>set(cur+0.25);
document.querySelector('details').ontoggle=load;show();
})();
</script>
<script>
(async()=>{const box=document.getElementById('fp');if(!box)return;const msg=document.getElementById('fpm');
const b2u=b=>btoa(String.fromCharCode(...new Uint8Array(b))).replace(/\\+/g,'-').replace(/\\//g,'_').replace(/=+$/,'');
const u2b=s=>Uint8Array.from(atob(s.replace(/-/g,'+').replace(/_/g,'/')),c=>c.charCodeAt(0));
const dev=()=>/Android/.test(navigator.userAgent)?'Android':/iPhone|iPad/.test(navigator.userAgent)?'iOS':/Linux/.test(navigator.userAgent)?'Linux':'Device';
async function load(){const j=await (await fetch('/webauthn/list')).json();box.innerHTML='';
 for(const it of j.items){const r=document.createElement('div');r.className='step';r.style.marginBottom='8px';
  const sp=document.createElement('span');sp.textContent=it.name+' ('+new Date(it.created*1000).toLocaleDateString()+')';
  const b=document.createElement('button');b.textContent='Remove';b.className='x';b.onclick=async()=>{await fetch('/webauthn/remove',{method:'POST',body:JSON.stringify({id:it.id})});load()};
  r.append(sp,b);box.appendChild(r)}
 const a=document.createElement('button');a.textContent='Add this device';a.style.width='100%%';a.onclick=add;box.appendChild(a)}
async function add(){msg.textContent='';
 try{const o=await (await fetch('/webauthn/reg/begin',{method:'POST'})).json();
  const c=await navigator.credentials.create({publicKey:{challenge:u2b(o.challenge),rp:o.rp,user:{id:u2b(o.user.id),name:o.user.name,displayName:o.user.displayName},
   pubKeyCredParams:o.pubKeyCredParams,authenticatorSelection:o.authenticatorSelection,timeout:o.timeout,attestation:'none',
   excludeCredentials:o.excludeCredentials.map(x=>({type:'public-key',id:u2b(x.id)}))}});
  const r=await fetch('/webauthn/reg/finish',{method:'POST',body:JSON.stringify({id:c.id,name:dev(),response:{clientDataJSON:b2u(c.response.clientDataJSON),attestationObject:b2u(c.response.attestationObject)}})});
  msg.textContent=r.ok?'Added. Next time you open the panel it asks for your fingerprint.':'The server rejected it';load()
 }catch(e){msg.textContent='Could not add: '+(e.name||e)}}
if(window.PublicKeyCredential)load();else box.textContent='This browser does not support fingerprint unlock here (needs HTTPS).'})();
</script>
""" % (SCALE_HTML if "scale" in FEATURES else "")
PAGE = PAGE.replace("@@TITLE@@", TITLE)

def niri_env():
    env = dict(os.environ)
    if "NIRI_SOCKET" not in env:
        socks = glob.glob(f"/run/user/{os.getuid()}/niri*.sock")
        if socks:
            env["NIRI_SOCKET"] = socks[0]
    return env

def niri_json(*args):
    out = subprocess.run(["niri", "msg", "--json", *args], env=niri_env(), capture_output=True, text=True, timeout=3)
    return json.loads(out.stdout)

def scale_target():
    """The output the scale applies to: the only enabled one, else the focused one."""
    outs = {k: v for k, v in niri_json("outputs").items() if v.get("logical")}
    if len(outs) == 1:
        name = next(iter(outs))
    else:
        name = niri_json("focused-output")["name"]
    return name, outs[name]["logical"]["scale"]

def set_scale(v):
    name, _ = scale_target()
    subprocess.run(["niri", "msg", "output", name, "scale", str(v)], env=niri_env(),
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    return scale_target()

def run(name):
    env = dict(os.environ)
    if "NIRI_SOCKET" not in env:
        socks = glob.glob(f"/run/user/{os.getuid()}/niri*.sock")
        if socks:
            env["NIRI_SOCKET"] = socks[0]
    subprocess.Popen(["niri", "msg", "action", *ACTIONS[name]], env=env,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST = json.dumps({
    "name": NAME, "short_name": TITLE, "start_url": "/", "scope": "/",
    "display": "standalone", "background_color": "#111111", "theme_color": "#111111",
    "icons": [{"src": f"/icon-{n}.png", "sizes": f"{n}x{n}", "type": "image/png", "purpose": "any maskable"} for n in (192, 512)],
})

# ---- PIN authentication -------------------------------------------------------------------
# Active only while the PIN file exists (create it with: panel.py --set-pin).
# Sessions and lockouts live in memory: restarting the service clears both.
PIN_FILE = os.environ.get("PANEL_PIN_FILE", os.path.expanduser("~/.config/niri-remote/pin"))
IDLE = int(os.environ.get("PANEL_PIN_TIMEOUT", "600"))   # seconds without use before the PIN is asked again
MAX_TRIES = 5
LOCKS = [600, 3600, 21600, 86400]                         # lockout after 5 failures: 10m, 1h, 6h, then 24h

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
            algo, it, salt, want = open(PIN_FILE).read().strip().split("$")
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

# ---- WebAuthn: fingerprint / face unlock -------------------------------------------------
# Needs the python "cryptography" package (Arch: python-cryptography). Without it the PIN still works.
PASSKEY_FILE = os.environ.get("PANEL_PASSKEY_FILE", os.path.expanduser("~/.config/niri-remote/passkeys.json"))
try:
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import hashes
    WEBAUTHN = True
except ImportError:
    WEBAUTHN = False

def b64u(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
def unb64u(s): return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

def cbor(b, i=0):
    """Minimal CBOR decoder (definite lengths only): enough for WebAuthn attestation data."""
    mt, ai = b[i] >> 5, b[i] & 31
    i += 1
    if ai < 24: n = ai
    elif ai in (24, 25, 26, 27):
        w = 1 << (ai - 24)
        n = int.from_bytes(b[i:i + w], "big"); i += w
    else: raise ValueError("unsupported cbor")
    if mt == 0: return n, i
    if mt == 1: return -1 - n, i
    if mt in (2, 3):
        if i + n > len(b): raise ValueError("short cbor")
        v = bytes(b[i:i + n])
        return (v if mt == 2 else v.decode()), i + n
    if mt == 4:
        out = []
        for _ in range(n):
            v, i = cbor(b, i); out.append(v)
        return out, i
    if mt == 5:
        out = {}
        for _ in range(n):
            k, i = cbor(b, i); v, i = cbor(b, i); out[k] = v
        return out, i
    raise ValueError("unsupported cbor")

class Passkeys:
    def __init__(self):
        self.lock = threading.Lock()
        self.challenges = {}   # challenge -> (expiry, kind)
    def load(self):
        try:
            return json.load(open(PASSKEY_FILE))
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
        return {"challenge": self.challenge("auth"), "rpId": rp, "timeout": 60000,
                "allowCredentials": [{"id": c["id"]} for c in items]}

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
    if ad[:32] != hashlib.sha256(rp.encode()).digest() or ad[32] & 0x45 != 0x45:  # rpId, user present, user verified, attested data
        raise ValueError("bad authData")
    n = int.from_bytes(ad[53:55], "big")
    cid = ad[55:55 + n]
    key = cbor(ad, 55 + n)[0]
    if key.get(1) != 2 or key.get(3) != -7 or key.get(-1) != 1 or len(key.get(-2, b"")) != 32 or len(key.get(-3, b"")) != 32:
        raise ValueError("unsupported key")
    wa_pubkey(key[-2], key[-3])  # rejects points not on the curve
    if d.get("id") != b64u(cid):
        raise ValueError("id mismatch")
    name = re.sub(r"[^\w .-]", "", str(d.get("name", "device")))[:40] or "device"
    with PK.lock:
        items = [c for c in PK.load() if c["id"] != b64u(cid)]
        items.append({"id": b64u(cid), "x": b64u(key[-2]), "y": b64u(key[-3]), "count": int.from_bytes(ad[33:37], "big"),
                      "name": name, "created": int(time.time())})
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
    if ad[:32] != hashlib.sha256(rp.encode()).digest() or ad[32] & 0x05 != 0x05:  # rpId, user present, user verified
        raise ValueError("bad authData")
    wa_pubkey(unb64u(cred["x"]), unb64u(cred["y"])).verify(unb64u(r["signature"]), ad + hashlib.sha256(raw).digest(), ec.ECDSA(hashes.SHA256()))
    n = int.from_bytes(ad[33:37], "big")
    if n or cred["count"]:
        if n <= cred["count"]:
            raise ValueError("replayed or cloned authenticator")
        with PK.lock:
            items = PK.load()
            for c in items:
                if c["id"] == cred["id"]: c["count"] = n
            PK.save(items)

LOGIN_PAGE = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>@@TITLE@@</title><link rel=manifest href=/manifest.webmanifest><meta name=theme-color content="#111111"><link rel=icon href=/icon-192.png><style>
body{background:#111;color:#ddd;font:16px sans-serif;margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center}
.b{width:min(320px,86vw);text-align:center}h3{font-weight:400;margin:0 0 18px;color:#889}
input{width:100%;box-sizing:border-box;background:#2a2d33;color:#ddd;border:0;border-radius:8px;padding:16px;font-size:28px;text-align:center;letter-spacing:.5em}
#m{height:22px;margin-top:14px;color:#c66;font-size:14px}
button{margin-top:10px;background:#2a2d33;color:#ddd;border:0;border-radius:8px;padding:14px 22px;font-size:16px}</style><body><div class=b><h3>@@TITLE@@</h3>
<input id=p type=password inputmode=numeric pattern="[0-9]*" maxlength=6 autocomplete=off><div id=m></div><button id=fb hidden>Use fingerprint</button></div>
<script>
const p=document.getElementById('p'),m=document.getElementById('m');let t=null;
function lock(s){p.disabled=true;p.value='';clearInterval(t);const end=Date.now()+s*1000;
 const tick=()=>{const r=Math.max(0,Math.round((end-Date.now())/1000));
  if(!r){clearInterval(t);p.disabled=false;m.textContent='';p.focus();return}
  const h=Math.floor(r/3600),mi=Math.floor(r%3600/60),se=r%60;
  m.textContent='Locked. Try again in '+(h?h+'h ':'')+(mi||h?mi+'m ':'')+se+'s'};
 tick();t=setInterval(tick,1000)}
async function go(){const r=await fetch('/login',{method:'POST',body:JSON.stringify({pin:p.value})});let j={};try{j=await r.json()}catch(_){}
 if(r.ok){location.reload()}else if(r.status===429){lock(j.locked||60)}
 else{m.textContent='Wrong PIN, '+(j.left??'?')+' tries left';p.value='';navigator.vibrate&&navigator.vibrate(60)}}
p.oninput=()=>{m.textContent='';if(p.value.length===6)go()};
const L=@@LOCK@@;if(L>0)lock(L);
const WA=@@WA@@;
const b2u=b=>btoa(String.fromCharCode(...new Uint8Array(b))).replace(/\\+/g,'-').replace(/\\//g,'_').replace(/=+$/,'');
const u2b=s=>Uint8Array.from(atob(s.replace(/-/g,'+').replace(/_/g,'/')),c=>c.charCodeAt(0));
async function bio(o){
 try{
  if(!o){const r=await fetch('/webauthn/auth/begin',{method:'POST'});if(!r.ok)throw 0;o=await r.json()}
  const c=await navigator.credentials.get({publicKey:{challenge:u2b(o.challenge),rpId:o.rpId,timeout:o.timeout,userVerification:'required',
   allowCredentials:o.allowCredentials.map(x=>({type:'public-key',id:u2b(x.id)}))}});
  const q=c.response;
  const r=await fetch('/webauthn/auth/finish',{method:'POST',body:JSON.stringify({id:c.id,response:{clientDataJSON:b2u(q.clientDataJSON),authenticatorData:b2u(q.authenticatorData),signature:b2u(q.signature)}})});
  if(r.ok){location.reload();return}
  m.textContent='Fingerprint not accepted'
 }catch(e){m.textContent='Fingerprint cancelled. Use the PIN or the button below'}
 if(!p.disabled)p.focus()}
const fb=document.getElementById('fb');
if(WA&&window.PublicKeyCredential){fb.hidden=false;fb.onclick=()=>bio(null);bio(WA)}else if(L<=0)p.focus();
</script>"""

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def handle(self):
        try:
            super().handle()
        except ConnectionError:  # client went away mid-request (e.g. phone slept)
            pass

    def _sid(self):
        try:
            c = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
            return c["sid"].value if "sid" in c else ""
        except http.cookies.CookieError:
            return ""

    def _rp(self):
        """(rpId, origin) from the Host header; WebAuthn only works on https, so the origin is https."""
        host = self.headers.get("Host", "").strip().lower()
        rp = host.rsplit(":", 1)[0] if re.search(r":\d+$", host) else host
        return (rp, "https://" + host) if re.fullmatch(r"[a-z0-9.-]+", rp) else (None, None)

    def _session_ok(self, sid):
        data = b"{}"
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data)))
        self.send_header("Set-Cookie", self._cookie(sid, 30 * 86400)); self.end_headers()
        self.wfile.write(data)

    def _empty(self, code):
        self.send_response(code); self.send_header("Content-Length", "0"); self.end_headers()

    def _cookie(self, sid, max_age):
        flags = "; Secure" if self.headers.get("X-Forwarded-Proto", "") == "https" else ""
        return "sid=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax%s" % (sid, max_age, flags)

    def do_GET(self):
        if self.path == "/manifest.webmanifest":
            self.send_response(200); self.send_header("Content-Type", "application/manifest+json"); self.send_header("Content-Length", str(len(MANIFEST.encode()))); self.end_headers()
            return self.wfile.write(MANIFEST.encode())
        if self.path in ("/icon-192.png", "/icon-512.png"):
            data = open(os.path.join(HERE, self.path[1:]), "rb").read()
            self.send_response(200); self.send_header("Content-Type", "image/png"); self.send_header("Content-Length", str(len(data))); self.end_headers()
            return self.wfile.write(data)
        if self.path == "/auth":  # lets the page ask whether its session is still valid (does not extend it)
            return self._empty(204 if not AUTH.enabled() or AUTH.peek(self._sid()) else 401)
        authed = AUTH.check(self._sid())
        if self.path == "/ws" and self.headers.get("Upgrade", "").lower() == "websocket":
            return self._ws() if authed else self._empty(401)
        if self.path == "/webauthn/list":
            if not authed: return self._empty(401)
            return self._json(lambda: {"items": [{k: c[k] for k in ("id", "name", "created")} for c in PK.load()]})
        if self.path == "/scale" and "scale" in FEATURES:
            if not authed: return self._empty(401)
            return self._json(lambda: dict(zip(("output", "scale"), scale_target())))
        if authed:
            fp = ('<h5>Fingerprint unlock</h5><div id=fp></div><div id=fpm style="color:#889;font-size:13px;margin:6px 2px"></div>'
                  if AUTH.enabled() and WEBAUTHN else "")
            lockbtn = fp + ('<h5>Session</h5><div class=g style="grid-template-columns:1fr"><button class=x id=lockbtn>Lock now</button></div>'
                       if AUTH.enabled() else "")
            data = PAGE.replace("@@LOCKBTN@@", lockbtn).encode()
        else:
            rp = self._rp()[0]
            wa_opts = PK.auth_options(rp) if rp else None
            data = (LOGIN_PAGE.replace("@@TITLE@@", TITLE).replace("@@LOCK@@", str(AUTH.remaining()))
                    .replace("@@WA@@", json.dumps(wa_opts).replace("<", "\\u003c"))).encode()
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data))); self.end_headers()
        self.wfile.write(data)

    def _json(self, fn, code=200):
        try:
            data = json.dumps(fn()).encode()
        except Exception:
            data, code = b"{}", 500
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers()
        self.wfile.write(data)

    def _ws(self):
        sid = self._sid()
        key = self.headers.get("Sec-WebSocket-Key", "")
        acc = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket"); self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", acc); self.end_headers()
        self.wfile.flush()
        self.close_connection = True
        r = self.rfile
        def rd(n):
            b = r.read(n)
            if len(b) < n: raise EOFError
            return b
        try:
            while True:
                b0, b1 = rd(2)
                op, ln = b0 & 15, b1 & 127
                if ln == 126: ln = struct.unpack(">H", rd(2))[0]
                elif ln == 127: ln = struct.unpack(">Q", rd(8))[0]
                if ln > 4096: break
                mask = rd(4) if b1 & 128 else b"\0\0\0\0"
                data = bytes(c ^ mask[i % 4] for i, c in enumerate(rd(ln)))
                if op == 8: break
                if op == 9:
                    self.wfile.write(bytes([0x8A, len(data)]) + data); self.wfile.flush()
                elif op == 1:
                    if not AUTH.check(sid): break
                    try:
                        d = json.loads(data)
                        c = lambda k: max(-200, min(200, int(d.get(k, 0))))
                        MOUSE.send(c("dx"), c("dy"), c("sx"), c("sy"), d.get("btn"), d.get("down"))
                    except Exception:
                        pass
        except (EOFError, OSError):
            pass
        for b in ("l", "r", "m"):  # never leave a button stuck if the phone drops
            try: MOUSE.send(btn=b, down=0)
            except Exception: pass
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 65536:
            self.close_connection = True
            return self._empty(413)
        body = self.rfile.read(n) if n else b""
        if self.path == "/login":
            try:
                pin = str(json.loads(body).get("pin", ""))
            except Exception:
                pin = ""
            if not re.fullmatch(r"\d{6}", pin):
                return self._empty(400)
            code, payload, sid = AUTH.login(pin)
            data = json.dumps(payload).encode()
            self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data)))
            if sid: self.send_header("Set-Cookie", self._cookie(sid, 30 * 86400))
            self.end_headers(); return self.wfile.write(data)
        if self.path == "/logout":
            AUTH.logout(self._sid())
            self.send_response(204); self.send_header("Set-Cookie", self._cookie("", 0)); self.send_header("Content-Length", "0"); self.end_headers()
            return
        rp, origin = self._rp()
        if self.path.startswith("/webauthn/auth/") and WEBAUTHN and rp:  # public: this is how you log in
            if self.path.endswith("/begin"):
                opts = PK.auth_options(rp)
                return self._json(lambda: opts) if opts else self._empty(404)
            try:
                wa_login(json.loads(body), rp, origin)
            except Exception:
                return self._empty(401)
            return self._session_ok(AUTH.create_session())
        if not AUTH.check(self._sid()):
            return self._empty(401)
        if self.path.startswith("/webauthn/") and WEBAUTHN and rp:
            if self.path == "/webauthn/reg/begin":
                return self._json(lambda: {
                    "challenge": PK.challenge("reg"), "timeout": 60000, "attestation": "none",
                    "rp": {"id": rp, "name": TITLE},
                    "user": {"id": b64u(b"niri-remote"), "name": "owner", "displayName": TITLE},
                    "pubKeyCredParams": [{"type": "public-key", "alg": -7}],
                    "authenticatorSelection": {"authenticatorAttachment": "platform", "userVerification": "required", "residentKey": "preferred"},
                    "excludeCredentials": [{"id": c["id"]} for c in PK.load()]})
            try:
                if self.path == "/webauthn/reg/finish":
                    wa_register(json.loads(body), rp, origin)
                elif self.path == "/webauthn/remove":
                    cid = json.loads(body).get("id")
                    with PK.lock:
                        PK.save([c for c in PK.load() if c["id"] != cid])
                else:
                    return self._empty(404)
            except Exception:
                return self._empty(400)
            return self._empty(204)
        if self.path == "/m":
            try:
                d = json.loads(body)
                c = lambda k: max(-200, min(200, int(d.get(k, 0))))
                MOUSE.send(c("dx"), c("dy"), c("sx"), c("sy"), d.get("btn"), d.get("down"))
                code = 204
            except Exception:
                code = 400
            self.send_response(code); self.send_header("Content-Length", "0"); self.end_headers()
            return
        if self.path.startswith("/scale/") and "scale" in FEATURES:
            try:
                v = round(float(self.path.removeprefix("/scale/")), 2)
                if not 0.5 <= v <= 4: raise ValueError
            except ValueError:
                self.send_response(400); self.send_header("Content-Length", "0"); self.end_headers()
                return
            return self._json(lambda: dict(zip(("output", "scale"), set_scale(v))))
        name = self.path.removeprefix("/do/")
        ok = name in ACTIONS
        if ok: run(name)
        self.send_response(204 if ok else 404); self.send_header("Content-Length", "0"); self.end_headers()
    def log_message(self, *a): pass

if __name__ == "__main__":
    if "--set-pin" in sys.argv:
        sys.exit(set_pin())
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    host = args[0] if args else os.environ.get("PANEL_BIND", "127.0.0.1")
    if not os.path.exists(PIN_FILE):
        print("warning: no PIN set, the panel is open to the tailnet. Run: panel.py --set-pin", file=sys.stderr, flush=True)
    ThreadingHTTPServer((host, 8765), H).serve_forever()
