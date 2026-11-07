"""Artifacts calling APIs with a vault secret (tunnel op secret.fetch): the Mac adds the key, the page never sees it.
Local echo server (http allowed for 127.0.0.1 only with CLO2_TEST_FETCH_HTTP=1).   ~/.clo2/venv/bin/python -I bridge/tests/secret_fetch.py"""
import base64, http.server, json, pathlib, sys, threading
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import Bridge, check, done

seen = []


class Echo(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _go(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode() if n else ""
        seen.append({"path": self.path, "auth": self.headers.get("Authorization"), "key": self.headers.get("X-Api-Key"), "body": body})
        if self.path.startswith("/redirect"):
            self.send_response(302); self.send_header("Location", "http://127.0.0.1:1/steal"); self.end_headers(); return
        out = json.dumps({"ok": True, "echo": seen[-1]}).encode()
        self.send_response(201 if self.command == "POST" else 200)
        self.send_header("Content-Type", "application/json"); self.send_header("X-Echo-Auth", self.headers.get("Authorization") or "")
        self.end_headers(); self.wfile.write(out)
    do_GET = do_POST = _go


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Echo)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"

b = Bridge(7799, "secretfetch", extra_env={"CLO2_TEST_FETCH_HTTP": "1"})
a = b.pair("Fetch phone")
KEY = "sk-test-" + "x7" * 12
check("secret saved", a.op("secret.set", name="API_KEY", value=KEY)["status"] == 200)


def fetch(**kw):
    r = a.op("secret.fetch", **kw)
    body = r.get("body") or {}
    raw = base64.b64decode(body.get("body") or "") if "body" in body else b""
    return r["status"], body, raw


st, body, raw = fetch(url=BASE + "/v1/me", secret="API_KEY")
check("secret: NAME → Authorization: Bearer, added on the Mac", st == 200 and body["status"] == 200 and seen[-1]["auth"] == "Bearer " + KEY)
check("…the answer never carries the key (body, headers)", KEY.encode() not in raw and b"[secret]" in raw
      and body["headers"].get("X-Echo-Auth") == "Bearer [secret]", raw[:200])

st, body, raw = fetch(url=BASE + "/q?key={{secret:API_KEY}}", method="POST", headers={"X-Api-Key": "{{secret:API_KEY}}", "Content-Type": "application/json"},
                      body=base64.b64encode(b'{"token":"{{secret:API_KEY}}"}').decode())
e = seen[-1]
check("{{secret:NAME}} in URL, header and body", body.get("status") == 201 and e["path"] == "/q?key=" + KEY and e["key"] == KEY
      and json.loads(e["body"])["token"] == KEY and e["auth"] is None, e)

st, body, raw = fetch(url=BASE + "/redirect", secret="API_KEY")
check("redirects aren't followed (the key stays with the host the user approved)", body.get("status") == 302 and not any(s["path"] == "/steal" for s in seen))

check("unknown secret → 404", fetch(url=BASE + "/x", secret="NOPE")[0] == 404)
check("no secret named → refused", fetch(url=BASE + "/x")[0] == 400)
check("http to anything but the test server → refused", fetch(url="http://example.com/", secret="API_KEY")[0] == 400)
check("https to localhost / LAN → refused", fetch(url="https://127.0.0.1/", secret="API_KEY")[0] == 400
      and fetch(url="https://192.168.1.1/", secret="API_KEY")[0] == 400 and fetch(url="https://localhost/", secret="API_KEY")[0] == 400)
check("bad method refused", fetch(url=BASE + "/x", secret="API_KEY", method="TRACE")[0] == 400)
st, body, raw = fetch(url="https://postman-echo.com/headers", secret="API_KEY")
if st == 200 and body.get("status") == 200:
    check("live https API: key sent, answer scrubbed", b"Bearer [secret]" in raw and KEY.encode() not in raw, raw[:300])
else:
    print("SKIP live https check:", st, body.get("error") or body.get("status"))
check("the log has names, never the value", "secret fetch API_KEY" in b.log() and KEY not in b.log())
done()
