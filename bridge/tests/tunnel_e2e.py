"""End-to-end check of pairing + an encrypted request through the real relay, against a running bridge.
  ~/.clo2/venv/bin/python -I bridge/tests/tunnel_e2e.py [port]"""
import base64, json, pathlib, sys, time, urllib.request
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from websockets.sync.client import connect
from tunnel import b64u, unb64u, seal, open_frame, check_code

port = sys.argv[1] if len(sys.argv) > 1 else "7788"
page = f"http://127.0.0.1:{port}"
post = lambda p, b=None: urllib.request.urlopen(urllib.request.Request(page + p, data=json.dumps(b or {}).encode(), headers={"X-CLO": "1", "Content-Type": "application/json"}))
state = lambda: json.load(urllib.request.urlopen(page + "/api/state"))

post("/api/pair/start")
qr = state()["pairing"]
p = json.loads(unb64u(qr.split("d=", 1)[1]))
dev = X25519PrivateKey.generate()
dpub = b64u(dev.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
key = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"clo2-v1", info=unb64u(p["k"]) + unb64u(dpub)).derive(
    dev.exchange(X25519PublicKey.from_public_bytes(unb64u(p["k"]))))
with connect(f"wss://{p['r']}/room/{p['room']}?role=dev&token={p['c']}") as ws:
    print("presence", ws.recv())
    ws.send(json.dumps({"pub": dpub, "data": seal(key, {"op": "hello", "name": "Test phone"})}))
    for _ in range(20):
        pend = state()["pending"]
        if pend: break
        time.sleep(0.3)
    assert pend and pend[0]["check"] == check_code(p["k"], dpub), pend
    post("/api/approve", {"tag": pend[0]["tag"]})
    reply = open_frame(key, json.loads(ws.recv())["data"])
    print("paired:", reply["op"], reply["name"])
token = reply["token"]
with connect(f"wss://{p['r']}/room/{p['room']}?role=dev&token={token}", max_size=None) as ws:
    print("presence", ws.recv())
    t = time.time()
    ws.send(json.dumps({"data": seal(key, {"id": "1", "method": "GET", "url": "clo/local/terminalSessions?pageSize=300&mask.fieldPaths=title"})}))
    r = open_frame(key, json.loads(ws.recv())["data"])
    print("sessions:", r["status"], len(r["body"].get("documents", [])), f"{(time.time()-t)*1000:.0f} ms")
    if "--turn" in sys.argv:   # a real Claude turn: queue → run → reply
        rid = "e2e-" + str(int(time.time()))
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        f = {"text": {"stringValue": "Reply with exactly: OK"}, "sessionId": {"stringValue": ""}, "cwd": {"stringValue": "/tmp"},
             "machine": {"stringValue": state()["label"].split(".")[0]}, "profile": {"stringValue": "claude"}, "status": {"stringValue": "pending"},
             "createdAt": {"timestampValue": now}}
        call = lambda m, u, b=None: (ws.send(json.dumps({"data": seal(key, {"id": u, "method": m, "url": u, "body": b})})), open_frame(key, json.loads(ws.recv())["data"]))[1]
        call("PATCH", f"clo/local/terminalInbox/{rid}", {"fields": f})
        for _ in range(90):
            d = call("GET", f"clo/local/terminalInbox/{rid}")["body"].get("fields", {})
            st = d.get("status", {}).get("stringValue")
            if st in ("done", "error", "cancelled"): break
            time.sleep(2)
        print("turn:", st, repr(d.get("reply", {}).get("stringValue", "")[:80]), d.get("error", {}).get("stringValue", ""))
devs = state()["devices"]
post("/api/revoke", {"id": devs[-1]["id"]})
try:
    with connect(f"wss://{p['r']}/room/{p['room']}?role=dev&token={token}") as ws:
        print("UNEXPECTED: still connects after revoke")
except Exception as e:
    print("revoked ok:", str(e)[:60])
