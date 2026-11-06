"""CLO 2 tools on this Mac (no cloud): artifacts and the secrets vault. Used by the bridge (tunnel ops) and as commands:

  clo2-artifact FILE.html --title "…" [--id ID]   → prints clo2://artifact/<id> (the app shows a live preview)
  clo2-secret list | get NAME | env NAME… | run NAME… -- cmd | ask NAME --label "…" | wait NAME

Secrets: one AES-256-GCM file (~/.clo2/secrets.bin, 0600) whose key lives in this Mac's login Keychain (service
"clo2-secrets"). Values arrive from paired devices end-to-end encrypted, are never written to the store, logs or replies,
and are scrubbed from transcripts. Artifacts: ~/.clo2/artifacts/<id>.html + a store doc (title, time) for the library.
"""
import base64
import datetime as dt
import hashlib
import json
import os
import pathlib
import platform
import re
import secrets as _rand
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import localstore  # noqa: E402

DIR = pathlib.Path(os.environ.get("CLO2_DIR") or pathlib.Path.home() / ".clo2")
STORE = os.environ.get("CLO2_STORE") or str(DIR / "store.db")
ARTIFACTS = DIR / "artifacts"
VAULT = DIR / "secrets.bin"
NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{6,40}$")
MACHINE = platform.node().split(".")[0] or "mac"
b64u = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")
now = lambda: dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _store():
    if localstore._db is None:
        localstore.open_store(STORE)
    return localstore


def _enc(v):
    return {"timestampValue": v} if isinstance(v, str) and v.endswith("Z") and "T" in v else (
        {"arrayValue": {"values": [_enc(x) for x in v]}} if isinstance(v, list) else
        {"integerValue": str(v)} if isinstance(v, int) and not isinstance(v, bool) else {"stringValue": str(v)})


# ---------------------------------------------------------------- artifacts
def artifact_save(path, title, aid=None):
    html = pathlib.Path(path).read_bytes()
    if len(html) > 20 * 1024 * 1024:
        raise SystemExit("Artifact too large (max 20 MB)")
    aid = aid if aid and SAFE_ID.match(aid) else b64u(_rand.token_bytes(12))
    ARTIFACTS.mkdir(parents=True, exist_ok=True, mode=0o700)
    dest = ARTIFACTS / f"{aid}.html"
    dest.write_bytes(html)
    os.chmod(dest, 0o600)
    _store().patch(f"clo/local/artifacts/{aid}", {k: _enc(v) for k, v in {
        "title": title or pathlib.Path(path).stem, "url": f"clo2://artifact/{aid}", "source": MACHINE,
        "size": len(html), "updatedAt": now()}.items()})
    return aid


def artifact_html(aid):
    if not SAFE_ID.match(aid or ""):
        return None
    f = ARTIFACTS / f"{aid}.html"
    return f.read_text(errors="replace") if f.exists() else None


# ---------------------------------------------------------------- secrets vault
LINUX = sys.platform.startswith("linux")


def file_key(name):
    """Linux (no login Keychain): 32 random bytes in ~/.clo2/keys/<name> (0600, folder 0700); created on first use."""
    d = DIR / "keys"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    f = d / re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    try:
        return base64.b64decode(f.read_text().strip())
    except (OSError, ValueError):
        pass
    key = os.urandom(32)
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as w:
        w.write(base64.b64encode(key).decode())
    return key


