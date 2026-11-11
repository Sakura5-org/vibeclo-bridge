"""CLO 2 tunnel: this Mac ↔ its paired phones/browsers through the relay, end-to-end encrypted.

Identity (~/.clo2/identity.json, 0600): X25519 key pair, room id, the Mac's relay token. Paired devices
(~/.clo2/devices.json): name, public key, hash of the device's relay token.

Crypto (same in Swift / JS): key = HKDF-SHA256(X25519(mac, device), salt "clo2-v1", info macPub ‖ devicePub, 32 bytes);
frame = base64(nonce 12 ‖ AES-256-GCM(raw-DEFLATE(JSON message)) ‖ tag). The relay sees only frames.

Pairing: the local page (http://127.0.0.1:7788) shows a QR {relay, room, Mac key, one-time code (5 min), name}. The phone
connects with the code, sends its public key + an encrypted hello; this Mac shows a 4-digit check code (from both keys)
and the user approves here → the phone gets its own device token (sent encrypted).

Requests from a device: {id, method, url, body} → the local store (Firestore REST shape) → {id, status, body}.

Direct path on the same network (the relay moves only ~65–100 KB/s): the Mac also listens on ws://<LAN IP>:7789. The
device learns the addresses through the tunnel (op "lan"), connects, gets a fresh random challenge, and both sides
switch to a per-connection key = HKDF(pairing key, salt challenge, info "clo2-lan"): only a paired device can talk,
only this Mac can answer, and a frame recorded on the Wi-Fi can't be replayed into another connection. Same frames,
same requests; presence, pairing and push stay on the relay. Revoking/resetting closes direct connections too.
"""
import base64
import hashlib
import json
import os
import pathlib
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import zlib
from concurrent.futures import ThreadPoolExecutor

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from websockets.sync.client import connect
from websockets.sync.server import serve as ws_serve

DIR = pathlib.Path(os.environ.get("CLO2_DIR") or pathlib.Path.home() / ".clo2")   # CLO2_DIR: tests
IDENTITY = DIR / "identity.json"
DEVICES = DIR / "devices.json"
SETTINGS = DIR / "settings.json"
# Where a new device may ask to pair from (the Mac owner picks it in the menu bar). A QR shown on this Mac's own screen
# always works: whoever scans it is standing at the Mac.
PAIR_VIA = {"tailnet": "Tailscale only", "wifi": "Same Wi-Fi only", "both": "Tailscale or same Wi-Fi",
            "any": "Anywhere (also links sent over the internet)"}
# What Claude may do for a paired device. "standard": edit files in the project, other actions needing permission are
# refused (claude -p can't ask). "full": --dangerously-skip-permissions when the device asks for it (yolo profile).
ACCESS = {"full": "Full access — claude --dangerously-skip-permissions", "standard": "Standard — claude --permission-mode acceptEdits"}
AUTO_UPDATE = {"on": "Automatic updates on", "off": "Automatic updates off"}
DEFAULTS = {"pairVia": "both", "access": "full", "autoUpdate": "on"}   # full = yolo, as plain `claude --dangerously-skip-permissions`


def setting(key):
    """The owner's choices for this Mac (installer / `clo2 settings` / menu bar), in ~/.clo2/settings.json."""
    try:
        return json.loads(SETTINGS.read_text()).get(key, DEFAULTS[key])
    except Exception:
        return DEFAULTS[key]


def set_setting(key, value):
    if value not in {"pairVia": PAIR_VIA, "access": ACCESS, "autoUpdate": AUTO_UPDATE}.get(key, ()):
        return False
    try:
        cur = json.loads(SETTINGS.read_text())
    except Exception:
        cur = {}
    _write_private(SETTINGS, {**cur, key: value})
    return True
PAIR_TTL = 300

b64u = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")
unb64u = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
token_hash = lambda t: b64u(hashlib.sha256(t.encode()).digest())


def _write_private(path, data):
    DIR.mkdir(mode=0o700, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def load_identity(reset=False):
    if IDENTITY.exists() and not reset:
        return json.loads(IDENTITY.read_text())
    key = X25519PrivateKey.generate()
    ident = {"priv": b64u(key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())),
             "pub": b64u(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)),
             "room": b64u(secrets.token_bytes(18)), "token": b64u(secrets.token_bytes(32)), "createdAt": time.time()}
    _write_private(IDENTITY, ident)
    return ident


def session_key(ident, dev_pub_b64):
    priv = X25519PrivateKey.from_private_bytes(unb64u(ident["priv"]))
    shared = priv.exchange(X25519PublicKey.from_public_bytes(unb64u(dev_pub_b64)))
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"clo2-v1",
                info=unb64u(ident["pub"]) + unb64u(dev_pub_b64)).derive(shared)


def _deflate(b):
    c = zlib.compressobj(6, zlib.DEFLATED, -15)   # raw DEFLATE: what Apple's .zlib and JS "deflate-raw" speak
    return c.compress(b) + c.flush()


def seal(key, obj):
    """The relay's Durable Object path is slow (~65 KB/s), and replies are mostly text: compress, then encrypt."""
    nonce = secrets.token_bytes(12)
    plain = _deflate(json.dumps(obj, ensure_ascii=False).encode())
    return base64.b64encode(nonce + AESGCM(key).encrypt(nonce, plain, None)).decode()


def open_frame(key, data):
    raw = base64.b64decode(data)
    return json.loads(zlib.decompress(AESGCM(key).decrypt(raw[:12], raw[12:], None), -15))


def push_key(key):
    """Key for notification text (see notify in clo_bridge.py): derived from the pairing key, used for nothing else."""
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"clo2-push", info=b"").derive(key)


def seal_push(key, obj):
    """Notification payload: base64(nonce ‖ AES-GCM(JSON)), no compression (a few hundred bytes; APNs allows 4 KB).
    The phone's Notification Service Extension opens it; Apple and the relay only carry it."""
    nonce = secrets.token_bytes(12)
    return base64.b64encode(nonce + AESGCM(push_key(key)).encrypt(nonce, json.dumps(obj, ensure_ascii=False).encode(), None)).decode()


