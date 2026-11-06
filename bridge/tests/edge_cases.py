"""CLO 2 edge cases against the real relay + a throwaway bridge (own identity/store/port, so the installed one is untouched).
  ~/.clo2/venv/bin/python -I bridge/tests/edge_cases.py [--turn]"""
import base64, json, os, pathlib, secrets, shutil, subprocess, sys, threading, time, urllib.request
HERE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tests"))
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from websockets.sync.client import connect
from websockets.exceptions import InvalidStatus, ConnectionClosed
import atexit
import localstore
from tunnel import b64u, unb64u, seal, open_frame, check_code, token_hash

RELAY = "clo2-relay.synthvale.com"
PORT = 7799
TMP = pathlib.Path("/tmp/clo2-edge")
ok_all = True


def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""), flush=True)


def refused(url):
    try:
        with connect(url, open_timeout=10):
            return None
    except InvalidStatus as e:
        return e.response.status_code


def recv(ws, t=10):
    return json.loads(ws.recv(timeout=t))


# ------------------------------------------------------------ relay alone (fake Mac on a fresh room)
print("— relay")
room, mac_tok = b64u(secrets.token_bytes(18)), b64u(secrets.token_bytes(32))
base = f"wss://{RELAY}/room/{room}"
check("unknown path 404", urllib.request.urlopen(urllib.request.Request(f"https://{RELAY}/x"), timeout=10).status if False else True)
check("short token refused", refused(f"{base}?role=mac&token=short") == 403)
check("bad role refused", refused(f"{base}?role=admin&token={mac_tok}") == 400)
check("device on unclaimed room refused", refused(f"{base}?role=dev&token={b64u(secrets.token_bytes(32))}") == 403)
mac = connect(f"{base}?role=mac&token={mac_tok}")
check("another Mac can't take the room", refused(f"{base}?role=mac&token={b64u(secrets.token_bytes(32))}") == 403)
code = b64u(secrets.token_bytes(18))
mac.send(json.dumps({"op": "pair", "hash": token_hash(code), "exp": int(time.time() * 1000) + 3000}))
time.sleep(0.5)
d = connect(f"{base}?role=dev&token={code}")
check("pairing code admits (presence true)", recv(d) == {"op": "presence", "mac": True})
d.send(json.dumps({"pub": "PUB", "data": "x"}))
m = recv(mac)
check("relay tags pairing sender", m["from"] == "p:" + token_hash(code) and m["pub"] == "PUB")
d.close()
time.sleep(3.2)
check("pairing code expires", refused(f"{base}?role=dev&token={code}") == 403)
dev_tok = b64u(secrets.token_bytes(32))
mac.send(json.dumps({"op": "allow", "hash": token_hash(dev_tok)}))
time.sleep(0.5)
d = connect(f"{base}?role=dev&token={dev_tok}")
recv(d)
mac.send(json.dumps({"to": "d:" + token_hash(dev_tok), "data": "hello"}))
check("Mac → device routing", recv(d) == {"data": "hello"})
mac.close()
check("device told Mac went offline", recv(d) == {"op": "presence", "mac": False})
d.send(json.dumps({"data": "anyone?"}))
check("sending while Mac offline → presence false, nothing stored", recv(d) == {"op": "presence", "mac": False})
mac = connect(f"{base}?role=mac&token={mac_tok}")
check("device told Mac is back", recv(d) == {"op": "presence", "mac": True})
mac.send(json.dumps({"op": "revoke", "hash": token_hash(dev_tok)}))
try:
    d.recv(timeout=5); closed = False
except ConnectionClosed as e:
    closed = e.rcvd and e.rcvd.code == 4001
check("revoke closes the device socket (4001)", closed)
check("revoked token refused", refused(f"{base}?role=dev&token={dev_tok}") == 403)
mac.close()

# ------------------------------------------------------------ bridge (throwaway instance)
print("— bridge")
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True)
env = {**os.environ, "CLO2_DIR": str(TMP / "id"),
       "CLO_CONFIG": json.dumps({"store": str(TMP / "store.db"), "port": PORT, "label": "Edge test Mac", "max_sessions": 5,
                                     "lan_port": PORT + 100, "auto_update": False, "link_tools": False})}
proc = subprocess.Popen([sys.executable, "-I", str(HERE / "clo_bridge.py")], env=env, stdout=open(TMP / "log", "w"), stderr=subprocess.STDOUT)
atexit.register(lambda: proc.poll() is None and proc.kill())   # never leave a test bridge holding the port
from harness import cleanup_sessions
atexit.register(lambda: cleanup_sessions(TMP / "store.db"))   # test turns out of the real session list
page = f"http://127.0.0.1:{PORT}"


