"""CLO 2 local control page: http://127.0.0.1:7788 — pair a phone (QR), approve it, see / revoke devices.
Only this Mac can open it (bound to 127.0.0.1). POSTs need the X-CLO header, which a web page elsewhere can't send
here without a CORS preflight this server never answers — so other sites can't approve devices behind your back."""
import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE = """<!doctype html><html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Vibeclo on this Mac</title>
<script src="https://cdn.jsdelivr.net/npm/qrcode-generator@1.4.4/qrcode.min.js"></script>
<style>
:root{color-scheme:light dark;--bg:#f5f4ef;--card:#fff;--text:#1f1e1d;--muted:#73726c;--accent:#c96442;--line:#e6e4dc}
@media(prefers-color-scheme:dark){:root{--bg:#1f1e1d;--card:#2a2927;--text:#f0eee6;--muted:#a3a198;--line:#3a3936}}
body{margin:0;background:var(--bg);color:var(--text);font:15px -apple-system,system-ui,sans-serif}
main{max-width:560px;margin:0 auto;padding:28px 18px}
h1{font-size:22px;margin:0 0 4px}.muted{color:var(--muted)}.small{font-size:13px}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px;margin:16px 0}
button{font:inherit;border:0;border-radius:10px;padding:9px 16px;background:var(--accent);color:#fff;cursor:pointer}
button.ghost{background:transparent;color:var(--accent);border:1px solid var(--line)}
#qr{display:flex;justify-content:center;margin:12px 0}#qr img{width:240px;height:240px;image-rendering:pixelated;background:#fff;padding:10px;border-radius:12px}
.row{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:10px 0;border-top:1px solid var(--line)}
.row:first-child{border-top:0}.code{font:600 28px ui-monospace,monospace;letter-spacing:6px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
</style></head><body><main>
<h1>Vibeclo</h1><div class="muted" id="status">…</div>
<div class="card"><b>Pair a phone or browser</b>
<p class="muted small">Open the Vibeclo app → Scan code. The code works once and expires after 5 minutes. Data is end-to-end encrypted; the relay server cannot read it and stores nothing.</p>
<div id="pair"></div></div>
<div class="card" id="pendingCard" hidden><b>Pairing requests</b><p class="muted small">Only allow if the 4-digit code below matches the one on the phone.</p><div id="pending"></div></div>
<div class="card"><b>Paired devices</b><div id="devices"></div></div>
<div class="card"><b>Reset pairing</b><p class="muted small">Creates a new key for this Mac. All paired devices lose access and must scan again.</p>
<button class="ghost" onclick="if(confirm('Disconnect all paired devices?'))post('/api/reset')">Reset</button></div>
</main><script>
const $=(id)=>document.getElementById(id);
const WEB=%WEB%;
const post=(p,b)=>fetch(p,{method:"POST",headers:{"X-CLO":"1","Content-Type":"application/json"},body:JSON.stringify(b||{})}).then(load);
const esc=(s)=>String(s).replace(/[&<>"]/g,(c)=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
let lastQr="";
async function load(){
  const s=await (await fetch("/api/state")).json();
  $("status").innerHTML=`<span class="dot" style="background:${s.online?"#34c759":"#ff3b30"}"></span>${esc(s.label)} · ${s.online?"connected":"not connected to the relay"}`;
  if(s.pairing){
    if(s.pairing!==lastQr){lastQr=s.pairing;const q=qrcode(0,"M");q.addData(s.pairing);q.make();
      const d=s.pairing.split("d=")[1];
      $("pair").innerHTML=`<div id="qr">${q.createImgTag(6,0)}</div><div class="muted small" style="text-align:center">Expires at ${new Date(s.pairingExp*1000).toLocaleTimeString()}</div>
        <ol class="small" style="line-height:1.6;padding-left:20px"><li><b>iPhone:</b> open the Vibeclo app → <b>Scan QR code</b> (or point the Camera app at the code → “Open in Vibeclo”)</li>
        <li><b>Can’t scan?</b> click <b>“Copy pairing link”</b>, send it to your iPhone, then in Vibeclo: <b>Add Mac</b> ▸ <b>Or paste a pairing link</b></li>
        <li>A device asking to pair appears here with a 4-digit code — if it matches the code on the device, click <b>Allow</b></li></ol>
        <p style="text-align:center;display:flex;gap:8px;justify-content:center;flex-wrap:wrap"><button class="ghost" id="copylink">Copy pairing link</button> <button class="ghost" onclick="post('/api/pair/stop')">Cancel</button></p>`;
      $("copylink").onclick=()=>{navigator.clipboard.writeText(s.pairingShare||s.pairing);$("copylink").textContent="Copied";};}
  } else { lastQr=""; $("pair").innerHTML=`<button onclick="post('/api/pair/start')">Show QR code</button>`; }
  $("pendingCard").hidden=!s.pending.length;
  $("pending").innerHTML=s.pending.map(p=>`<div class="row"><div><div>${esc(p.name)}</div><div class="code">${p.check}</div></div><div><button onclick="post('/api/approve',{tag:'${p.tag}'})">Allow</button> <button class="ghost" onclick="post('/api/deny',{tag:'${p.tag}'})">Deny</button></div></div>`).join("");
  $("devices").innerHTML=s.devices.length?s.devices.map(d=>`<div class="row"><div><div>${esc(d.name)}</div><div class="muted small">Paired ${new Date(d.addedAt*1000).toLocaleDateString()}${d.lastSeen?" · last used "+new Date(d.lastSeen*1000).toLocaleString():""}</div></div><button class="ghost" onclick="if(confirm('Revoke this device's access?'))post('/api/revoke',{id:'${d.id}'})">Revoke</button></div>`).join(""):`<p class="muted small">No devices yet.</p>`;
}
load();setInterval(load,1500);
</script></body></html>"""


