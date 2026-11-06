"""Notification text, end to end: the turn summary (Claude's `⤷` line, else the first paragraph), the device's key
stamped on its push registration, and a real turn that pushes `+summary` through the relay. The fake token is refused
by Apple, which also checks dead-token cleanup.   ~/.clo2/venv/bin/python -I bridge/tests/push.py"""
import ast, base64, json, pathlib, re, secrets, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import Bridge, check, done, HERE
import tunnel
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# push_summary / _plain, taken from clo_bridge.py without running the bridge
src = (HERE / "clo_bridge.py").read_text()
ns = {"re": re}
for node in ast.parse(src).body:
    if isinstance(node, ast.FunctionDef) and node.name in ("_plain", "push_summary"):
        exec(compile(ast.Module([node], []), "clo_bridge.py", "exec"), ns)
summary = ns["push_summary"]

check("the ⤷ line wins", summary("Did a lot.\n\n```\ncode\n```\n\n⤷ Xong: thêm test, **11/11** qua. Cần anh: duyệt PR.")
      == "Xong: thêm test, 11/11 qua. Cần anh: duyệt PR.")
check("…the last one, markdown stripped", summary("⤷ old\ntext\n⤷ `new` [link](https://x)") == "new link")
check("no ⤷: first prose paragraph, tools/code/tables skipped",
      summary("🔧 Read: a.py\n```\nx = 1\n```\n| a | b |\nDone. **POST /login** now allows 10 attempts.\nSecond line.\n\nMore.")
      == "Done. POST /login now allows 10 attempts. Second line.")
long = summary("⤷ " + "word " * 100)
check("long text cut at a word, ≤ 220", len(long) <= 220 and long.endswith("…") and "  " not in long)
check("empty reply → empty", summary("") == "" and summary("🔧 Bash: ls") == "")

# the key and payload format the phone opens (Swift side: Shared/PushCrypto.swift, interop checked with swiftc)
k = secrets.token_bytes(32)
raw = base64.b64decode(tunnel.seal_push(k, {"t": "T", "b": "B"}))
check("seal_push opens with HKDF(pairing key, 'clo2-push')",
      json.loads(AESGCM(tunnel.push_key(k)).decrypt(raw[:12], raw[12:], None)) == {"t": "T", "b": "B"})

b = Bridge(7797, "push")
a = b.pair("Push phone")
token = secrets.token_hex(32)
r = a.call("PATCH", f"clo/local/devices/{token}", {"fields": {"token": {"stringValue": token}, "env": {"stringValue": "sandbox"}}})
f = a.doc(f"clo/local/devices/{token}") or {}
check("push registration carries the phone's key", r["status"] == 200 and f.get("pub", {}).get("stringValue") == a.pub, f)
r = a.call("PATCH", f"clo/local/devices/{token}", {"fields": {"token": {"stringValue": token}, "pub": {"stringValue": "forged"}}})
check("…a phone can't claim another's key", (a.doc(f"clo/local/devices/{token}") or {}).get("pub", {}).get("stringValue") == a.pub)

token2 = secrets.token_hex(32)   # the same phone, a new token (app update): the old one must go, or every turn rings twice
r = a.call("PATCH", f"clo/local/devices/{token2}", {"fields": {"token": {"stringValue": token2}, "env": {"stringValue": "sandbox"}}})
check("a phone's new token replaces its old one", r["status"] == 200 and a.doc(f"clo/local/devices/{token}") is None
      and a.doc(f"clo/local/devices/{token2}") is not None)
c = b.pair("Other phone")
token3 = secrets.token_hex(32)
c.call("PATCH", f"clo/local/devices/{token3}", {"fields": {"token": {"stringValue": token3}, "env": {"stringValue": "sandbox"}}})
check("…another phone's token is left alone", a.doc(f"clo/local/devices/{token2}") is not None and a.doc(f"clo/local/devices/{token3}") is not None)
c.call("DELETE", f"clo/local/devices/{token3}")
token = token2

rid = secrets.token_hex(8)
a.queue(rid, "Reply with exactly one word: ok", profile="claude-yolo")
st, _ = a.wait(rid)
for _ in range(20):
    if "push done" in b.log() or "push input" in b.log():
        break
    time.sleep(0.5)
line = next((l for l in b.log().splitlines() if re.search(r"push (done|input) " + token[:8], l)), "")
check("a finished turn pushes with an encrypted summary", st == "done" and "+summary" in line, line or b.log()[-1500:])
for _ in range(30):
    if "dropped a dead device token" in b.log():
        break
    time.sleep(0.5)
check("Apple refuses the fake token → the bridge forgets it", "dropped a dead device token" in b.log()
      and a.doc(f"clo/local/devices/{token}") is None)
done()
