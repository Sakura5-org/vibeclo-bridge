"""Shared test harness: a throwaway CLO 2 bridge (own identity/store/port) + a fake phone talking through the real relay."""
import atexit, base64, json, os, pathlib, secrets, shutil, signal, subprocess, sys, time, urllib.request
HERE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from websockets.sync.client import connect
from websockets.exceptions import InvalidStatus, ConnectionClosed
from tunnel import b64u, unb64u, seal, open_frame, check_code, token_hash

RELAY = "clo2-relay.synthvale.com"
ok_all = True


def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""), flush=True)


def done():
    print("ALL PASSED" if ok_all else "SOME FAILED")
    sys.exit(0 if ok_all else 1)


def refused(url):
    try:
        with connect(url, open_timeout=10):
            return None
    except InvalidStatus as e:
        return e.response.status_code


def recv(ws, t=10):
    return json.loads(ws.recv(timeout=t))


def ts(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))


def cleanup_sessions(store):
    """Claude Code sessions this test bridge started in /tmp (its inbox/branch docs name them) → deleted, so test turns
    don't pile up in the real session list (the installed bridge scans the same ~/.claude/projects)."""
    import re, sqlite3
    try:
        db = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
        rows = db.execute("SELECT fields FROM docs WHERE coll IN ('terminalInbox', 'terminalBranches')").fetchall()
        db.close()
    except Exception:
        return 0
    sids = set()
    for (fields,) in rows:
        sids |= set(re.findall(r'"(?:\w*[sS]essionId|sid)": \{"stringValue": "([0-9a-f-]{36})"', fields))
    proj = pathlib.Path.home() / ".claude/projects/-private-tmp"
    n = 0
    for sid in sids:
        f = proj / f"{sid}.jsonl"
        if f.exists():
            f.unlink(); n += 1
        shutil.rmtree(proj / sid, ignore_errors=True)
    return n


class Bridge:
    def __init__(self, port, name="edge", extra=None, extra_env=None):
        self.port, self.tmp = port, pathlib.Path(f"/tmp/clo2-{name}")
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.tmp.mkdir(parents=True)
        cfg = {"store": str(self.tmp / "store.db"), "port": port, "label": "Test Mac", "max_sessions": 5, "link_tools": False,
               "lan_port": port + 100, "ts_web": 0, "auto_update": False, **(extra or {})}
        self.env = {**os.environ, "CLO2_DIR": str(self.tmp / "id"), "CLO_CONFIG": json.dumps(cfg), "CLO2_NO_NOTIFY": "1", "CLO2_NO_BONJOUR": "1", **(extra_env or {})}
        self.page = f"http://127.0.0.1:{port}"
        self.start()
        atexit.register(self.stop)

    def start(self):
        self.proc = subprocess.Popen([sys.executable, "-I", str(HERE / "clo_bridge.py")], env=self.env,
                                     stdout=open(self.tmp / "log", "a"), stderr=subprocess.STDOUT)
        for _ in range(60):
            try:
                if self.state()["online"]: return
            except Exception: pass
            time.sleep(0.5)

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try: self.proc.wait(10)
            except subprocess.TimeoutExpired: self.proc.kill()
        cleanup_sessions(self.tmp / "store.db")

    def log(self):
        return (self.tmp / "log").read_text()

    def post(self, p, b=None, headers=None):
        h = {"X-CLO": "1", "Content-Type": "application/json"} if headers is None else headers
        try:
            return urllib.request.urlopen(urllib.request.Request(self.page + p, data=json.dumps(b or {}).encode(), headers=h), timeout=15).status
        except urllib.error.HTTPError as e:
            return e.code
        except TimeoutError:
            self.proc.send_signal(signal.SIGUSR1); time.sleep(1)
            print("   (diag) control page stuck on", p, "— bridge threads:\n" + self.log()[-6000:])
            raise

    def state(self):
        return json.load(urllib.request.urlopen(self.page + "/api/state", timeout=10))

    def pair(self, name):
        d = Device(self, name)
        d.approve()
        return d


