"""CLO 2 bridge auto-update for Macs that installed with the one-line installer (~/.clo2/app, not a git checkout).

The site serves bridge/MANIFEST.json {version, seq, files: {name: sha256}, pip: [...]} and bridge/MANIFEST.sig, an
Ed25519 signature of those exact bytes made by deploy.sh with the release key (kept in the owner's vault, never on the
site). An update is taken only if the signature checks against RELEASE_PUB below, `seq` is higher than the installed
one (no rollback to an older signed release) and every downloaded file matches its hash. Files are staged in a temp
folder first; any failure leaves the installed bridge untouched. The caller restarts gracefully (after running turns).

  python3 -I updater.py [APP_DIR]   # one check now (prints what happened)
"""
import base64
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))   # run as a script with -I
import trial  # noqa: E402

RELEASE_PUB = "QHso7bBNWwK4dDP4YdJSX0Ap6MgoP+od54y9TQsAySQ="   # Ed25519; private half: vault secret CLO2_RELEASE_KEY
BASE = os.environ.get("CLO2_BASE") or "https://clo.sakura5.app"
NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")
EXECUTABLE = {"install.sh", "restart.sh", "clo-resume"}
MAX_FILE = 8 * 1024 * 1024


def _get(url, limit=MAX_FILE):
    req = urllib.request.Request(url, headers={"Cache-Control": "no-cache", "User-Agent": "clo2-bridge"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{url}: too large")
    return data


def verify(manifest_bytes, sig_b64, pub_b64=RELEASE_PUB):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    Ed25519PublicKey.from_public_bytes(base64.b64decode(pub_b64)).verify(base64.b64decode(sig_b64.strip()), manifest_bytes)
    m = json.loads(manifest_bytes)
    if not isinstance(m.get("seq"), int) or not isinstance(m.get("files"), dict) or "clo_bridge.py" not in m["files"]:
        raise ValueError("manifest shape")
    for name, digest in m["files"].items():
        if not NAME.match(name) or not re.fullmatch(r"[0-9a-f]{64}", str(digest)):
            raise ValueError(f"manifest entry {name!r}")
    return m


def installed(app_dir):
    try:
        return json.loads((pathlib.Path(app_dir) / "MANIFEST.json").read_text())
    except (OSError, ValueError):
        return {"seq": 0}


def snapshot(app):
    """Copy the installed release (its manifest's files + MANIFEST.json + VERSION) to app/prev/, replacing the last one."""
    prev = app / "prev"
    shutil.rmtree(prev, ignore_errors=True)
    prev.mkdir()
    for name in [*(installed(app).get("files") or {}), "MANIFEST.json", "VERSION"]:
        if re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", name) and (app / name).is_file():
            shutil.copy2(app / name, prev / name)


def check(app_dir, log=print, base=BASE, pub=RELEASE_PUB, python=sys.executable):
    """One update pass → the new manifest if files were replaced, else None. Raises on a bad/tampered release."""
    app = pathlib.Path(app_dir)
    raw = _get(f"{base}/bridge/MANIFEST.json", 256 * 1024)
    m = verify(raw, _get(f"{base}/bridge/MANIFEST.sig", 4096).decode(), pub)
    if m["seq"] <= int(installed(app).get("seq") or 0) or m["seq"] <= trial.bad_seq(app):
        return None   # nothing newer, or this is the release that was rolled back (a later seq is the fix)
    stage = pathlib.Path(tempfile.mkdtemp(prefix="upd.", dir=app.parent))
    try:
        for name, digest in m["files"].items():
            data = _get(f"{base}/bridge/{name}")
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError(f"{name}: hash mismatch — update skipped")
            (stage / name).write_bytes(data)
        if b"CLO 2 bridge" not in (stage / "clo_bridge.py").read_bytes():
            raise ValueError("clo_bridge.py: not the CLO 2 bridge")
        pip = [p for p in m.get("pip") or [] if re.fullmatch(r"[A-Za-z0-9_.\-\[\]<>=,]{1,60}", str(p))]
        if sys.platform.startswith("linux"):
            pip = [p for p in pip if re.split(r"[<>=]", p)[0] != "rumps"]   # the menu bar is macOS only (rumps won't build on Linux)
        if pip:   # new dependency (e.g. the menu bar's rumps): install before the new code needs it
            r = subprocess.run([python, "-m", "pip", "install", "-q", "--disable-pip-version-check", *pip],
                               capture_output=True, text=True, timeout=600)
            if r.returncode != 0:
                raise RuntimeError("pip: " + (r.stderr.strip().splitlines() or ["failed"])[-1])
        snapshot(app)   # the running release → prev/, so trial.py can put it back if the new one keeps crashing
        (app / ".trial.json").write_text(json.dumps({"seq": m["seq"], "version": m.get("version"), "starts": 0,
                                                     "from": installed(app).get("seq")}))
        for name in m["files"]:
            if name in EXECUTABLE:
                os.chmod(stage / name, 0o755)
            os.replace(stage / name, app / name)
        (app / "VERSION").write_text(str(m.get("version") or "") + "\n")   # what the menu bar shows
        (app / "MANIFEST.json").write_bytes(raw)   # last: a half-done swap retries next time
        log("update: installed", m.get("version"), f"(seq {m['seq']})")
        return m
    finally:
        shutil.rmtree(stage, ignore_errors=True)


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else str(pathlib.Path(__file__).resolve().parent)
    print(check(target) or "up to date")