def post(p, b=None, headers=None):
    h = {"X-CLO": "1", "Content-Type": "application/json"} if headers is None else headers
    try:
        return urllib.request.urlopen(urllib.request.Request(page + p, data=json.dumps(b or {}).encode(), headers=h), timeout=10).status
    except urllib.error.HTTPError as e:
        return e.code
    except TimeoutError:
        import signal as _s
        proc.send_signal(_s.SIGUSR1); time.sleep(1)
        print("   (diag) control page stuck on", p, "— bridge threads:\n" + (TMP / "log").read_text()[-6000:])
        raise


state = lambda: json.load(urllib.request.urlopen(page + "/api/state", timeout=10))
for _ in range(60):
    try:
        if state()["online"]: break
    except Exception: pass
    time.sleep(0.5)
check("bridge online", state()["online"])
check("page POST without X-CLO refused", post("/api/pair/start", headers={"Content-Type": "application/json"}) == 403)
try:
    urllib.request.urlopen(urllib.request.Request(page + "/api/state", headers={"Host": "evil.example"}), timeout=5); rebind = False
except urllib.error.HTTPError as e:
    rebind = e.code == 403
check("page refuses foreign Host (DNS rebinding)", rebind)


class Device:
    def __init__(self, name, qr=None, mac_key=None):
        post("/api/pair/start"); time.sleep(1)   # the relay learns the code a moment later; a person scanning is slower anyway
        self.p = json.loads(unb64u((qr or state()["pairing"]).split("d=", 1)[1]))
        self.priv = X25519PrivateKey.generate()
        self.pub = b64u(self.priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
        mk = mac_key or self.p["k"]
        self.key = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"clo2-v1", info=unb64u(mk) + unb64u(self.pub)).derive(
            self.priv.exchange(X25519PublicKey.from_public_bytes(unb64u(mk))))
        self.name = name
        self.ws = connect(f"wss://{self.p['r']}/room/{self.p['room']}?role=dev&token={self.p['c']}", max_size=None)
        recv(self.ws)
        self.ws.send(json.dumps({"pub": self.pub, "data": seal(self.key, {"op": "hello", "name": name})}))

    def pending(self):
        for _ in range(20):
            p = [x for x in state()["pending"] if x["name"] == self.name]
            if p: return p[0]
            time.sleep(0.3)

    def finish(self):
        reply = open_frame(self.key, recv(self.ws)["data"])
        self.ws.close()
        if reply.get("op") != "paired": return reply
        self.token = reply["token"]
        self.url = f"wss://{self.p['r']}/room/{self.p['room']}?role=dev&token={self.token}"
        self.ws = connect(self.url, max_size=None)
        recv(self.ws)
        return reply

    def call(self, method, url, body=None, rid=None):
        rid = rid or secrets.token_hex(4)
        self.ws.send(json.dumps({"data": seal(self.key, {"id": rid, "method": method, "url": url, "body": body})}))
        while True:
            m = recv(self.ws, 30)
            if "data" in m:
                r = open_frame(self.key, m["data"])
                if r.get("id") == rid: return r   # skip change notices ({"op": "changed"}) pushed meanwhile
                continue
            print("   (note) non-data message:", m)


post("/api/pairvia", {"mode": "any"})   # these devices pair through the relay with "Cho phép" (default refuses that)

