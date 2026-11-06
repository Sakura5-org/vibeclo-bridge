"""CLO 2 direct path (same network): discovery through the relay, challenge/auth, requests, replay, wrong keys,
revoke/reset closing it, and speed vs the relay. Throwaway bridge (harness)."""
import base64, json, os, secrets, sys, threading, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Bridge, check, done, recv, b64u, unb64u, seal, open_frame
from websockets.sync.client import connect
from websockets.exceptions import ConnectionClosed
from tunnel import lan_key

b = Bridge(7794, "lan")
d = b.pair("LAN phone")
check("paired", getattr(d, "token", None))

r = d.op("lan")
info = r["body"]
check("op lan through the relay → port + hosts", r["status"] == 200 and info["port"] == 7894 and isinstance(info["hosts"], list), info)
check("hosts are private/tailnet IPv4 only", all(not h.startswith(("127.", "169.254.")) for h in info["hosts"]), info["hosts"])


class Lan:
    def __init__(self, dev, host="127.0.0.1", pub=None, key=None):
        self.ws = connect(f"ws://{host}:{info['port']}", max_size=None, open_timeout=5)
        hello = json.loads(self.ws.recv(timeout=5))
        self.c = unb64u(hello["c"])
        self.key = lan_key(key or dev.key, self.c)
        self.ws.send(json.dumps({"pub": pub or dev.pub, "data": seal(self.key, {"op": "auth"})}))

    def ok(self):
        return open_frame(self.key, json.loads(self.ws.recv(timeout=5))["data"])

    def call(self, obj, t=30):
        rid = obj.setdefault("id", secrets.token_hex(6))
        self.ws.send(json.dumps({"data": seal(self.key, obj)}))
        while True:
            m = json.loads(self.ws.recv(timeout=t))
            r = open_frame(self.key, m["data"])
            if r.get("id") == rid:
                return r


def closed_code(ws, t=5):
    try:
        while True:
            ws.recv(timeout=t)
    except ConnectionClosed as e:
        return e.rcvd.code if e.rcvd else None
    except TimeoutError:
        return "open"


# ---- handshake
l = Lan(d)
ok = l.ok()
check("auth → Mac proves itself with the per-connection key", ok.get("op") == "ok" and ok.get("name") == "Test Mac", ok)
r = l.call({"method": "GET", "url": "clo/local/terminalSessions?pageSize=5"})
check("store request over the direct path", r["status"] == 200, r.get("status"))
r = l.call({"op": "lan"})
check("tunnel op over the direct path", r["status"] == 200 and r["body"]["port"] == 7894)

# ---- replay: the same frame twice on one connection → one answer
frame = seal(l.key, {"id": "replay1", "method": "GET", "url": "clo/local/terminalSessions?pageSize=1"})
l.ws.send(json.dumps({"data": frame})); l.ws.send(json.dumps({"data": frame}))
got = []
try:
    while True:
        got.append(open_frame(l.key, json.loads(l.ws.recv(timeout=3))["data"]).get("id"))
except TimeoutError:
    pass
check("replayed frame on the same connection is ignored", got.count("replay1") == 1, got)

# ---- a frame recorded on one connection can't be replayed into another
l2 = Lan(d); l2.ok()
l2.ws.send(json.dumps({"data": frame}))
try:
    l2.ws.recv(timeout=3); replay_other = "answered"
except TimeoutError:
    replay_other = "ignored"
check("frame from another connection is ignored (different key)", replay_other == "ignored", replay_other)

# ---- wrong identities
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives import serialization
stranger = b64u(X25519PrivateKey.generate().public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
bad = Lan(d, pub=stranger)
check("unpaired public key → closed 4003", closed_code(bad.ws) == 4003)
bad = Lan(d, key=os.urandom(32))
check("paired pub but wrong key (can't decrypt auth) → closed 4003", closed_code(bad.ws) == 4003)
ws = connect(f"ws://127.0.0.1:{info['port']}", open_timeout=5); ws.recv(timeout=5); ws.send("garbage")
check("garbage first message → closed 4003", closed_code(ws) == 4003)
ws = connect(f"ws://127.0.0.1:{info['port']}", open_timeout=5); ws.recv(timeout=5)
check("silent client → closed after the 10 s auth window", closed_code(ws, 15) == 4003)
l3 = Lan(d); l3.ok()
l3.ws.send(json.dumps({"data": "not-a-frame"})); l3.ws.send("{oops")
check("garbage after auth doesn't kill the connection", l3.call({"op": "lan"})["status"] == 200)

# ---- the Mac's LAN IP (not just loopback) answers too
if info["hosts"]:
    try:
        lh = Lan(d, host=info["hosts"][0]); check(f"direct path on {info['hosts'][0]}", lh.ok().get("op") == "ok")
        lh.ws.close()
    except Exception as e:
        check(f"direct path on {info['hosts'][0]}", False, e)

# ---- 20 requests in flight at once on one connection (answered in any order, each matched by id)
for i in range(20):
    l3.ws.send(json.dumps({"data": seal(l3.key, {"id": f"c{i}", "method": "GET", "url": "clo/local/terminalSessions?pageSize=3"})}))
res = {}
try:
    while len(res) < 20:
        r = open_frame(l3.key, json.loads(l3.ws.recv(timeout=30))["data"])
        res[r["id"]] = r["status"]
except TimeoutError:
    pass
check("20 requests in flight all answered", sorted(res.values()) == [200] * 20 and len(res) == 20, res)

# ---- speed: 4 MB upload direct vs 1 MB through the relay
def upload(call, size, rid):
    data = os.urandom(size); chunk = 384 * 1024; n = (size + chunk - 1) // chunk
    t = time.time()
    for i in range(n):
        r = call({"op": "upload", "rid": rid, "name": "blob.bin", "index": i, "total": n,
                  "data": base64.b64encode(data[i * chunk:(i + 1) * chunk]).decode()})
        assert r["status"] == 200, r
    return size / (time.time() - t) / 1024
lan_kbs = upload(lambda o: l3.call(o, 60), 4 * 1024 * 1024, "lanspeed1")
relay_kbs = upload(lambda o: d.op(o.pop("op"), **o), 1024 * 1024, "relayspeed1")
print(f"   direct {lan_kbs:.0f} KB/s · relay {relay_kbs:.0f} KB/s")
check("direct path much faster than the relay", lan_kbs > 3 * relay_kbs, (lan_kbs, relay_kbs))
os.system("rm -rf ~/.clo2-uploads/lanspeed1 ~/.clo2-uploads/relayspeed1")

# ---- revoke closes direct connections; reconnecting is refused
devs = b.state()["devices"]
b.post("/api/revoke", {"id": devs[0]["id"]})
check("revoke → direct connection closed 4001", closed_code(l3.ws) == 4001)
again = Lan(d)
check("revoked device can't open a direct connection", closed_code(again.ws) == 4003)

# ---- reset closes everyone's direct connections
d2 = b.pair("LAN phone 2")
l4 = Lan(d2); l4.ok()
b.post("/api/reset")
check("reset → direct connection closed", closed_code(l4.ws) == 4001)

# ---- lan_port 0 → no listener, op lan says so
b0 = Bridge(7793, "lan0", {"lan_port": 0})
d0 = b0.pair("no-lan")
r = d0.op("lan")
check("lan_port 0 → op lan reports no direct path", r["body"] == {"port": 0, "hosts": []}, r["body"])
done()
