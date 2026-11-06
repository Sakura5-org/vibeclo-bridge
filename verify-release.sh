#!/bin/bash
# Check that what clo.sakura5.app hands out — and what is installed on this computer — is exactly the code in this
# repository: every file of the signed release manifest must match bridge/<file> here byte for byte (SHA-256).
#   git clone https://github.com/Sakura5-org/vibeclo-bridge && cd vibeclo-bridge && ./verify-release.sh
# Needs only bash, curl and python3. Run it before installing, or any time after.
set -euo pipefail
cd "$(dirname "$0")"
BASE="${CLO2_BASE:-https://clo.sakura5.app}"
APP="$HOME/.clo2/app"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
curl -fsSL "$BASE/bridge/MANIFEST.json" -o "$tmp/MANIFEST.json"
curl -fsSL "$BASE/bridge/MANIFEST.sig" -o "$tmp/MANIFEST.sig"
curl -fsSL "$BASE/install.sh" -o "$tmp/install.sh"

export RELEASE_PUB="$(sed -n 's/.*CLO2_RELEASE_PUB:-\([A-Za-z0-9+/=]*\)}.*/\1/p' install.sh)"
# 1. The signature, with the same check the installer runs (taken from this repo's install.sh, not the downloaded one).
python3 -I - "$tmp/MANIFEST.json" "$tmp/MANIFEST.sig" > /dev/null < <(sed -n "/^FILES=.*<<'PY'$/,/^PY$/p" install.sh | sed '1d;$d') \
  && echo "✓ release manifest is signed with the Vibeclo release key" \
  || { echo "✗ release manifest signature does NOT verify"; exit 1; }

# 2. Files: release vs this repo (and vs the installed copy, if any).
python3 -I - "$tmp/MANIFEST.json" "$APP" <<'PY'
import hashlib, json, pathlib, sys
m = json.load(open(sys.argv[1])); app = pathlib.Path(sys.argv[2]); bad = 0
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
print(f"release {m['version']} (seq {m['seq']}), {len(m['files'])} files")
for name, h in sorted(m["files"].items()):
    here = sha(pathlib.Path("bridge") / name)
    if here != h:
        bad += 1; print(f"✗ {name}: differs from bridge/{name} in this repo" if here else f"✗ {name}: not in this repo")
inst = pathlib.Path(app / "MANIFEST.json")
if inst.is_file():
    im = json.loads(inst.read_text())
    diff = [n for n, h in im["files"].items() if sha(app / n) != h]
    print(f"installed: {im.get('version')} (seq {im.get('seq')})" + (" — same as the release" if im.get("seq") == m["seq"] else ""))
    for n in diff: bad += 1; print(f"✗ installed {n} was changed after install")
    if im.get("seq") == m["seq"] and not diff: print("✓ installed bridge = release")
print("✓ the release is exactly this repository's bridge/" if not bad else f"{bad} problem(s) — see above")
sys.exit(1 if bad else 0)
PY
cmp -s "$tmp/install.sh" install.sh && echo "✓ the one-line installer on $BASE = install.sh in this repo" \
  || { echo "✗ the installer on $BASE differs from install.sh in this repo"; exit 1; }