def lan_key(key, challenge):
    """Per-connection key for the direct path (see module doc)."""
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=challenge, info=b"clo2-lan").derive(key)


def lan_addresses():
    """This Mac's IPv4 addresses a phone on the same network (or the same tailnet) could reach, best first."""
    try:
        cmd = ["/sbin/ifconfig"] if os.path.exists("/sbin/ifconfig") else ["ip", "-4", "-o", "addr"]   # Linux: `ip` prints "inet a.b.c.d/24" too
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return []
    ips = re.findall(r"\binet (\d+\.\d+\.\d+\.\d+)", out)
    rank = lambda ip: (0 if ip.startswith("192.168.") else 1 if ip.startswith(("10.", "172.")) else 2)
    private = lambda ip: (ip.startswith(("192.168.", "10.")) or re.match(r"172\.(1[6-9]|2\d|3[01])\.", ip)
                          or re.match(r"100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.", ip))
    return sorted(dict.fromkeys(ip for ip in ips if private(ip)), key=rank)[:6]


LINUX = sys.platform.startswith("linux")


def gui_env():
    """Linux: the desktop session's display for dialogs/notifications (a systemd user service may not have it)."""
    env = dict(os.environ)
    run = env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={run}/bus")
    if not env.get("WAYLAND_DISPLAY") and not env.get("DISPLAY"):
        # started at boot (lingering) before anyone logged in: borrow the desktop shell's display + X cookie
        keys = ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY")
        for pid in sorted((d for d in os.listdir("/proc") if d.isdigit()), key=int):
            try:
                if os.stat(f"/proc/{pid}").st_uid != os.getuid():
                    continue
                if open(f"/proc/{pid}/comm").read().strip() not in ("gnome-shell", "plasmashell", "xfce4-session", "cinnamon", "mate-session"):
                    continue
                pe = dict(x.split("=", 1) for x in open(f"/proc/{pid}/environ").read().split("\0") if "=" in x)
            except OSError:
                continue
            env.update({k: pe[k] for k in keys if pe.get(k)})
            if env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"):
                break
        env.setdefault("DISPLAY", ":0")
    return env


