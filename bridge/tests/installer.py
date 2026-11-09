"""The one-line installer (web/install.sh) only installs a manifest signed with the release key.
Local web server, throwaway HOME, test key; the installer's last step (bridge/install.sh: venv + LaunchAgent) is cut off,
so nothing on this computer is touched. Also: its stdlib Ed25519 check agrees with `cryptography`, the baked-in key
matches updater.RELEASE_PUB, and the live release verifies."""
import base64, functools, hashlib, http.server, json, os, pathlib, re, shutil, subprocess, sys, threading, urllib.request
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import check, done
import updater
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

BRIDGE = pathlib.Path(__file__).resolve().parents[1]
REPO = BRIDGE.parent
SRC = next(p for p in (REPO / "web/install.sh", REPO / "install.sh") if p.exists())
ROOT = pathlib.Path("/tmp/clo2-installer"); shutil.rmtree(ROOT, ignore_errors=True)
SITE = ROOT / "site"; (SITE / "bridge").mkdir(parents=True)
FILES = ["trial.py", "clo_bridge.py", "localstore.py", "updater.py", "install.sh", "restart.sh", "clo-resume"]
pub_of = lambda k: base64.b64encode(k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
key, other = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
text = SRC.read_text()
check("installer has the release key of updater.py", f'CLO2_RELEASE_PUB:-{updater.RELEASE_PUB}' in text)
lines = text.splitlines()
assert lines[-1].startswith("exec bash"), lines[-1]
(ROOT / "install.sh").write_text("\n".join(lines[:-1] + ['echo "WOULD-RUN $APP/install.sh"']) + "\n")

# Verifier extracted from the installer, run on its own against `cryptography` signatures.
VERIFY = re.search(r"<<'PY'\n(.*?)\nPY\n", text, re.S).group(1)
(ROOT / "verify.py").write_text(VERIFY)


def stdlib_verify(pub, msg, sig):
    (ROOT / "m").write_bytes(msg); (ROOT / "s").write_text(sig)
    r = subprocess.run([sys.executable, "-I", str(ROOT / "verify.py"), str(ROOT / "m"), str(ROOT / "s")],
                       env={"RELEASE_PUB": pub, "PATH": os.environ["PATH"]}, capture_output=True, text=True)
    return r.returncode == 0


m = json.dumps({"files": {}}).encode()
msgs = [json.dumps({"seq": i, "files": {"a.py": "0" * 64}}).encode() for i in range(1, 6)]
good = [stdlib_verify(pub_of(key), x, base64.b64encode(key.sign(x)).decode()) for x in msgs]
check("stdlib Ed25519 accepts valid signatures", all(good))
sig = key.sign(m)
flipped = bytes([sig[0] ^ 1]) + sig[1:]
check("…rejects a changed message, signature, other key, garbage",
      not stdlib_verify(pub_of(key), m + b" ", base64.b64encode(sig).decode())
      and not stdlib_verify(pub_of(key), m, base64.b64encode(flipped).decode())
      and not stdlib_verify(pub_of(other), m, base64.b64encode(sig).decode())
      and not stdlib_verify(pub_of(key), m, "not base64!!") and not stdlib_verify(pub_of(key), m, ""))


def publish(signer=key, tamper=None, sig=True):
    d = SITE / "bridge"
    for f in FILES: shutil.copy(BRIDGE / f, d / f)
    (d / "VERSION").write_text("test 2026-01-01\n")
    m = json.dumps({"version": "test", "seq": 1, "pip": [],
                    "files": {f: hashlib.sha256((d / f).read_bytes()).hexdigest() for f in FILES}}).encode()
    s = base64.b64encode(signer.sign(m)).decode()
    if tamper: m = tamper(m)
    (d / "MANIFEST.json").write_bytes(m)
    (d / "MANIFEST.sig").unlink(missing_ok=True)
    if sig: (d / "MANIFEST.sig").write_text(s)


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a): pass
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(SITE)))
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"


def install():
    home = ROOT / "home"; shutil.rmtree(home, ignore_errors=True); home.mkdir()
    env = dict(os.environ, HOME=str(home), CLO2_BASE=BASE, CLO2_RELEASE_PUB=pub_of(key))
    r = subprocess.run(["bash", str(ROOT / "install.sh")], env=env, capture_output=True, text=True, timeout=60)
    app = home / ".clo2/app"
    return r.returncode == 0 and "WOULD-RUN" in r.stdout, app, r.stdout + r.stderr


publish()
ok, app, out = install()
check("signed release installs", ok and all((app / f).exists() for f in FILES), out[-300:])

publish(tamper=lambda m: m.replace(b'"version": "test"', b'"version": "evil"'))
ok, app, out = install()
check("tampered manifest → refused, nothing installed", not ok and "isn't signed" in out and not app.exists(), out[-300:])

publish(signer=other)
ok, app, out = install()
check("signed with another key → refused", not ok and not app.exists(), out[-300:])

publish(sig=False)
ok, app, out = install()
check("no signature file → refused", not ok and not app.exists(), out[-300:])

srv.shutdown()
try:
    live = "https://clo2.sakura5.app/bridge/"
    get = lambda f: urllib.request.urlopen(urllib.request.Request(live + f, headers={"User-Agent": "curl/8"}), timeout=15).read()
    m, s = get("MANIFEST.json"), get("MANIFEST.sig").decode()
    check("the live release verifies with the baked-in key", stdlib_verify(updater.RELEASE_PUB, m, s))
except OSError as e:
    print("SKIP live release check:", e)
done()