class Device:
    def __init__(self, bridge, name, mac_key=None):
        self.b = bridge
        bridge.post("/api/settings", {"key": "pairVia", "value": "any"})   # test devices pair with the sent link (default refuses it)
        bridge.post("/api/pair/start"); time.sleep(1)   # the relay learns the code a moment later; a person scanning is slower anyway
        self.p = json.loads(unb64u(bridge.state()["pairing"].split("d=", 1)[1]))
        self.priv = X25519PrivateKey.generate()
        self.pub = b64u(self.priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
        mk = mac_key or self.p["k"]
        self.key = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"clo2-v1", info=unb64u(mk) + unb64u(self.pub)).derive(
            self.priv.exchange(X25519PublicKey.from_public_bytes(unb64u(mk))))
        self.name = name
        self.ws = connect(f"wss://{self.p['r']}/room/{self.p['room']}?role=dev&token={self.p['c']}", max_size=None, ping_timeout=90)
        recv(self.ws)
        self.ws.send(json.dumps({"pub": self.pub, "data": seal(self.key, {"op": "hello", "name": name})}))

    def pending(self):
        for _ in range(30):
            p = [x for x in self.b.state()["pending"] if x["name"] == self.name]
            if p: return p[0]
            time.sleep(0.3)

    def approve(self):
        self.b.post("/api/approve", {"tag": self.pending()["tag"]})
        return self.finish()

    def finish(self):
        reply = open_frame(self.key, recv(self.ws)["data"])
        self.ws.close()
        if reply.get("op") != "paired": return reply
        self.token = reply["token"]
        self.url = f"wss://{self.p['r']}/room/{self.p['room']}?role=dev&token={self.token}"
        self.ws = connect(self.url, max_size=None, ping_timeout=90)
        recv(self.ws)
        return reply

    def send(self, obj):
        self.ws.send(json.dumps({"data": seal(self.key, obj)}))

    def call(self, method, url, body=None, rid=None, t=30, **extra):
        rid = rid or secrets.token_hex(4)
        self.send({"id": rid, "method": method, "url": url, "body": body, **extra})
        while True:
            m = recv(self.ws, t)
            if "data" in m:
                r = open_frame(self.key, m["data"])
                if r.get("id") == rid: return r
            else:
                print("   (note) non-data message:", m)

    def op(self, op, t=60, **kw):
        """Tunnel op (upload / secret / artifact…) → its reply."""
        rid = secrets.token_hex(4)
        self.send({"id": rid, "op": op, **kw})
        while True:
            m = recv(self.ws, t)
            if "data" in m:
                r = open_frame(self.key, m["data"])
                if r.get("id") == rid: return r

    def doc(self, path):
        r = self.call("GET", path)
        return r["body"].get("fields", {}) if r["status"] == 200 else None

    def queue(self, rid, text, sid="", cwd="/tmp", profile="claude-yolo", **fields):
        f = {"text": text, "sessionId": sid, "cwd": cwd, "profile": profile, "status": "pending",
             "machine": subprocess.run(["hostname", "-s"], capture_output=True, text=True).stdout.strip(), **fields}
        enc = {}
        for k, v in f.items():
            if isinstance(v, bool): enc[k] = {"booleanValue": v}
            elif isinstance(v, int): enc[k] = {"integerValue": str(v)}
            elif isinstance(v, list): enc[k] = {"arrayValue": {"values": [{"stringValue": x} for x in v]}}
            else: enc[k] = {"stringValue": v}
        enc["createdAt"] = {"timestampValue": ts(time.time())}
        return self.call("PATCH", f"clo/local/terminalInbox/{rid}", {"fields": enc})

    def wait(self, rid, until=("done", "error", "cancelled"), t=180):
        st, f = "", {}
        for _ in range(int(t / 2)):
            f = self.doc(f"clo/local/terminalInbox/{rid}") or {}
            st = f.get("status", {}).get("stringValue", "")
            if st in until: break
            time.sleep(2)
        return st, f