def notify(text):
    """macOS notification (best effort): a device is waiting for "Allow" in the CLO menu."""
    if os.environ.get("CLO2_NO_NOTIFY"):
        return
    if LINUX:
        try:
            subprocess.Popen(["notify-send", "-a", "Vibeclo", "Vibeclo", text], env=gui_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        return
    script = f'display notification {json.dumps(text + " — click the Vibeclo menu bar icon to allow", ensure_ascii=False)} with title "Vibeclo" sound name "Glass"'
    try:
        subprocess.Popen(["/usr/bin/osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


TAILSCALE = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
class NoTailnetWeb(Exception):
    """Web over Tailscale stays off by choice / tailnet setting — not retried."""


TS_WEB_SAVED = DIR / "tailnet.json"


def ts_web_saved(port):
    try:
        d = json.loads(TS_WEB_SAVED.read_text())
        return d if d.get("port") == port and str(d.get("host", "")).rsplit(":", 1)[0].endswith(".ts.net") else {}
    except (OSError, ValueError):
        return {}


def setup_ts_web(port):
    """`tailscale serve --bg --https=8443 http://127.0.0.1:<port>` unless already there; nothing else in the serve config
    is touched. Saves (host:8443, owner's Tailscale login) to ~/.clo2/tailnet.json. Raises NoTailnetWeb when it must stay
    off, other errors when the CLI fails."""
    if os.environ.get("CLO2_NO_TAILNET"):
        raise NoTailnetWeb("CLO2_NO_TAILNET")
    st = ts_json("status")
    host = st["Self"]["DNSName"].rstrip(".")
    if host not in (st.get("CertDomains") or []):   # HTTPS certificates off for this tailnet: serve would only ask to enable them
        raise NoTailnetWeb("HTTPS is not enabled for this tailnet (Tailscale admin → DNS → HTTPS Certificates)")
    login = str(((st.get("User") or {}).get(str(st["Self"].get("UserID"))) or {}).get("LoginName") or "")
    cli = TAILSCALE if os.path.exists(TAILSCALE) else "tailscale"
    have = subprocess.run(["tailscale", "serve", "status", "--json"], executable=cli, capture_output=True, text=True, timeout=8).stdout
    target = f"http://127.0.0.1:{port}"
    if target not in have:
        if f"{host}:{TS_WEB_HTTPS}" in have:
            raise NoTailnetWeb(f"port {TS_WEB_HTTPS} on {host} is already served by something else")
        r = subprocess.run(["tailscale", "serve", "--bg", "--yes", f"--https={TS_WEB_HTTPS}", target], executable=cli,
                           capture_output=True, text=True, timeout=30)
        if r.returncode:
            raise RuntimeError((r.stderr or r.stdout).strip()[:300])
    ts = f"{host}:{TS_WEB_HTTPS}"
    _write_private(TS_WEB_SAVED, {"host": ts, "login": login, "port": port})
    return ts, login


TS_WEB_HTTPS = int(os.environ.get("CLO2_TS_WEB_HTTPS") or 8443)   # https port on the tailnet name (tailscale serve)


def ts_json(*args):
    """`tailscale <cmd> --json …` → dict. The app's CLI sometimes answers nothing (it starts the app's helper first):
    retry a few times."""
    cli = TAILSCALE if os.path.exists(TAILSCALE) else "tailscale"
    err, r = "", None
    for i in range(4):
        # argv[0] "tailscale": the Mac app's binary runs as the CLI only under that name, else it tries to be the GUI
        try:
            r = subprocess.run(["tailscale", args[0], "--json", *args[1:]], executable=cli, capture_output=True, text=True, timeout=8)
        except subprocess.TimeoutExpired:
            # Seen on every call from the launchd bridge on a Mac with the App Store Tailscale (fine from Terminal):
            # the CLI never gets through to the app there. Retrying only makes pairing dialogs a minute late.
            raise RuntimeError(f"tailscale {args[0]}: timed out (the Tailscale CLI can't reach the app from here)")
        out = r.stdout.strip()
        if out.startswith("{"):
            return json.loads(out)
        err = (out or r.stderr or "").strip()[:200]
        time.sleep(0.7 * (i + 1))
    raise RuntimeError(f"tailscale {args[0]}: no output (exit {r.returncode if r else '?'}) {err}")


def tailnet_info(ip, log=None):
    """(same user, device name) for a Tailscale address: same = logged in as this Mac's Tailscale user (not a node shared
    in from someone else's tailnet, not a tagged server). Shown in the "Cho phép" dialog; never approves by itself."""
    if not str(ip).startswith("100.") or os.environ.get("CLO2_NO_TAILNET"):
        return False, ""
    try:
        me = ts_json("status")["Self"]["UserID"]
        node = ts_json("whois", ip)["Node"]
        same = bool(me) and node.get("User") == me and not node.get("Tags")
        if not same and log:
            log("tailnet: not the same user", ip, node.get("User"), "vs", me, "tags", node.get("Tags"))
        return same, str(node.get("ComputedName") or node.get("Name") or "").split(".")[0][:40]
    except Exception as e:
        if log:
            log("tailnet check failed for", ip, "-", type(e).__name__, str(e)[:300])
        return False, ""


def tailnet_same_user(ip, log=None):
    return tailnet_info(ip, log)[0]


def tailnet_macs(port, timeout=3):
    """Other machines of this Mac's Tailscale user that run CLO 2 (answer on the direct-path port): [{name, host, k}].
    The phone can't list the tailnet itself; a paired Mac can, so "Thêm Mac" shows them for one-tap pairing."""
    try:
        st = ts_json("status")
    except Exception:
        return []
    me = st.get("Self", {}).get("UserID")
    hosts = [(p.get("HostName") or "", (p.get("TailscaleIPs") or [""])[0]) for p in (st.get("Peer") or {}).values()
             if p.get("Online") and p.get("UserID") == me and not p.get("Tags") and (p.get("TailscaleIPs") or [""])[0].startswith("100.")]

    def probe(h):
        name, ip = h
        try:
            with connect(f"ws://{ip}:{port}", open_timeout=timeout, close_timeout=1) as ws:
                hello = json.loads(ws.recv(timeout=timeout))
            if hello.get("op") == "challenge" and hello.get("k"):
                return {"name": str(hello.get("n") or name)[:60], "host": ip, "k": hello["k"][:16]}
        except Exception:
            return None
    with ThreadPoolExecutor(max_workers=16) as pool:
        return [m for m in pool.map(probe, hosts) if m]


# Collections whose changes paired devices hear about at once (op "changed"), so they don't have to poll.
PUSH_COLLS = {"terminalSessions", "terminalInbox", "terminalSeen"}
LAN_GRACE = float(os.environ.get("CLO2_LAN_GRACE") or 30)      # s a pairing dialog stays up after the phone's connection dropped (it reconnects with the same key)
ACTIVE_FOR = 180   # a device that made a request in the last 3 min gets change notices through the relay


def _dialog(cmd, alive, timeout, **kw):
    """Run a dialog; close it as soon as `alive()` turns False (the device gave up or asked again) → None."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, **kw)
    end = time.time() + timeout
    while proc.poll() is None:
        if not alive() or time.time() > end:
            proc.terminate()
            proc.wait(5)
            return None
        time.sleep(0.3)
    return proc.returncode, proc.stdout.read()


def ask_dialog(name, code, via="", timeout=170, alive=lambda: True):
    """A dialog in the middle of the Mac's screen asking for the 4 digits the device shows (like Bluetooth pairing):
    True = typed the right code, False = "Deny", None = no answer / no screen. A wrong code asks again (3 tries).
    Typing it, not just clicking, means the user really looked at that device. The menu bar icon can be quit or hidden
    behind the notch; this can't be missed."""
    if os.environ.get("CLO2_NO_NOTIFY"):
        return None
    q = lambda t: json.dumps(t, ensure_ascii=False)
    end = time.time() + timeout
    note = ""
    for _ in range(3):
        left = int(end - time.time())
        if left < 5:
            return None
        text = (f"“{name}” wants to connect to this Mac" + (f"\n{via}" if via else "") + "."
                "\n\nOnce paired, it can run Claude Code here — read and change files, run commands as you."
                "\n\nType the 4-digit code shown on that device:" + note)
        if LINUX:   # zenity: exit 0 = Pair (typed text on stdout), 1 = Deny, 5 = timed out, other = no screen
            try:
                r = _dialog(["zenity", "--entry", "--title", "Vibeclo", "--text", text.replace("Mac", "computer"),
                             "--ok-label", "Pair", "--cancel-label", "Deny", "--timeout", str(left)], alive, left + 10, env=gui_env())
            except Exception:
                return None
            if r is None or r[0] not in (0, 1):
                return None
            if r[0] == 1:
                return False if alive() else None
            out = "text returned:" + r[1].strip()
        else:
            script = (f'display dialog {q(text)} default answer "" with title "Vibeclo" with icon caution '
                      f'buttons {{"Deny", "Pair"}} default button "Pair" giving up after {left}')
            try:
                r = _dialog(["/usr/bin/osascript", "-e", script], alive, left + 10)
            except Exception:
                return None
            if r is None:
                return None
            out = r[1]
            if "gave up:true" in out:
                return None
            if "button returned:Pair" not in out:
                return False
        typed = "".join(c for c in out.split("text returned:", 1)[-1].split(",")[0] if c.isdigit())
        if typed == code:
            return True
        note = "\n\n⚠️ Wrong code — check the device and try again."
    return False


def check_code(mac_pub, dev_pub):
    """4 digits both screens show, so the user knows the phone talking to this Mac is theirs."""
    return f"{int.from_bytes(hashlib.sha256(unb64u(mac_pub) + unb64u(dev_pub)).digest()[:4], 'big') % 10000:04d}"


class Tunnel:
    def __init__(self, relay, label, handle, log, ops=None, lan_port=0, ts_web=0, web="https://clo2.sakura5.app"):
        self.relay, self.label, self.handle, self.log = relay.rstrip("/"), label, handle, log
        self.ops = ops or {}
        self.on_push_result = None       # op name → fn(req, device) → (status, body): uploads, secrets, artifacts…
        self.ident = load_identity()
        self.devices = json.loads(DEVICES.read_text()) if DEVICES.exists() else []
        if not SETTINGS.exists() and self.devices:
            # installed before these settings existed: it ran yolo turns; keep that (the owner changes it with clo2 settings)
            _write_private(SETTINGS, {"pairVia": "both", "access": "full"})
            self.log("settings: existing install → access full (as before), pairing from Tailscale or Wi-Fi")
        self.pair_via = setting("pairVia")
        self.ws = None
        self.online = False
        self.echo_seen, self.echo_ok = None, False   # relay heartbeat (see heartbeat())
        self.done, self.done_lock = {}, threading.Lock()   # (device, request id) → (time, reply frame | None while running)
        self.pairing = None        # {"code", "hash", "exp"}
        self.lan_ok = {}           # device key → until when a Wi-Fi/Tailscale pairing approved here may fetch its link again
        self.pending = {}          # relay tag "p:<hash>" → {"pub", "name", "check", "at"}
        self.keys = {}             # device pub → session key
        self.send_lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=8)
        self.lan_port = lan_port
        self.lan = {}              # direct connections: ws → device hash
        self.lan_out = {}          # direct connections: ws → (key, reply) for change notices
        self.active = {}           # device hash → last request time (relay path)
        self.changes, self.changes_lock = set(), threading.Lock()
        self.web = web.rstrip("/")
        self.web_origins = {self.web, "https://clo2.synthvale.com", "https://clo.sakura5.app"}   # public + owner's + old domain (all served)
        self.ts_web, self.ts_host, self.ts_login = ts_web, "", ""   # web app ↔ this Mac over Tailscale HTTPS (see start_ts_web)
        if lan_port:
            self.start_lan()
            if ts_web:
                threading.Thread(target=self.start_ts_web, daemon=True).start()

    # ------------------------------------------------------------ state for the local page
    def state(self):
        return {"online": self.online, "label": self.label, "room": self.ident["room"],
                "pairing": self.qr() if self.pairing and self.pairing["exp"] > time.time() else None,
                "pairingShare": self.qr(share=True) if self.pairing and self.pairing["exp"] > time.time() else None,
                "pairingExp": self.pairing["exp"] if self.pairing else 0, "pairVia": self.pair_via, "access": setting("access"),
                "autoUpdate": setting("autoUpdate"),
                "pending": [{"tag": t, "name": p["name"], "check": p["check"], "via": p.get("via", "")} for t, p in self.pending.items()],
                "devices": [{"id": d["hash"], "name": d["name"], "addedAt": d["addedAt"], "lastSeen": d.get("lastSeen", 0)} for d in self.devices]}

    def qr(self, share=False):
        """The pairing link. Shown on this Mac's screen (QR, "open the web app here") it carries `a`: whoever presents it
        is approved at once — it never left the screen. The copy-to-send-elsewhere link (`share`) leaves `a` out, so a
        device using it still waits for "Cho phép" with the 4-digit check."""
        host = urllib.parse.urlsplit(self.relay).netloc
        payload = {"v": 1, "r": host, "room": self.ident["room"], "k": self.ident["pub"], "c": self.pairing["code"], "n": self.label}
        if not share:
            payload["a"] = self.pairing["auto"]
        return "clo2://pair?d=" + b64u(json.dumps(payload, separators=(",", ":")).encode())

    def start_pairing(self):
        code = b64u(secrets.token_bytes(18))
        self.pairing = {"code": code, "hash": token_hash(code), "exp": time.time() + PAIR_TTL, "auto": b64u(secrets.token_bytes(18))}
        self.ctl({"op": "pair", "hash": self.pairing["hash"], "exp": int(self.pairing["exp"] * 1000)})
        return self.qr()

    def stop_pairing(self, only=None):
        if only and (not self.pairing or self.pairing["hash"] != only):   # a newer pairing started meanwhile: keep it
            return
        if self.pairing:
            self.ctl({"op": "pair", "hash": self.pairing["hash"], "exp": 0})
        self.pairing = None
        self.pending = {t: p for t, p in self.pending.items() if p.get("lan")}   # Wi-Fi requests wait on their own socket

    def approve(self, tag):
        p = self.pending.pop(tag, None)
        if not p:
            return False
        if p.get("lan"):   # asked over Wi-Fi: on_lan_pair hands the phone a pairing link
            self.lan_ok[p["pub"]] = time.time() + 300   # also if its socket dropped meanwhile: it can come back for the link
            self.log("pairing approved for", p["name"])
            p["answer"] = "ok"
            p["event"].set()
            return True
        token = b64u(secrets.token_bytes(32))
        dev = {"hash": token_hash(token), "name": p["name"], "pub": p["pub"], "addedAt": time.time()}
        for old in [d for d in self.devices if d.get("pub") == p["pub"]]:   # re-paired (app reinstalled): new token replaces the old
            self.ctl({"op": "revoke", "hash": old["hash"]})
        self.devices = [d for d in self.devices if d.get("pub") != p["pub"]] + [dev]
        _write_private(DEVICES, self.devices)
        self.ctl({"op": "allow", "hash": dev["hash"]})
        self.send_to(tag, p["pub"], {"op": "paired", "token": token, "name": self.label})
        if self.pairing:
            threading.Timer(2.0, self.stop_pairing, kwargs={"only": self.pairing["hash"]}).start()   # let the reply out before the pairing socket closes
        self.log("paired", p["name"])
        return True

    def set_pair_via(self, mode):
        if not set_setting("pairVia", mode):
            return False
        self.pair_via = mode
        self.log("pair via:", mode)
        return True

    def set_setting(self, key, value):
        if key == "pairVia":
            return self.set_pair_via(value)
        ok = set_setting(key, value)
        if ok:
            self.log("setting", key, "=", value)
        return ok

    def pair_refusal(self, how):
        """None when a new device asking over `how` ("tailnet" | "wifi" | "link") may be asked about; else why not."""
        mode = self.pair_via
        if mode == "any" or (how == "tailnet" and mode in ("tailnet", "both")) or (how == "wifi" and mode in ("wifi", "both")):
            return None
        return (f"This Mac only accepts pairing from: {PAIR_VIA.get(mode, mode)}. Scan the QR code on the Mac's screen "
                f"(run `clo2 pair`), or change it with `clo2 settings` / the Vibeclo menu bar icon.")

    def known(self, pub):
        return bool(pub) and any(d.get("pub") == pub for d in self.devices)

    def ask(self, tag):
        """Approve / deny from a dialog on this Mac where the user types the device's 4 digits (the menu bar and the
        local page still work too)."""
        p = self.pending.get(tag)
        if not p:
            return

        def run():
            ok = ask_dialog(p["name"], p["check"], p.get("via", ""), alive=lambda: self.pending.get(tag) is p)
            if self.pending.get(tag) is not p:   # answered elsewhere meanwhile
                return
            if ok is True:
                self.approve(tag)
            elif ok is False:
                self.deny(tag)
            else:
                notify(f"{p['name']} wants to connect · code {p['check']}")
        threading.Thread(target=run, daemon=True).start()

    def deny(self, tag):
        p = self.pending.pop(tag, None)
        if p and p.get("lan"):
            p["answer"] = "no"
            p["event"].set()
        elif p:
            self.send_to(tag, p["pub"], {"op": "denied"})

    def revoke(self, dev_id):
        for d in self.devices:
            if d["hash"] == dev_id:
                self.log("removed device", d["name"])   # revoked here, or the phone chose "Forget" for this Mac
        self.devices = [d for d in self.devices if d["hash"] != dev_id]
        _write_private(DEVICES, self.devices)
        self.ctl({"op": "revoke", "hash": dev_id})
        self.close_lan(lambda h: h == dev_id)

    def close_lan(self, which):
        for ws, h in list(self.lan.items()):
            if which(h):
                threading.Thread(target=lambda w=ws: w.close(4001, "revoked"), daemon=True).start()

    def reset(self):
        """New Mac identity and room: every paired device loses access."""
        self.ctl({"op": "reset"})
        self.devices = []
        _write_private(DEVICES, self.devices)
        self.ident = load_identity(reset=True)
        self.keys.clear()
        self.close_lan(lambda h: True)
        ws = self.ws
        if ws:   # reconnect under the new room; closing waits for the handshake, so not on the caller's thread
            threading.Thread(target=lambda: ws.close(), daemon=True).start()

    # ------------------------------------------------------------ change notices (instead of devices polling)
    def changed(self, path):
        """localstore listener: remember "terminalSessions/<id>"-style paths; flushed together ~0.3 s later."""
        parts = path.strip("/").split("/")
        if len(parts) < 2 or parts[-2] not in PUSH_COLLS:
            return
        with self.changes_lock:
            first = not self.changes
            self.changes.add("/".join(parts[-2:]))
        if first:
            threading.Timer(0.3, self.flush_changes).start()

    def flush_changes(self):
        with self.changes_lock:
            paths, self.changes = sorted(self.changes), set()
        if not paths:
            return
        ev = {"op": "changed", "paths": paths[:200]}
        direct = set()
        for ws, (key, reply) in list(self.lan_out.items()):
            try:
                reply(seal(key, ev))
                direct.add(self.lan.get(ws))
            except Exception:
                pass
        now = time.time()
        for d in self.devices:
            if d["hash"] not in direct and now - self.active.get(d["hash"], 0) < ACTIVE_FOR:
                try:
                    self.send_to("d:" + d["hash"], d["pub"], ev)
                except Exception:
                    pass

    # ------------------------------------------------------------ relay connection
    def ctl(self, msg):
        with self.send_lock:
            if self.ws:
                try:
                    self.ws.send(json.dumps(msg))
                except Exception as e:
                    self.log("tunnel send failed:", e)

    def key_for(self, pub):
        if pub not in self.keys:
            self.keys[pub] = session_key(self.ident, pub)
        return self.keys[pub]

    def send_to(self, tag, pub, obj):
        self.ctl({"to": tag, "data": seal(self.key_for(pub), obj)})

    def run(self):
        backoff = 1
        while True:
            url = f"{self.relay}/room/{self.ident['room']}?role=mac&token={self.ident['token']}"
            try:
                with connect(url, max_size=64 * 1024 * 1024, open_timeout=20, ping_interval=30, ping_timeout=90) as ws:   # slow links: a pong can queue behind big frames
                    self.ws, self.online, backoff = ws, True, 1
                    self.log("tunnel online", self.relay)
                    for d in self.devices:   # the relay keeps these too; re-sending heals a reset room
                        ws.send(json.dumps({"op": "allow", "hash": d["hash"]}))
                    if self.pairing and self.pairing["exp"] > time.time():
                        ws.send(json.dumps({"op": "pair", "hash": self.pairing["hash"], "exp": int(self.pairing["exp"] * 1000)}))
                    beat = threading.Thread(target=self.heartbeat, args=(ws,), daemon=True)
                    beat.start()
                    for raw in ws:
                        self.on_message(raw)
            except Exception as e:
                self.log("tunnel down:", e)
                if getattr(getattr(e, "rcvd", None), "code", 0) == 4000:
                    # Another bridge with this identity took the room: don't fight it every second.
                    self.log("another CLO 2 bridge is using this Mac's identity — retrying in 60 s")
                    backoff = 60
            self.ws, self.online = None, False
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)

    ECHO_EVERY, ECHO_WAIT = 45, 30

    def heartbeat(self, ws):
        """End-to-end check of the relay link: websocket pings only prove the TCP link, not that the relay still routes
        this Mac's frames here. Every 45 s send {op: echo}; the relay answers on the socket it routes to. No answer in
        30 s (once the relay has shown it answers) → close, and the loop above reconnects."""
        n, self.echo_ok = 0, False
        while self.ws is ws:
            time.sleep(self.ECHO_EVERY)
            if self.ws is not ws:
                return
            n += 1
            self.echo_seen = None
            try:
                with self.send_lock:
                    ws.send(json.dumps({"op": "echo", "n": n}))
            except Exception:
                return
            deadline = time.time() + self.ECHO_WAIT
            while time.time() < deadline and self.echo_seen != n and self.ws is ws:
                time.sleep(0.5)
            if self.echo_seen == n:
                self.echo_ok = True
            elif self.echo_ok and self.ws is ws:   # a relay without echo support never answers: don't flap on it
                self.log("tunnel deaf: relay stopped answering the heartbeat — reconnecting")
                try:
                    ws.close()
                except Exception:
                    pass
                return

    def on_message(self, raw):
        if os.environ.get("CLO2_DEBUG"):
            self.log("frame in", len(raw))
        try:
            m = json.loads(raw)
        except ValueError:
            return
        if m.get("op") == "echo":
            self.echo_seen = m.get("n")
            return
        if m.get("op") == "pushResult":
            if self.on_push_result:
                self.pool.submit(self.on_push_result, m)
            return
        tag, data = m.get("from", ""), m.get("data")
        if not data:
            return
        if tag.startswith("p:"):
            self.on_pairing(tag, m.get("pub") or "", data)
        elif tag.startswith("d:"):
            dev = next((d for d in self.devices if d["hash"] == tag[2:]), None)
            if dev:
                self.pool.submit(self.on_request, tag, dev, data)

    def on_pairing(self, tag, pub, data):
        if not self.pairing or self.pairing["exp"] < time.time() or tag[2:] != self.pairing["hash"]:
            return
        try:
            hello = open_frame(self.key_for(pub), data)   # decrypts only if the phone had this Mac's key from the QR
        except Exception:
            return
        if hello.get("op") != "hello":
            return
        name = str(hello.get("name") or "Device")[:60]
        self.pending[tag] = {"pub": pub, "name": name, "check": check_code(self.ident["pub"], pub), "at": time.time()}
        auto = hello.get("a")
        if isinstance(auto, str) and secrets.compare_digest(auto, self.pairing.get("auto", "")):
            self.log("pairing request from", name, "— QR from this screen, approved")
            self.approve(tag)
            return
        if self.known(pub):   # proved it holds a key this Mac already trusts (the hello opened with it)
            self.log("pairing request from", name, "— already a paired device, approved")
            self.approve(tag)
            return
        why = self.pair_refusal("link")
        if why:
            self.pending.pop(tag, None)
            self.log("pairing request from", name, "over a sent link — refused (pair via", self.pair_via + ")")
            self.send_to(tag, pub, {"op": "denied", "why": why})
            return
        self.pending[tag]["via"] = "via a pairing link sent over the internet"
        self.log("pairing request from", name)
        self.ask(tag)

    def drop_old_tokens(self, url, pub):
        """A phone's push token changes (app updates, restores); Apple may keep delivering to the old one, and then every
        turn rang twice — once with the summary, once the old plain way. Its registration names the phone (`pub`), so
        any other token that phone registered here goes."""
        coll, token = url.rsplit("/", 1)
        try:
            status, listing = self.handle("GET", coll + "?pageSize=200", None)
            for d in (listing or {}).get("documents", []) if status == 200 else []:
                other = d["name"].rsplit("/", 1)[1]
                if other != token and d.get("fields", {}).get("pub", {}).get("stringValue") == pub:
                    self.handle("DELETE", f"{coll}/{other}", None)
                    self.log("push: dropped an old token of the same phone")
        except Exception as e:
            self.log("push: old token cleanup failed", e)

    def on_request(self, tag, dev, data, key=None, reply=None, seen=None):
        """One request from a device — via the relay (reply to its tag) or a direct connection (key/reply/seen given)."""
        key = key or self.key_for(dev["pub"])
        try:
            req = open_frame(key, data)
        except Exception:
            return
        rid = req.get("id")
        if seen is not None:   # direct: the same frame twice on one connection is a replay
            if not rid or rid in seen:
                return
            seen.add(rid)
        # The phone resends what was in flight when a link dropped (maybe on the other path): answer again from the
        # cache, never run it twice. None = still running → the first run's answer goes out when it's done.
        send = reply or (lambda frame: self.ctl({"to": tag, "data": frame}))
        if rid:
            k = (dev["hash"], rid)
            with self.done_lock:
                now = time.time()
                if len(self.done) > 500:
                    self.done = {x: v for x, v in self.done.items() if now - v[0] < 600}
                prev = self.done.get(k)
                if prev is None:
                    self.done[k] = (now, None, [])
                elif prev[1] is None:
                    prev[2].append((key, send))
                    return
            if prev is not None:
                status, body = prev[1]
                send(seal(key, {"id": rid, "status": status, "body": body}))
                return
        dev["lastSeen"] = time.time()
        if seen is None:
            self.active[dev["hash"]] = dev["lastSeen"]
        if req.get("op") == "forget":   # "Quên máy Mac này" on the phone
            self.revoke(dev["hash"])
            return
        try:
            op = req.get("op")
            if op == "lan":
                status, body = 200, {"port": self.lan_port, "hosts": lan_addresses() if self.lan_port else [], "ts": self.ts_host}
            elif op == "peers":
                status, body = 200, {"macs": tailnet_macs(self.lan_port or 7789)}
            elif op:
                fn = self.ops.get(op)
                status, body = fn(req, dev) if fn else (400, {"error": f"unknown op {op}"})
            else:
                method, url, body = req.get("method", "GET"), req.get("url", ""), req.get("body")
                # A phone registering for notifications: remember which paired device it is, so the Mac can encrypt
                # the notification text for exactly that phone (only its key opens it).
                reg = method == "PATCH" and re.search(r"/devices/[0-9a-f]{20,200}$", url.split("?")[0]) and isinstance(body, dict)
                if reg:
                    body.setdefault("fields", {})["pub"] = {"stringValue": dev["pub"]}
                status, body = self.handle(method, url, body)
                if reg and status == 200:
                    self.drop_old_tokens(url.split("?")[0], dev["pub"])
        except Exception as e:
            status, body = 500, {"error": str(e)[:500]}
        frame = seal(key, {"id": rid, "status": status, "body": body})
        if os.environ.get("CLO2_DEBUG"):
            self.log("frame out", len(frame), "status", status)
        waiting = []
        if rid:
            with self.done_lock:
                waiting = self.done.get((dev["hash"], rid), (0, None, []))[2]
                self.done[(dev["hash"], rid)] = (time.time(), (status, body), [])
        send(frame)
        for k2, s in waiting:   # the same request resent (other path / after a drop) while this one ran
            try:
                s(seal(k2, {"id": rid, "status": status, "body": body}))
            except Exception:
                pass

    def on_lan_pair(self, ws, first, peer, ts_login=None):
        """A phone on this Wi-Fi asks to pair (found us by Bonjour; no QR). Both screens show the 4-digit check; after
        "Cho phép" here it gets the pairing link sealed to its key and pairs through the relay as if it scanned the QR."""
        pub, name = str(first.get("pub") or ""), str(first.get("name") or "Device")[:60]
        app = re.sub(r"[^\w .()-]", "", str(first.get("app") or "?"))[:30]   # app build: tells old apps apart in the log
        try:
            key = self.key_for(pub)
        except Exception:
            return ws.close(4003, "bad key")
        tail = str(peer).startswith("100.")
        why = self.pair_refusal("tailnet" if tail else "wifi")
        if why:
            self.log("pairing request from", name, peer, "— refused (pair via", self.pair_via + ")")
            ws.send(json.dumps({"op": "denied", "why": why}))
            return ws.close(4003, "denied")
        tag, entry = next(((t, e) for t, e in self.pending.items() if e.get("lan") and e["pub"] == pub), (None, None))
        if entry:   # same phone, same key, back after its connection dropped: the dialog it opened is still up
            self.log("pairing request from", name, peer, f"(app {app}) — reconnected, same dialog")
        else:
            for t, old in list(self.pending.items()):   # the phone asked again with a new key (new code): close the old dialog
                if old.get("lan") and old.get("peer") == peer:
                    self.pending.pop(t, None)
                    old["event"].set()
            tag = "lan:" + secrets.token_hex(6)
            entry = {"peer": peer, "pub": pub, "name": name, "check": check_code(self.ident["pub"], pub), "at": time.time(),
                     "lan": True, "event": threading.Event(), "answer": None, "socks": 0}
            self.pending[tag] = entry
        entry["socks"] += 1
        if entry["event"].is_set():
            pass   # already answered (another socket of this phone is sending the link)
        elif self.known(pub) or self.lan_ok.get(pub, 0) > time.time():
            # the pairing link goes back sealed to this key: only its holder — a device already paired, or one the owner
            # approved a moment ago whose connection then dropped — can use it
            self.log("pairing request from", name, peer, "— already approved, sending the link again")
            entry["answer"] = "ok"
            entry["event"].set()
        elif "via" not in entry:
            # Tailscale already knows whose device it is: say so in the dialog, but "Cho phép" here is still required
            if ts_login is not None:   # through tailscale serve: it vouches for the account (header it sets itself)
                same, node = bool(self.ts_login) and ts_login.lower() == self.ts_login.lower(), f"{peer} · {ts_login}"
            else:
                same, node = tailnet_info(peer, self.log) if str(peer).startswith("100.") else (False, "")
            entry["via"] = (f"via Tailscale · device “{node or peer}” · " + ("same Tailscale account as this Mac" if same
                            else "⚠️ a DIFFERENT Tailscale account")) if str(peer).startswith("100.") else f"on this Wi-Fi ({peer})"
            self.log("pairing request from", name, peer, f"(app {app}) —", entry["via"])
            self.ask(tag)
        try:
            ws.send(json.dumps({"op": "waiting", "check": entry["check"]}))
            while not entry["event"].wait(1):
                if time.time() - entry["at"] > 180:
                    return ws.close(4008, "timeout")
                ws.ping()   # raises once the phone has gone
            if entry["answer"] is None:
                return ws.close(4009, "asked again")
            if entry["answer"] != "ok":
                ws.send(json.dumps({"op": "denied"}))
                return ws.close(4003, "denied")
            if not (self.pairing and self.pairing["exp"] > time.time() + 30):
                self.start_pairing()
            ws.send(json.dumps({"op": "pairlink", "data": seal(key, {"link": self.qr()})}))
            self.log("pairing link sent to", name)
            ws.recv(timeout=30)   # the phone closes once it has it
        except Exception as e:
            if not entry["event"].is_set():   # why phones lose this socket while the owner types the code
                self.log("pairing connection from", name, "ended while waiting:", type(e).__name__, str(e)[:120],
                         f"after {time.time() - entry['at']:.0f} s")
        finally:
            entry["socks"] -= 1

            def drop():   # the phone stayed away: its dialog goes too (a reconnect within the grace time keeps it)
                if entry["socks"] <= 0 and self.pending.get(tag) is entry:
                    self.pending.pop(tag, None)
            if entry["event"].is_set() or time.time() - entry["at"] > 180:
                drop()
            else:
                threading.Timer(LAN_GRACE, drop).start()
            try:
                ws.close()
            except Exception:
                pass

    # ------------------------------------------------------------ direct path (same network)
    def start_lan(self):
        try:
            srv = ws_serve(self.on_lan, "0.0.0.0", self.lan_port, max_size=64 * 1024 * 1024, open_timeout=10, ping_interval=30)
        except OSError as e:
            self.log("direct path off (port", self.lan_port, "):", e)
            self.lan_port = 0
            return
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.log("direct path: ws://<this Mac>:%d" % self.lan_port)
        self.advertise()

    # ------------------------------------------------------------ the web app over Tailscale HTTPS
    def start_ts_web(self):
        """The web app (https) can't open ws:// to this Mac — mixed content. `tailscale serve` gives this Mac a real https
        name on the owner's tailnet (https://<mac>.<tailnet>.ts.net:8443) and forwards it to 127.0.0.1:<ts_web>, where the
        same direct-path protocol runs: pairing (the owner types the 4-digit code here) and, once paired, requests.
        Only tailnet devices reach that name; the peer is the address Tailscale puts in X-Forwarded-For."""
        try:
            srv = ws_serve(self.on_ts_web, "127.0.0.1", self.ts_web, max_size=64 * 1024 * 1024, open_timeout=10, ping_interval=30)
        except OSError as e:
            self.log("web over Tailscale off (port", self.ts_web, "):", e)
            return
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        for attempt in range(6):   # Tailscale may be slow / not up yet after login or a restart: try again for ~15 min
            if self.serve_tailnet(attempt):
                return
            time.sleep(min(600, 30 * 2 ** attempt))

    def serve_tailnet(self, attempt=0):
        """Make sure this Mac's `tailscale serve` entry exists. True when done or when it must not be retried (a setting /
        the tailnet says no). When the CLI can't be used from here (launchd + App Store Tailscale), the name saved by the
        last setup that worked (`clo2 tailscale` in Terminal, or an earlier start) is used: serve config persists."""
        try:
            if self.pair_via == "wifi":
                raise NoTailnetWeb("pairing is set to Wi-Fi only")
            self.ts_host, self.ts_login = setup_ts_web(self.ts_web)
            self.log("web over Tailscale:", f"wss://{self.ts_host}")
            return True
        except Exception as e:
            final = isinstance(e, NoTailnetWeb)
            saved = {} if final else ts_web_saved(self.ts_web)
            if saved:
                self.ts_host, self.ts_login = saved["host"], saved.get("login", "")
                self.log("web over Tailscale:", f"wss://{self.ts_host}", "(saved setup; CLI here:", str(e)[:120] + ")")
                return True
            self.log("web over Tailscale off:", type(e).__name__, str(e)[:300], "" if final else
                     f"(try {attempt + 1}, again later; or run `clo2 tailscale` in Terminal once)")
            return final

    def on_ts_web(self, ws):
        headers = ws.request.headers
        hops = [h.strip() for h in ",".join(headers.get_all("X-Forwarded-For")).split(",") if h.strip()]
        peer = hops[-1] if hops else ""   # the last hop is the one tailscale serve added (a client can only prepend)
        if headers.get("Origin", "").rstrip("/") not in self.web_origins or not peer.startswith("100.") or not headers.get("Tailscale-User-Login"):
            self.log("web over Tailscale refused:", headers.get("Origin", "?"), peer or "no peer")
            return ws.close(4003, "forbidden")
        return self.on_lan(ws, peer, headers.get("Tailscale-User-Login", ""))

    def advertise(self):
        """Bonjour `_clo2._tcp` so the app finds this Mac on the same Wi-Fi without a QR. TXT carries the addresses
        (the app connects to them directly) and the Mac's public key prefix; nothing secret."""
        if os.environ.get("CLO2_NO_BONJOUR"):
            return
        try:
            txt = [f"v=1", f"p={self.lan_port}", f"h={','.join(lan_addresses())}", f"k={self.ident['pub'][:16]}"]
            # dns-sd keeps advertising after its parent dies (crash, SIGKILL, a test killed): the wrapper ends it once
            # this bridge is gone, so no ghost "Mac" stays in the app's Wi-Fi list.
            reg = 'avahi-publish -s "$1" "$2" "$4" "${@:5}"' if LINUX else 'dns-sd -R "$@"'   # name type [domain] port txt…
            watch = (reg + ' & d=$!; '
                     'while [ "$(ps -o ppid= -p $$ | tr -d " ")" = "%d" ] && kill -0 $d 2>/dev/null; do sleep 3; done; kill $d 2>/dev/null'
                     % os.getpid())   # our parent changed (re-parented to launchd) = the bridge is gone, even if not yet reaped
            self.bonjour = subprocess.Popen(["/bin/bash", "-c", watch, "clo2-bonjour", self.label, "_clo2._tcp", "local", str(self.lan_port), *txt],
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            import atexit
            atexit.register(self.bonjour.terminate)
        except Exception as e:
            self.log("bonjour off:", e)

    def on_lan(self, ws, peer=None, ts_login=None):
        """{op:"challenge", c} → device {pub, data: seal(k, {op:"auth"})} → {data: seal(k, {op:"ok", name})} → requests."""
        challenge = secrets.token_bytes(16)
        peer = peer or (getattr(ws, "remote_address", None) or ("?",))[0]
        try:
            ws.send(json.dumps({"op": "challenge", "c": b64u(challenge), "k": self.ident["pub"], "n": self.label}))
            first = json.loads(ws.recv(timeout=10))
            if first.get("op") == "pairreq":
                return self.on_lan_pair(ws, first, peer, ts_login)
            pub = str(first.get("pub") or "")
            dev = next((d for d in self.devices if d["pub"] == pub), None)
            if not dev:
                self.log("direct connection refused (not paired) from", peer)
                return ws.close(4003, "not paired")
            key = lan_key(self.key_for(pub), challenge)
            if open_frame(key, first.get("data") or "").get("op") != "auth":
                self.log("direct connection refused (bad auth) from", peer)
                return ws.close(4003, "bad auth")
        except Exception as e:
            if type(e).__name__ != "ConnectionClosedOK":   # another CLO 2 Mac listing the tailnet just looks and leaves
                self.log("direct connection failed from", peer, "-", type(e).__name__)
            return ws.close(4003, "bad auth")
        lock, seen = threading.Lock(), set()

        def reply(frame):
            with lock:
                ws.send(json.dumps({"data": frame}))
        reply(seal(key, {"op": "ok", "name": self.label}))
        self.lan[ws] = dev["hash"]
        self.lan_out[ws] = (key, reply)
        self.log("direct connection from", dev["name"], "via", "Tailscale" if str(peer).startswith("100.") else "Wi-Fi/LAN", peer)
        try:
            for raw in ws:
                if not any(d["hash"] == dev["hash"] for d in self.devices):   # revoked meanwhile
                    break
                try:
                    data = json.loads(raw).get("data")
                except (ValueError, AttributeError):
                    continue
                if isinstance(data, str):
                    self.pool.submit(self.on_request, "", dev, data, key, reply, seen)
        except Exception:
            pass
        finally:
            self.lan.pop(ws, None)
            self.lan_out.pop(ws, None)
            try:
                ws.close()
            except Exception:
                pass
