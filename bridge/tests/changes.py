"""CLO 2 change notices + easy pairing: a write to a session / inbox reaches active devices as {op:"changed"} (relay
and direct path), idle devices aren't woken, other collections stay quiet; /api/localpair (same Mac, web app origin
only) gives a link the Mac approves at once; Tailscale same-user check. Throwaway bridge (harness)."""
import json, os, secrets, sys, time, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Bridge, Device, check, done, recv, unb64u, seal, open_frame
from websockets.sync.client import connect
from tunnel import lan_key, tailnet_same_user

b = Bridge(7796, "changes")
d = b.pair("Change phone")
idle = b.pair("Idle phone")
check("paired", getattr(d, "token", None) and getattr(idle, "token", None))


def notices(ws, key, t=3):
    """Every {op: changed} frame on `ws` for `t` seconds → the paths in them."""
    paths, end = [], time.time() + t
    while time.time() < end:
        try:
            m = json.loads(ws.recv(timeout=max(0.1, end - time.time())))
        except TimeoutError:
            break
        if "data" in m:
            r = open_frame(key, m["data"])
            if r.get("op") == "changed":
                paths += r["paths"]
    return paths


def put(dev, path, rid=None):
    return dev.call("PATCH", f"clo/local/{path}", {"fields": {"title": {"stringValue": secrets.token_hex(3)}}}, rid=rid)


# The device becomes "active" by making a request; the idle one never asks anything after pairing.
check("request ok", put(d, "terminalSessions/s1")["status"] == 200)
got = notices(d.ws, d.key)
check("own write comes back as a notice", "terminalSessions/s1" in got, got)
check("idle device gets nothing", notices(idle.ws, idle.key, 1) == [])

# Several writes inside the coalescing window → one frame, all paths. Sent back to back without waiting for the
# replies: dev.call() drops frames that aren't its reply, so with a slow relay (> 0.3 s a round trip) the notices
# for the first writes used to arrive while it waited and were thrown away — the test failed, not the bridge.
for i in range(5):
    d.send({"id": secrets.token_hex(4), "method": "PATCH", "url": f"clo/local/terminalInbox/r{i}",
            "body": {"fields": {"title": {"stringValue": secrets.token_hex(3)}}}})
got = notices(d.ws, d.key, 8)   # the relay can take ~1 s a hop; the frame comes ~0.3 s after the last write lands
check("inbox writes noticed", all(f"terminalInbox/r{i}" in got for i in range(5)), got)

put(d, "bridge/x")
put(d, "skills/x")
got = notices(d.ws, d.key, 2)
check("other collections stay quiet", got == [], got)

# Direct path: notices go there, not through the relay.
info = d.op("lan")["body"]
lan = connect(f"ws://127.0.0.1:{info['port']}", max_size=None, open_timeout=5)
c = unb64u(json.loads(lan.recv(timeout=5))["c"])
lk = lan_key(d.key, c)
lan.send(json.dumps({"pub": d.pub, "data": seal(lk, {"op": "auth"})}))
check("direct auth ok", open_frame(lk, json.loads(lan.recv(timeout=5))["data"]).get("op") == "ok")
put(idle, "terminalSessions/s2")   # someone else writes
check("direct connection hears it", "terminalSessions/s2" in notices(lan, lk), "")
check("…and the relay doesn't repeat it for that device", "terminalSessions/s2" not in notices(d.ws, d.key, 1))
lan.close()

# Same Mac: local pairing link.
def get(path, origin=None, method="GET"):
    h = {"Origin": origin} if origin else {}
    try:
        r = urllib.request.urlopen(urllib.request.Request(b.page + path, headers=h, method=method), timeout=10)
        return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()

st, h, body = get("/api/localpair", "https://evil.example")
check("localpair refuses other sites", st == 403 and "Access-Control-Allow-Origin" not in h, st)
st, h, _ = get("/api/localpair", "https://clo.sakura5.app", "OPTIONS")
check("preflight allows the web app (Private Network Access)",
      st == 204 and h.get("Access-Control-Allow-Origin") == "https://clo.sakura5.app" and h.get("Access-Control-Allow-Private-Network") == "true", h)
st, h, body = get("/api/localpair", "https://clo.sakura5.app")
link = json.loads(body)["link"]
p = json.loads(unb64u(link.split("d=", 1)[1]))
check("localpair link carries auto-approve", st == 200 and p.get("a") and h.get("Access-Control-Allow-Origin") == "https://clo.sakura5.app", p)

# A browser using it is paired without "Cho phép".
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from harness import b64u
priv = X25519PrivateKey.generate()
pub = b64u(priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
key = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"clo2-v1", info=unb64u(p["k"]) + unb64u(pub)).derive(
    priv.exchange(X25519PublicKey.from_public_bytes(unb64u(p["k"]))))
time.sleep(1)
ws = connect(f"wss://{p['r']}/room/{p['room']}?role=dev&token={p['c']}", max_size=None)
recv(ws)
ws.send(json.dumps({"pub": pub, "data": seal(key, {"op": "hello", "name": "Local browser", "a": p["a"]})}))
reply = open_frame(key, recv(ws, 15)["data"])
check("same-Mac browser paired at once", reply.get("op") == "paired" and reply.get("token"), reply)
ws.close()

# Tailscale: this Mac's own address is "same user"; non-tailnet addresses never are.
own = None
try:
    st = json.loads(os.popen("/Applications/Tailscale.app/Contents/MacOS/Tailscale status --json 2>/dev/null").read())
    own = st["Self"]["TailscaleIPs"][0]
    other = next((x["TailscaleIPs"][0] for x in st["Peer"].values() if x.get("UserID") != st["Self"]["UserID"]), None)
except Exception:
    other = None
if own:
    check("tailnet: this Mac's own user → same", tailnet_same_user(own))
    if other:
        check("tailnet: a node shared from another user → not same", not tailnet_same_user(other), other)
else:
    print("SKIP tailnet checks (Tailscale not running)")
r = d.op("peers", t=30)
check("op peers → list of {name, host, k}", r["status"] == 200 and all({"name", "host", "k"} <= set(m) for m in r["body"]["macs"]), r)
check("non-tailnet address → never auto", not tailnet_same_user("192.168.1.5") and not tailnet_same_user("127.0.0.1"))

done()