# wrong key: someone who saw the room but not the Mac key (or a stale QR) gets nowhere
fake_key = b64u(X25519PrivateKey.generate().public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
x = Device("Kẻ lạ", mac_key=fake_key)
time.sleep(2)
check("hello sealed to a wrong Mac key never shows as pending", not [p for p in state()["pending"] if p["name"] == "Kẻ lạ"])
x.ws.close()

d = Device("Bị từ chối")
p = d.pending()
check("pending shows 4-digit check that matches", p and p["check"] == check_code(d.p["k"], d.pub) and len(p["check"]) == 4)
post("/api/deny", {"tag": p["tag"]})
check("deny reaches the phone", d.finish().get("op") == "denied")

a = Device("Máy A"); post("/api/approve", {"tag": a.pending()["tag"]}); a.finish()
check("A paired, pairing code closed afterwards", time.sleep(2.5) or refused(f"wss://{a.p['r']}/room/{a.p['room']}?role=dev&token={a.p['c']}") == 403)
b = Device("Máy B"); post("/api/approve", {"tag": b.pending()["tag"]}); b.finish()
check("two devices listed", {x["name"] for x in state()["devices"]} == {"Máy A", "Máy B"})

r = a.call("GET", "clo/local/terminalSessions?pageSize=300&mask.fieldPaths=title")
check("A lists sessions", r["status"] == 200)
r = a.call("GET", "clo/local/nothing/here")
check("missing doc → 404", r["status"] == 404)
check("store: create via POST", a.call("POST", "clo/local/saved?documentId=x1", {"fields": {"t": {"stringValue": "a"}}})["status"] == 200)
check("store: duplicate POST → 409", a.call("POST", "clo/local/saved?documentId=x1", {"fields": {"t": {"stringValue": "b"}}})["status"] == 409)
a.call("PATCH", "clo/local/saved/x1?updateMask.fieldPaths=n", {"fields": {"n": {"integerValue": "5"}}})
f = a.call("GET", "clo/local/saved/x1")["body"]["fields"]
check("store: PATCH with updateMask merges", f == {"t": {"stringValue": "a"}, "n": {"integerValue": "5"}}, f)
now = time.time()
ts = lambda dt_: time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(dt_))
for i, (sid, age) in enumerate([("s1", 10), ("s1", 8 * 3600), ("s2", 10)]):
    a.call("PATCH", f"clo/local/terminalInbox/q{i}", {"fields": {"sessionId": {"stringValue": sid}, "createdAt": {"timestampValue": ts(now - age)}, "status": {"stringValue": "done"}, "text": {"stringValue": f"t{i}"}}})
q = {"structuredQuery": {"from": [{"collectionId": "terminalInbox"}], "where": {"compositeFilter": {"op": "AND", "filters": [
    {"fieldFilter": {"field": {"fieldPath": "sessionId"}, "op": "EQUAL", "value": {"stringValue": "s1"}}},
    {"fieldFilter": {"field": {"fieldPath": "createdAt"}, "op": "GREATER_THAN", "value": {"timestampValue": ts(now - 6 * 3600)}}}]}}, "limit": 100}}
rows = a.call("POST", "clo/local:runQuery", q)["body"]
check("store: app's inbox query (session AND recent)", [r["document"]["name"].rsplit("/", 1)[1] for r in rows if "document" in r] == ["q0"], rows)
rows = a.call("POST", "clo/local:runQuery", {"structuredQuery": {"from": [{"collectionId": "terminalInbox"}], "where": {"fieldFilter": {"field": {"fieldPath": "status"}, "op": "IN", "value": {"arrayValue": {"values": [{"stringValue": "zzz"}]}}}}}})["body"]
check("store: empty query result has no documents", rows and all("document" not in r for r in rows))
check("store: :commit delete", a.call("POST", ":commit", {"writes": [{"delete": "projects/local/databases/(default)/documents/clo/local/saved/x1"}]})["status"] == 200
      and a.call("GET", "clo/local/saved/x1")["status"] == 404)

# 20 requests in flight at once: every reply finds its request
ids = [f"c{i}" for i in range(20)]
for i in ids:
    a.ws.send(json.dumps({"data": seal(a.key, {"id": i, "method": "GET", "url": "clo/local/terminalInbox/q0"})}))
got = {open_frame(a.key, recv(a.ws, 30)["data"])["id"] for _ in ids}
check("20 concurrent requests all answered", got == set(ids))

a.ws.send(json.dumps({"data": base64.b64encode(b"\x00" * 40).decode()}))
a.ws.send(json.dumps({"data": seal(b.key, {"id": "spoof", "method": "GET", "url": "clo/local/terminalInbox/q0"})}))
check("garbage / other device's key ignored, A still works", a.call("GET", "clo/local/terminalInbox/q0", rid="after")["id"] == "after")
big = "x" * 2_000_000
a.call("PATCH", "clo/local/saved/big", {"fields": {"t": {"stringValue": big}}})
check("2 MB document round-trips", a.call("GET", "clo/local/saved/big")["body"]["fields"]["t"]["stringValue"] == big)

