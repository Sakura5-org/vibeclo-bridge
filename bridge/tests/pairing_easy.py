"""CLO 2 easier pairing: QR from the Mac's screen pairs with no "Cho phép"; the copy-to-send link still asks; Wi-Fi
pairing (found by Bonjour) shows the check on both sides and hands over the link after "Cho phép"; a request resent
through the relay gets the same answer without running twice. Throwaway bridge + the real relay."""
import json, os, secrets, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Bridge, check, done, recv, b64u, unb64u, seal, open_frame
from websockets.sync.client import connect
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from tunnel import check_code

b = Bridge(7796, "easy", extra_env={"CLO2_LAN_GRACE": "4"})


def keypair(mac_pub):
    priv = X25519PrivateKey.generate()
    pub = b64u(priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"clo2-v1", info=unb64u(mac_pub) + unb64u(pub)).derive(
        priv.exchange(X25519PublicKey.from_public_bytes(unb64u(mac_pub))))
    return pub, key


def hello(link, name, a=None, keys=None):
    p = json.loads(unb64u(link.split("d=", 1)[1]))
    pub, key = keys or keypair(p["k"])
    ws = connect(f"wss://{p['r']}/room/{p['room']}?role=dev&token={p['c']}", max_size=None)
    recv(ws)
    body = {"op": "hello", "name": name}
    if a:
        body["a"] = a
    ws.send(json.dumps({"pub": pub, "data": seal(key, body)}))
    return ws, key, p


def answer(ws, key, t=15):
    try:
        return open_frame(key, recv(ws, t)["data"])
    except Exception as e:
        return {"error": repr(e)}


def pending(name, n=20):
    for _ in range(n):
        x = [p for p in b.state()["pending"] if p["name"] == name]
        if x:
            return x[0]
        time.sleep(0.3)


# ---- QR shown on the Mac: carries `a` → paired straight away
b.post("/api/pair/start"); time.sleep(1)
st = b.state()
full = json.loads(unb64u(st["pairing"].split("d=", 1)[1]))
share = json.loads(unb64u(st["pairingShare"].split("d=", 1)[1]))
check("QR link carries the auto-approve secret", isinstance(full.get("a"), str) and len(full["a"]) > 16)
check("copy-to-send link leaves it out", "a" not in share and share["c"] == full["c"])
ws, key, _ = hello(st["pairing"], "QR phone", a=full["a"])
r = answer(ws, key); ws.close()
check("QR from this screen → paired with no Cho phép", r.get("op") == "paired" and r.get("token"), r)
check("device listed", any(d["name"] == "QR phone" for d in b.state()["devices"]))

# ---- where pairing may come from (menu bar: "Cho phép ghép qua"); default Tailscale or Wi-Fi, no sent links
check("default: Tailscale or Wi-Fi", b.state().get("pairVia") == "both", b.state().get("pairVia"))
b.post("/api/pair/start"); time.sleep(1)
st = b.state()
ws2, key2, _ = hello(st["pairingShare"], "Link-only device")
r = answer(ws2, key2); ws2.close()
check("sent link refused by default, phone told why", r.get("op") == "denied" and "only accepts pairing" in r.get("why", ""), r)
check("…and nothing waits on the Mac", pending("Link-only device", n=2) is None)
check("unknown mode rejected", b.post("/api/pairvia", {"mode": "x"}) == 400)
b.post("/api/pairvia", {"mode": "any"})
check("mode saved", b.state().get("pairVia") == "any")

# ---- shared link (no a) and a wrong a: wait for the user (one device at a time per pairing session)
b.post("/api/pair/start"); time.sleep(1)
st = b.state()
ws2, key2, _ = hello(st["pairingShare"], "Wrong-a device", a="x" * 24)
check("wrong auto secret → waits for Cho phép", pending("Wrong-a device") is not None)
b.post("/api/deny", {"tag": pending("Wrong-a device")["tag"]})
check("deny still works", answer(ws2, key2).get("op") == "denied")
ws2.close()
ws, key, _ = hello(st["pairingShare"], "Shared-link laptop")
check("shared link → waits for Cho phép (check code shown)", (pending("Shared-link laptop") or {}).get("check"))
b.post("/api/approve", {"tag": pending("Shared-link laptop")["tag"]})
check("approve still works", answer(ws, key).get("op") == "paired")
ws.close()

# ---- Wi-Fi pairing (Bonjour → direct port)
lan = b.state().get("lanPort") or 7896


def wifi_ask(name, keys=None):
    w = connect(f"ws://127.0.0.1:{lan}", open_timeout=5)
    ch = json.loads(w.recv(timeout=5))
    mac_pub = ch.get("k")
    pub, key = keys or keypair(mac_pub)
    w.send(json.dumps({"op": "pairreq", "pub": pub, "name": name}))
    return w, ch, pub, key


b.post("/api/pairvia", {"mode": "tailnet"})
w, ch, pub, key = wifi_ask("Wi-Fi phone")
m = json.loads(w.recv(timeout=5)); w.close()
check("Tailscale only → a Wi-Fi request is refused with the reason", m.get("op") == "denied" and m.get("why"), m)
b.post("/api/pairvia", {"mode": "wifi"})
w, ch, pub, key = wifi_ask("Wi-Fi phone")
check("challenge tells the Mac's key + name (for the check code)", ch.get("k") and ch.get("n") == "Test Mac", ch)
wait = json.loads(w.recv(timeout=5))
check("phone told to wait, with the same 4 digits as the Mac",
      wait.get("op") == "waiting" and wait.get("check") == check_code(ch["k"], pub), wait)