def _vault_key():
    """32 random bytes in the login Keychain (one per CLO 2 data folder); created on first use."""
    account = "vault-" + hashlib.sha256(str(DIR.resolve()).encode()).hexdigest()[:10]
    if LINUX:
        return file_key(f"clo2-secrets-{account}")
    r = subprocess.run(["security", "find-generic-password", "-s", "clo2-secrets", "-a", account, "-w"], capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        return base64.b64decode(r.stdout.strip())
    key = os.urandom(32)
    r = subprocess.run(["security", "add-generic-password", "-U", "-s", "clo2-secrets", "-a", account,
                        "-l", "CLO 2 secrets (this Mac)", "-w", base64.b64encode(key).decode()], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("Keychain: " + (r.stderr.strip() or "could not create the key"))
    return key


def _load():
    if not VAULT.exists():
        return {}
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    raw = VAULT.read_bytes()
    return json.loads(AESGCM(_vault_key()).decrypt(raw[:12], raw[12:], b"clo2-vault"))


def _save(data):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    nonce = os.urandom(12)
    tmp = VAULT.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(nonce + AESGCM(_vault_key()).encrypt(nonce, json.dumps(data).encode(), b"clo2-vault"))
    os.replace(tmp, VAULT)


def secret_set(name, value, label=""):
    if not NAME.match(name or "") or not isinstance(value, str) or not value or len(value) > 64 * 1024:
        raise ValueError("Invalid name or value")
    data = _load()
    data[name] = {"value": value, "label": label[:200], "updatedAt": now()}
    _save(data)
    _store().patch(f"clo/local/secrets/{name}", {k: _enc(v) for k, v in {
        "name": name, "label": label[:200], "machines": [MACHINE], "editedAt": now()}.items()})


def secret_delete(name):
    data = _load()
    data.pop(name, None)
    _save(data)
    _store().delete(f"clo/local/secrets/{name}")


def secret_get(name):
    s = _load().get(name)
    return s["value"] if s else None


def secret_values():
    return {n: s["value"] for n, s in _load().items()}


# ---------------------------------------------------------------- commands
def main(argv):
    if not argv:
        raise SystemExit(__doc__)
    cmd, args = argv[0], argv[1:]
    if cmd == "artifact":
        title, aid, files = "", None, []
        it = iter(args)
        for a in it:
            if a == "--title": title = next(it, "")
            elif a == "--id": aid = next(it, None)
            else: files.append(a)
        if len(files) != 1 or not pathlib.Path(files[0]).is_file():
            raise SystemExit('Usage: clo2-artifact FILE.html --title "…" [--id ID]')
        print(f"clo2://artifact/{artifact_save(files[0], title, aid)}")
    elif cmd == "secret":
        if not args:
            raise SystemExit(__doc__)
        sub, rest = args[0], args[1:]
        if sub == "list":
            for n, s in sorted(_load().items()):
                print(n + (f"  — {s['label']}" if s.get("label") else ""))
        elif sub == "get":
            v = secret_get(rest[0] if rest else "")
            if v is None:
                raise SystemExit(f"No secret {rest[0] if rest else ''} yet — use `clo2-secret ask NAME --label \"…\"`")
            sys.stdout.write(v)
        elif sub == "env":
            for n in rest:
                v = secret_get(n)
                if v is None:
                    raise SystemExit(f"No secret {n} yet")
                print(f"export {n}={shlex_quote(v)}")
        elif sub == "run":
            if "--" not in rest:
                raise SystemExit("Usage: clo2-secret run NAME… -- command")
            i = rest.index("--")
            env = dict(os.environ)
            for n in rest[:i]:
                v = secret_get(n)
                if v is None:
                    raise SystemExit(f"No secret {n} yet")
                env[n] = v
            os.execvpe(rest[i + 1], rest[i + 1:], env)
        elif sub == "ask":
            name = rest[0] if rest else ""
            label = rest[rest.index("--label") + 1] if "--label" in rest and rest.index("--label") + 1 < len(rest) else ""
            if not NAME.match(name):
                raise SystemExit("Secret name: letters, digits, _ (e.g. GITHUB_TOKEN)")
            from urllib.parse import urlencode
            print(f"`clo2-secret://ask?{urlencode({'name': name, 'label': label})}`")
        elif sub == "wait":
            name = rest[0] if rest else ""
            for _ in range(600):
                if secret_get(name) is not None:
                    print(f"{name} already exists")
                    return
                time.sleep(1)
            raise SystemExit(f"Timed out waiting for {name}")
        else:
            raise SystemExit(__doc__)
    else:
        raise SystemExit(__doc__)


def shlex_quote(s):
    import shlex
    return shlex.quote(s)


if __name__ == "__main__":
    main(sys.argv[1:])