LEGACY_WEB = ("https://clo2.synthvale.com", "https://clo.sakura5.app")   # owner's domain + the old public one; still served, still allowed


def serve(tunnel, port=7788, log=print, web="https://clo2.sakura5.app"):
    allowed = {web.rstrip("/"), *LEGACY_WEB}
    page = PAGE.replace("%WEB%", json.dumps(web.rstrip("/")))

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _cors(self):
            """The web app (https://clo2.sakura5.app, clo2.synthvale.com or the old clo.sakura5.app) opened in a browser on this Mac may ask for the local pairing
            link; Chrome's Private Network Access sends a preflight first. No other site gets an answer it can read."""
            origin = self.headers.get("Origin", "")
            if origin in allowed:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Private-Network", "true")
                self.send_header("Vary", "Origin")
                return True
            return False

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.send_header("Access-Control-Allow-Methods", "GET")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            if self.headers.get("Host", "").split(":")[0] not in ("127.0.0.1", "localhost"):   # DNS rebinding
                return self._json({"error": "forbidden"}, 403)
            if self.path == "/api/localpair":
                # Same Mac: a pairing link the Mac approves at once (it carries `a`, like the QR on this screen).
                # Only readable by this page and the web app's own origin (CORS); a browser here is this Mac's user.
                origin = self.headers.get("Origin", "")
                if origin and origin not in allowed:
                    return self._json({"error": "forbidden"}, 403)
                if not (tunnel.pairing and tunnel.pairing["exp"] > time.time() + 30):
                    tunnel.start_pairing()
                data = json.dumps({"link": tunnel.qr(), "name": tunnel.label}).encode()
                self.send_response(200)
                self._cors()
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                return self.wfile.write(data)
            if self.path.split("?")[0] == "/web":
                # Same Mac, browsers that block https → http://127.0.0.1 fetches (Safari): the web app's "Connect this Mac"
                # button navigates here and we send it back with the auto-approved link in the fragment (never sent to
                # a server). Only to the web app's own origins; a site that sends someone here just gets them paired
                # with their own Mac on our page — it can't read the link.
                q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                to = (q.get("to") or [web])[0].rstrip("/")
                if to not in allowed:
                    return self._json({"error": "forbidden"}, 403)
                if not (tunnel.pairing and tunnel.pairing["exp"] > time.time() + 30):
                    tunnel.start_pairing()
                self.send_response(302)
                self.send_header("Location", to + "/#pair=" + tunnel.qr().split("d=", 1)[1])
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path == "/api/state":
                return self._json(dict(tunnel.state(), web=web.rstrip("/")))
            if self.path in ("/", "/index.html"):
                data = page.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                return self.wfile.write(data)
            self._json({"error": "not found"}, 404)

        def do_POST(self):
            if self.headers.get("X-CLO") != "1" or self.headers.get("Host", "").split(":")[0] not in ("127.0.0.1", "localhost"):
                return self._json({"error": "forbidden"}, 403)
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            p = self.path
            if p == "/api/pair/start":
                tunnel.start_pairing()
            elif p == "/api/pair/stop":
                tunnel.stop_pairing()
            elif p == "/api/approve":
                tunnel.approve(body.get("tag", ""))
            elif p == "/api/deny":
                tunnel.deny(body.get("tag", ""))
            elif p == "/api/revoke":
                tunnel.revoke(body.get("id", ""))
            elif p == "/api/settings":
                if not tunnel.set_setting(body.get("key", ""), body.get("value", "")):
                    return self._json({"error": "bad setting"}, 400)
            elif p == "/api/pairvia":
                if not tunnel.set_pair_via(body.get("mode", "")):
                    return self._json({"error": "bad mode"}, 400)
            elif p == "/api/reset":
                tunnel.reset()
            else:
                return self._json({"error": "not found"}, 404)
            self._json({"ok": True})

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f"control page: http://127.0.0.1:{port}")
    return srv
