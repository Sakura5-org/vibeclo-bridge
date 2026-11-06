"""Release trial / automatic rollback (bridge/trial.py + updater.snapshot): a new signed release is on trial until it
runs healthy; one that keeps crashing gets the previous release back, its seq is skipped, and the next seq is taken.
Local web server + throwaway app dir, no LaunchAgents, no network."""
import base64, functools, hashlib, http.server, json, os, pathlib, shutil, sys, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import check, done
import trial, updater
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

BRIDGE = pathlib.Path(__file__).resolve().parents[1]
ROOT = pathlib.Path("/tmp/clo2-rollback"); shutil.rmtree(ROOT, ignore_errors=True)
SITE, APP = ROOT / "site", ROOT / "app"
(SITE / "bridge").mkdir(parents=True); APP.mkdir(parents=True)
key = Ed25519PrivateKey.generate()
PUB = base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
FILES = ["trial.py", "clo_bridge.py", "localstore.py", "updater.py"]


def publish(seq, marker):
    d = SITE / "bridge"
    for f in FILES:
        data = (BRIDGE / f).read_bytes() + (f"\n# release {marker}\n".encode() if f == "localstore.py" else b"")
        (d / f).write_bytes(data)
    m = json.dumps({"version": marker, "seq": seq, "pip": [],
                    "files": {f: hashlib.sha256((d / f).read_bytes()).hexdigest() for f in FILES}}).encode()
    (d / "MANIFEST.json").write_bytes(m)
    (d / "MANIFEST.sig").write_text(base64.b64encode(key.sign(m)).decode())


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a): pass
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(SITE)))
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"
upd = lambda: updater.check(APP, lambda *a: None, BASE, PUB)
marker = lambda: (APP / "localstore.py").read_text().rsplit("release ", 1)[1].strip()
quiet = lambda *a: None

publish(10, "v10")
check("first release installs", upd() and marker() == "v10")
trial.ok(APP)   # pretend v10 ran healthy
check("no trial file once healthy", not (APP / ".trial.json").exists())
check("normal start: no trial", trial.start(APP, quiet) is None)

publish(11, "v11")
check("new release installs", upd() and marker() == "v11")
check("…previous release kept in prev/", "release v10" in (APP / "prev/localstore.py").read_text()
      and json.loads((APP / "prev/MANIFEST.json").read_text())["seq"] == 10)
check("…and it is on trial", json.loads((APP / ".trial.json").read_text())["seq"] == 11)
check("starts 1–3 stay on trial", [trial.start(APP, quiet) for _ in range(3)] == ["trial"] * 3 and marker() == "v11")
check("4th start without getting healthy → rolled back", trial.start(APP, quiet) == "rolled-back" and marker() == "v10")
check("…installed manifest is the old one again", json.loads((APP / "MANIFEST.json").read_text())["seq"] == 10)
check("…bad seq remembered, no trial left", trial.bad_seq(APP) == 11 and not (APP / ".trial.json").exists())
check("updater skips the bad release", upd() is None and marker() == "v10")

publish(12, "v12")
check("the fix (higher seq) installs", upd() and marker() == "v12")
check("healthy release: ok() ends the trial", trial.start(APP, quiet) == "trial" and (trial.ok(APP) or True)
      and not (APP / ".trial.json").exists() and trial.start(APP, quiet) is None)

publish(13, "v13")
upd()
(APP / ".trial.json").write_text("{not json")
check("corrupt trial file counts as a start, never crashes", trial.start(APP, quiet) == "trial")
srv.shutdown()
done()
