"""CLO 2 bridge auto-update (bridge/updater.py) and the one-line installer against a local web server:
signed release → swap; tampered file / bad or foreign signature / older seq / hostile names → nothing changes."""
import base64, functools, hashlib, http.server, json, os, pathlib, shutil, subprocess, sys, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import check, done
import updater
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

BRIDGE = pathlib.Path(__file__).resolve().parents[1]
ROOT = pathlib.Path("/tmp/clo2-update"); shutil.rmtree(ROOT, ignore_errors=True)
SITE, APP = ROOT / "site", ROOT / "home/.clo2/app"
(SITE / "bridge").mkdir(parents=True); APP.mkdir(parents=True)
key = Ed25519PrivateKey.generate()
PUB = base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
FILES = ["trial.py", "clo_bridge.py", "localstore.py", "tunnel.py", "pairpage.py", "clo2_tools.py", "clo2_cli.py", "updater.py", "clo2_menubar.py",
         "clo-resume", "install.sh", "restart.sh"]


def publish(seq, marker, files=None, sign_with=key, pip=None, tamper=None, raw_manifest=None):
    d = SITE / "bridge"
    for f in FILES:
        data = (BRIDGE / f).read_bytes()
        if f == "localstore.py":
            data += f"\n# release {marker}\n".encode()
        (d / f).write_bytes(data)
    m = raw_manifest or json.dumps({"version": marker, "seq": seq, "pip": pip or [],
                                    "files": files or {f: hashlib.sha256((d / f).read_bytes()).hexdigest() for f in FILES}}).encode()
    (d / "MANIFEST.json").write_bytes(m)
    (d / "MANIFEST.sig").write_text(base64.b64encode(sign_with.sign(m)).decode())
    (d / "VERSION").write_text(marker)
    if tamper:
        (d / tamper).write_bytes((d / tamper).read_bytes() + b"# evil\n")


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a): pass
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(SITE)))
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"
quiet = lambda *a: None
upd = lambda: updater.check(APP, quiet, BASE, PUB, python=str(ROOT / "home/.clo2/venv/bin/python"))   # pip → throwaway venv
marker = lambda: ("release" in (APP / "localstore.py").read_text() and (APP / "localstore.py").read_text().rsplit("release ", 1)[1].strip()) if (APP / "localstore.py").exists() else None

# ---- installer (web/install.sh) from the local site, throwaway HOME, no LaunchAgents
publish(10, "v10")
shutil.copy(BRIDGE.parent / "web/install.sh", SITE / "install.sh")
env = {**os.environ, "HOME": str(ROOT / "home"), "CLO2_BASE": BASE, "CLO2_NO_AGENT": "1"}
r = subprocess.run(["bash", str(SITE / "install.sh")], env=env, capture_output=True, text=True, timeout=900)
check("installer: downloads every manifest file + verifies hashes", r.returncode == 0 and all((APP / f).exists() for f in FILES), r.stdout[-400:] + r.stderr[-400:])
check("installer: keeps MANIFEST.json (seq for the updater)", json.loads((APP / "MANIFEST.json").read_text())["seq"] == 10)
plist = ROOT / "home/Library/LaunchAgents"
check("installer: writes bridge + menu bar agents", (plist / "com.clo2.bridge.plist").exists() and (plist / "com.clo2.menubar.plist").exists(), list(plist.glob("*")))
mp = (plist / "com.clo2.menubar.plist").read_text()
check("menu bar agent: stays quit after 'Thoát' (KeepAlive SuccessfulExit false), GUI session only", "<key>SuccessfulExit</key><false/>" in mp and "Aqua" in mp)
py = ROOT / "home/.clo2/venv/bin/python"
r = subprocess.run([str(py), "-c", "import rumps, cryptography, websockets"], capture_output=True, text=True)
check("installer: venv has rumps + cryptography + websockets", r.returncode == 0, r.stderr[-300:])
r = subprocess.run([str(py), "-I", str(APP / "clo2_menubar.py"), "--check"], capture_output=True, text=True, timeout=60)
check("installed menu bar builds its menu", r.returncode == 0 and "Pair a phone" in r.stdout, r.stderr[-300:])

publish(10, "v10b", tamper="localstore.py")
r = subprocess.run(["bash", str(SITE / "install.sh")], env=env, capture_output=True, text=True, timeout=900)
check("installer: refuses a file that doesn't match the manifest", r.returncode != 0 and "bị sai" in r.stdout and marker() == "v10", r.stdout[-200:])

# ---- updater
publish(10, "v10")
check("same seq → no update", upd() is None and marker() == "v10")
publish(11, "v11")
m = upd()
check("higher seq, good signature → files swapped", m and m["seq"] == 11 and marker() == "v11", marker())
check("installed MANIFEST.json now seq 11", json.loads((APP / "MANIFEST.json").read_text())["seq"] == 11)
check("…and VERSION (shown in the menu bar) says v11", (APP / "VERSION").read_text().strip() == "v11", (APP / "VERSION").read_text())
check("scripts stay executable", os.access(APP / "install.sh", os.X_OK) and os.access(APP / "clo-resume", os.X_OK))
check("no staging folders left behind", not list(APP.parent.glob("upd.*")))