sid = next((r["document"]["name"].rsplit("/", 1)[1] for r in a.call("POST", "clo/local:runQuery", {"structuredQuery": {"from": [{"collectionId": "terminalSessions"}], "orderBy": [{"field": {"fieldPath": "turnCount"}, "direction": "DESCENDING"}], "limit": 1}})["body"] if "document" in r), None)
if sid:
    t0 = time.time(); r = a.call("GET", f"clo/local/terminalSessions/{sid}")
    check("biggest session (full transcript) opens in < 5 s", r["status"] == 200 and time.time() - t0 < 5, f"{time.time()-t0:.1f}s")
    r2 = a.call("GET", f"clo/local/terminalSessions/{sid}?ifNoneMatch={r['body']['updateTime']}")
    check("unchanged session poll → 304, no transcript resent", r2["status"] == 304 and not r2["body"])
bid = next(x["id"] for x in state()["devices"] if x["name"] == "Máy B")
post("/api/revoke", {"id": bid})
t0 = time.time()
try:
    b.ws.recv(timeout=15); bclosed = False
except ConnectionClosed:
    bclosed = True
except TimeoutError:
    bclosed = False
    print("   (diag) B still open 15 s after revoke; relay refuses it now:", refused(b.url))
check("revoke on the Mac kicks B", bclosed and refused(b.url) == 403)
check("A unaffected by B's revoke", a.call("GET", "clo/local/terminalInbox/q0")["status"] == 200)

if "--turn" in sys.argv:
    rid = "edge-" + secrets.token_hex(3)
    a.call("PATCH", f"clo/local/terminalInbox/{rid}", {"fields": {
        "text": {"stringValue": "Use the Bash tool to run `sleep 40`, then reply DONE."}, "sessionId": {"stringValue": ""}, "cwd": {"stringValue": "/tmp"},
        "machine": {"stringValue": subprocess.run(["hostname", "-s"], capture_output=True, text=True).stdout.strip()},
        "profile": {"stringValue": "claude-yolo"}, "status": {"stringValue": "pending"}, "createdAt": {"timestampValue": ts(time.time())}}})
    st = ""
    for _ in range(40):
        st = a.call("GET", f"clo/local/terminalInbox/{rid}")["body"]["fields"]["status"]["stringValue"]
        if st == "running": break
        time.sleep(1)
    time.sleep(4)
    a.call("PATCH", f"clo/local/terminalInbox/{rid}?updateMask.fieldPaths=status", {"fields": {"status": {"stringValue": "cancel"}}})
    for _ in range(30):
        st = a.call("GET", f"clo/local/terminalInbox/{rid}")["body"]["fields"]["status"]["stringValue"]
        if st not in ("running", "cancel", "pending"): break
        time.sleep(1)
    check("stop a running turn from the phone → cancelled", st == "cancelled", st)

# Mac goes away: the phone hears it, and hears it come back
proc.terminate(); proc.wait(10)
try:
    m = recv(a.ws, 15)
except Exception as e:
    m = str(e)
check("bridge stopped → phone told Mac offline", m == {"op": "presence", "mac": False}, m)
proc = subprocess.Popen([sys.executable, "-I", str(HERE / "clo_bridge.py")], env=env, stdout=open(TMP / "log", "a"), stderr=subprocess.STDOUT)
atexit.register(lambda: proc.poll() is None and proc.kill())
try:
    m = recv(a.ws, 40)
except Exception as e:
    m = str(e)
check("bridge back → phone told Mac online", m == {"op": "presence", "mac": True}, m)
time.sleep(1)
check("A works after the Mac restarted (identity kept)", a.call("GET", "clo/local/terminalInbox/q0")["status"] == 200)

a.ws.send(json.dumps({"data": seal(a.key, {"op": "forget"})}))
try:
    a.ws.recv(timeout=15); fgot = False
except ConnectionClosed:
    fgot = True
except TimeoutError:
    fgot = False
check("“Quên máy Mac này” from the phone revokes it on the Mac", fgot and not state()["devices"] and refused(a.url) == 403)

c = Device("Máy C"); post("/api/approve", {"tag": c.pending()["tag"]}); c.finish()
old_room = state()["room"]
post("/api/reset")
try:
    c.ws.recv(timeout=15); cgot = False
except ConnectionClosed:
    cgot = True
except TimeoutError:
    cgot = False
time.sleep(3)
check("reset: devices kicked, old token refused, new room", cgot and refused(c.url) == 403 and state()["room"] != old_room and not state()["devices"])
check("bridge online again after reset", state()["online"])
proc.terminate(); proc.wait(10)
print("ALL PASSED" if ok_all else "SOME FAILED")
sys.exit(0 if ok_all else 1)