p = pending("Wi-Fi phone")
check("Wi-Fi request shows on the Mac (menu / page)", p and p["tag"].startswith("lan:") and p["check"] == wait["check"], p)
check("…saying how it came (Wi-Fi + address)", "Wi-Fi" in p.get("via", ""), p)
b.post("/api/approve", {"tag": p["tag"]})
m = json.loads(w.recv(timeout=10))
link = open_frame(key, m["data"])["link"] if m.get("op") == "pairlink" else ""
w.close()
lp = json.loads(unb64u(link.split("d=", 1)[1])) if link else {}
check("after Cho phép: pairing link sealed to the phone's key, with auto secret", lp.get("a") and lp.get("k") == ch["k"], m)
ws, key3, _ = hello(link, "Wi-Fi phone", a=lp.get("a"), keys=(pub, key))
r = answer(ws, key3); ws.close()
check("phone pairs with that link — no second Cho phép", r.get("op") == "paired", r)
wifi_keys = (pub, key)

# the same phone asks again (app reinstalled; its key survives in the keychain): it holds a trusted key → no Cho phép
w, ch, pub, key = wifi_ask("Wi-Fi phone", keys=wifi_keys)
wait = json.loads(w.recv(timeout=5))
m = wait if wait.get("op") == "pairlink" else json.loads(w.recv(timeout=5))
link = open_frame(key, m["data"])["link"] if m.get("op") == "pairlink" else ""
w.close()
check("already-paired device → pairing link at once", link and pending("Wi-Fi phone", n=1) is None, m)
lp = json.loads(unb64u(link.split("d=", 1)[1])) if link else {}
ws, key3, _ = hello(link, "Wi-Fi phone", a=lp.get("a"), keys=wifi_keys)
r = answer(ws, key3); ws.close()
check("…re-paired, and listed once (old token replaced)", r.get("op") == "paired" and
      sum(d["name"] == "Wi-Fi phone" for d in b.state()["devices"]) == 1, b.state()["devices"])

w, ch, pub, key = wifi_ask("Denied phone")
w.recv(timeout=5)
b.post("/api/deny", {"tag": pending("Denied phone")["tag"]})
check("Wi-Fi request denied → phone told", json.loads(w.recv(timeout=10)).get("op") == "denied")
w.close()

w, ch, pub, key = wifi_ask("Gone phone"); w.recv(timeout=5); w.close()
time.sleep(1)
check("phone's connection dropped → its request stays a moment (it may come back)", pending("Gone phone", n=1) is not None)
time.sleep(6)
check("phone that gave up → request disappears from the Mac", pending("Gone phone", n=3) is None)

# connection drops while the owner types the code (phone screen locked): it comes back with the same key
w, ch, pub, key = wifi_ask("Locked phone"); w.recv(timeout=5)
tag1 = pending("Locked phone")["tag"]
w.close(); time.sleep(1)
w, ch, _, _ = wifi_ask("Locked phone", keys=(pub, key))
wait = json.loads(w.recv(timeout=5))
p2 = [x for x in b.state()["pending"] if x["name"] == "Locked phone"]
check("reconnect with the same key → same request (same dialog, same code), not a second one",
      len(p2) == 1 and p2[0]["tag"] == tag1 and wait.get("check") == check_code(ch["k"], pub), (p2, wait))
b.post("/api/approve", {"tag": tag1})
m = json.loads(w.recv(timeout=10)); w.close()
check("…approved → the link goes to the new connection", m.get("op") == "pairlink" and open_frame(key, m["data"]).get("link"), m)

# approved while the phone was away → when it comes back the link is waiting
w, ch, pub, key = wifi_ask("Away phone"); w.recv(timeout=5)
tag = pending("Away phone")["tag"]
w.close(); time.sleep(0.5)
b.post("/api/approve", {"tag": tag})
w, ch, _, _ = wifi_ask("Away phone", keys=(pub, key))
m = json.loads(w.recv(timeout=5))
m = m if m.get("op") == "pairlink" else json.loads(w.recv(timeout=5))
w.close()
check("approved while disconnected → link sent when it reconnects", m.get("op") == "pairlink", m)

# the app asks again with a NEW key (new code): the old request (and its dialog) goes away
w1, _, _, _ = wifi_ask("Retry phone"); w1.recv(timeout=5)
w2, _, _, _ = wifi_ask("Retry phone"); w2.recv(timeout=5)
time.sleep(1)
check("new key from the same address → only the newest request stays", len([x for x in b.state()["pending"] if x["name"] == "Retry phone"]) == 1)
w1.close(); w2.close()

# ---- relay resend: same id → same answer, run once
d = b.pair("Resend phone")
rid = "dup" + secrets.token_hex(3)
r1 = d.call("GET", "clo/local/terminalSessions?pageSize=3", rid=rid)
r2 = d.call("GET", "clo/local/terminalSessions?pageSize=3", rid=rid)
check("resent request answered", r1["status"] == r2["status"] == 200, (r1.get("status"), r2.get("status")))
check("…with the same reply (not run twice)", r1["body"] == r2["body"], (r1["body"], r2["body"]))
done()
