"""CLO 2 web app over Tailscale HTTPS: `tailscale serve` forwards https://<mac>.ts.net:8443 to 127.0.0.1:<ts_web> and adds
X-Forwarded-For (+ Tailscale-User-Login). Here the test plays tailscale serve: only the web app's origin with a tailnet
peer gets in; a client can't fake the peer (the last X-Forwarded-For hop counts); pairing waits for the owner, then hands
back a link; the paired browser then talks to the Mac directly over that same port. Throwaway bridge + the real relay."""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Bridge, check, done, recv, b64u, unb64u, seal, open_frame
from websockets.sync.client import connect
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from tunnel import check_code, lan_key

PORT = 7999
WEB = "https://clo.sakura5.app"
b = Bridge(7798, "tsweb", {"ts_web": PORT}, extra_env={"CLO2_NO_TAILNET": "1", "CLO2_LAN_GRACE": "4"})


def keypair(mac_pub):
    priv = X25519PrivateKey.generate()
    pub = b64u(priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"clo2-v1", info=unb64u(mac_pub) + unb64u(pub)).derive(
        priv.exchange(X25519PublicKey.from_public_bytes(unb64u(mac_pub))))
    return pub, key


def ts(origin=WEB, xff="100.101.102.103", login="owner@example.com"):
    h = {}
    if origin: h["Origin"] = origin
    if xff: h["X-Forwarded-For"] = xff
    if login: h["Tailscale-User-Login"] = login
    return connect(f"ws://127.0.0.1:{PORT}", additional_headers=h, open_timeout=5, max_size=None)


def refused(**kw):
    try:
        ws = ts(**kw)
        ws.recv(timeout=5)
        return False
    except Exception as e:
        return "4003" in str(e) or type(e).__name__.startswith("ConnectionClosed")


def pending(name, n=20):
    for _ in range(n):
        x = [p for p in b.state()["pending"] if p["name"] == name]
        if x: return x[0]
        time.sleep(0.3)


check("no Tailscale headers (a local client hitting the port) → refused", refused(origin=WEB, xff="", login=""))
check("another website's origin → refused", refused(origin="https://evil.example"))
check("peer not on the tailnet → refused", refused(xff="192.168.1.20"))
check("client prepends a fake tailnet hop, real last hop isn't → refused", refused(xff="100.64.0.9, 192.168.1.20"))
check("no Tailscale-User-Login (tagged node / not via serve) → refused", refused(login=""))

# pairing: challenge → pairreq → waits for the owner → link sealed to this browser's key
w = ts(xff="203.0.113.7, 100.101.102.103")   # a client-prepended hop is ignored; the last one is the real peer
ch = json.loads(w.recv(timeout=5))
check("challenge with the Mac's key + name", ch.get("op") == "challenge" and ch.get("k") and ch.get("n") == "Test Mac", ch)
pub, key = keypair(ch["k"])
w.send(json.dumps({"op": "pairreq", "pub": pub, "name": "Chrome on laptop", "app": "web"}))
wait = json.loads(w.recv(timeout=5))
check("browser told to wait, with the 4 digits the Mac shows", wait.get("op") == "waiting" and wait.get("check") == check_code(ch["k"], pub), wait)
p = pending("Chrome on laptop")
check("request shows on the Mac, from the tailnet peer", p and "Tailscale" in p.get("via", "") and "100.101.102.103" in p.get("via", ""), p)
b.post("/api/approve", {"tag": p["tag"]})
m = json.loads(w.recv(timeout=10)); w.close()
link = open_frame(key, m["data"])["link"] if m.get("op") == "pairlink" else ""
lp = json.loads(unb64u(link.split("d=", 1)[1])) if link else {}
check("after the owner approves: pairing link sealed to the browser, auto-approving", lp.get("a") and lp.get("k") == ch["k"], m)

# finish through the relay with that link (as the web app does), then go direct over the Tailscale port
ws = connect(f"wss://{lp['r']}/room/{lp['room']}?role=dev&token={lp['c']}", max_size=None)
recv(ws)
ws.send(json.dumps({"pub": pub, "data": seal(key, {"op": "hello", "name": "Chrome on laptop", "a": lp["a"]})}))
r = {}
for _ in range(5):
    x = recv(ws, 15)
    if x.get("data"):
        r = open_frame(key, x["data"]); break
ws.close()
check("paired through the relay, no second approval", r.get("op") == "paired" and r.get("token"), r)

d = ts()
c = unb64u(json.loads(d.recv(timeout=5))["c"])
lk = lan_key(key, c)
d.send(json.dumps({"pub": pub, "data": seal(lk, {"op": "auth"})}))
ok = open_frame(lk, json.loads(d.recv(timeout=5))["data"])
check("paired browser → direct link over the Tailscale port", ok.get("op") == "ok", ok)
d.send(json.dumps({"data": seal(lk, {"id": "r1", "op": "lan"})}))
for _ in range(10):
    a = open_frame(lk, json.loads(d.recv(timeout=10))["data"])
    if a.get("id") == "r1": break
check("op lan answers over it, with the ts name field", a.get("status") == 200 and "ts" in a.get("body", {}), a)
d.send(json.dumps({"data": seal(lk, {"id": "r2", "method": "GET", "url": "clo/local/terminalSessions?pageSize=1"})}))
for _ in range(10):
    a = open_frame(lk, json.loads(d.recv(timeout=10))["data"])
    if a.get("id") == "r2": break
check("store request over the direct link", a.get("status") == 200, a)
d.close()

# an unpaired key can't use the direct link
x = ts()
c = unb64u(json.loads(x.recv(timeout=5))["c"])
opub, okey = keypair(ch["k"])
x.send(json.dumps({"pub": opub, "data": seal(lan_key(okey, c), {"op": "auth"})}))
try:
    x.recv(timeout=5); closed = False
except Exception:
    closed = True
check("unpaired key → direct link refused", closed)
done()