def refused(name, **kw):
    publish(**kw)
    try:
        upd(); ok = False
    except Exception:
        ok = True
    check(name, ok and marker() == "v11" and json.loads((APP / "MANIFEST.json").read_text())["seq"] == 11, marker())


refused("tampered file (hash mismatch) → nothing replaced", seq=12, marker="v12", tamper="tunnel.py")
refused("signed by another key → rejected", seq=12, marker="v12", sign_with=Ed25519PrivateKey.generate())
publish(12, "v12"); (SITE / "bridge/MANIFEST.sig").write_text(base64.b64encode(b"x" * 64).decode())
try: upd(); bad_sig = False
except Exception: bad_sig = True
check("garbage signature → rejected", bad_sig and marker() == "v11")
refused("hostile file name (../) → rejected", seq=12, marker="v12",
        raw_manifest=json.dumps({"version": "v12", "seq": 12, "files": {"clo_bridge.py": "0" * 64, "../evil.py": "0" * 64}}).encode())
refused("manifest without the bridge → rejected", seq=12, marker="v12",
        raw_manifest=json.dumps({"version": "v12", "seq": 12, "files": {"tunnel.py": "0" * 64}}).encode())
d = SITE / "bridge"; publish(12, "v12")
(d / "clo_bridge.py").write_bytes(b"print('hi')\n")
files = {f: hashlib.sha256((d / f).read_bytes()).hexdigest() for f in FILES}
mm = json.dumps({"version": "v12", "seq": 12, "pip": [], "files": files}).encode()
(d / "MANIFEST.json").write_bytes(mm); (d / "MANIFEST.sig").write_text(base64.b64encode(key.sign(mm)).decode())
try: upd(); notbridge = False
except Exception: notbridge = True
check("signed but not the CLO 2 bridge → rejected", notbridge and marker() == "v11")
publish(9, "v9")
check("older signed release (rollback) → ignored", upd() is None and marker() == "v11")
refused("pip install of a package that doesn't exist → no swap", seq=13, marker="v13", pip=["clo2-no-such-package-xyz"])
publish(13, "v13", pip=["rumps"])
check("new release with a pip dependency → installed", (upd() or {}).get("seq") == 13 and marker() == "v13")

# ---- a running installed bridge picks a release up by itself and exits gracefully (launchd would start the new code)
import time
cfg = {"port": 7791, "lan_port": 7891, "update_first": 1, "update_every": 3, "label": "Update test Mac"}
benv = {**env, "CLO_CONFIG": json.dumps(cfg), "CLO2_BASE": BASE}
# the installed updater must trust this test's key: same file, test key swapped in (only inside the throwaway HOME)
u = APP / "updater.py"; u.write_text(u.read_text().replace(updater.RELEASE_PUB, PUB))
publish(13, "v13", pip=["rumps"]); (SITE / "bridge/updater.py").write_bytes(u.read_bytes())
files = {f: hashlib.sha256((SITE / "bridge" / f).read_bytes()).hexdigest() for f in FILES}
def republish(seq, mk):
    publish(seq, mk); (SITE / "bridge/updater.py").write_bytes(u.read_bytes())
    fs = {f: hashlib.sha256((SITE / "bridge" / f).read_bytes()).hexdigest() for f in FILES}
    mm = json.dumps({"version": mk, "seq": seq, "pip": [], "files": fs}).encode()
    (SITE / "bridge/MANIFEST.json").write_bytes(mm); (SITE / "bridge/MANIFEST.sig").write_text(base64.b64encode(key.sign(mm)).decode())
republish(13, "v13")
log = open(ROOT / "bridge.log", "w")
proc = subprocess.Popen([str(py), "-I", str(APP / "clo_bridge.py")], env=benv, stdout=log, stderr=subprocess.STDOUT)
time.sleep(8)
check("installed bridge runs (seq 13 = current → no restart)", proc.poll() is None, (ROOT / "bridge.log").read_text()[-500:])
republish(14, "v14")
for _ in range(60):
    if proc.poll() is not None: break
    time.sleep(1)
text = (ROOT / "bridge.log").read_text()
check("new release → bridge installs it and exits for a graceful restart", proc.poll() == 0 and "update: installed v14" in text
      and "restart requested" in text and marker() == "v14", text[-600:])
if proc.poll() is None: proc.kill()

# ---- the real release key is what the bridge trusts; deploy.sh's manifest parses
check("updater trusts the release key in the vault (pub embedded)", len(base64.b64decode(updater.RELEASE_PUB)) == 32)
done()
