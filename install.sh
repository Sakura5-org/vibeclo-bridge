#!/bin/bash
# Vibeclo bridge on a Mac (or Ubuntu/Linux) — one line:  curl -fsSL https://clo.sakura5.app/install.sh | bash
# Downloads the bridge into ~/.clo2/app, sets up a private Python env and a LaunchAgent, then opens the pairing page.
# Uninstall:  bash ~/.clo2/app/install.sh uninstall   (your data in ~/.clo2 stays; delete the folder to wipe it)
set -euo pipefail
BASE="${CLO2_BASE:-https://clo.sakura5.app}"
APP="$HOME/.clo2/app"

case "$(uname)" in Darwin|Linux) ;; *) echo "The Vibeclo bridge runs on macOS or Linux."; exit 1;; esac
command -v python3 >/dev/null || { echo "Python 3 is needed (macOS: xcode-select --install · Ubuntu: sudo apt install python3 python3-venv)."; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || { echo "Python 3.9 or newer is needed."; exit 1; }
if ! command -v claude >/dev/null && [[ ! -x "$HOME/.local/bin/claude" ]]; then
  echo "Claude Code (the claude command) isn't installed. Install it first: https://claude.com/claude-code — then run this again."; exit 1
fi

mkdir -p -m 700 "$HOME/.clo2"
tmp="$(mktemp -d "$HOME/.clo2/dl.XXXXXX")"
trap 'rm -rf "$tmp"' EXIT
curl -fsSL "$BASE/bridge/MANIFEST.json" -o "$tmp/MANIFEST.json"
curl -fsSL "$BASE/bridge/MANIFEST.sig" -o "$tmp/MANIFEST.sig"
curl -fsSL "$BASE/bridge/VERSION" -o "$tmp/VERSION"
# The manifest (name + sha256 of every bridge file) must carry the release signature — the same Ed25519 key installed
# bridges check on every update (bridge/updater.py RELEASE_PUB) — so not even the web host can hand out other code.
# Checked with the standard library only (RFC 8032 verification; no pip packages exist yet at this point).
FILES="$(RELEASE_PUB="${CLO2_RELEASE_PUB:-QHso7bBNWwK4dDP4YdJSX0Ap6MgoP+od54y9TQsAySQ=}" python3 -I - "$tmp/MANIFEST.json" "$tmp/MANIFEST.sig" <<'PY'
import base64, hashlib, json, os, re, sys
p = 2**255 - 19; q = 2**252 + 27742317777372353535851937790883648493
d = -121665 * pow(121666, p - 2, p) % p; I = pow(2, (p - 1) // 4, p)
def add(P, Q):
    A = (P[1] - P[0]) * (Q[1] - Q[0]) % p; B = (P[1] + P[0]) * (Q[1] + Q[0]) % p
    C = 2 * P[3] * Q[3] * d % p; D = 2 * P[2] * Q[2] % p
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F % p, G * H % p, F * G % p, E * H % p)
def mul(s, P):
    Q = (0, 1, 1, 0)
    while s:
        if s & 1: Q = add(Q, P)
        P = add(P, P); s >>= 1
    return Q
def point(b):
    y = int.from_bytes(b, "little"); sign = y >> 255; y &= (1 << 255) - 1
    if y >= p: raise ValueError
    x2 = (y * y - 1) * pow(d * y * y + 1, p - 2, p) % p
    x = pow(x2, (p + 3) // 8, p)
    if (x * x - x2) % p: x = x * I % p
    if (x * x - x2) % p or (x == 0 and sign): raise ValueError
    if x & 1 != sign: x = p - x
    return (x, y, 1, x * y % p)
def verify(pub, msg, sig):
    if len(pub) != 32 or len(sig) != 64: return False
    try: A, R = point(pub), point(sig[:32])
    except ValueError: return False
    s = int.from_bytes(sig[32:], "little")
    if s >= q: return False
    h = int.from_bytes(hashlib.sha512(sig[:32] + pub + msg).digest(), "little") % q
    G = point(int(4 * pow(5, p - 2, p) % p).to_bytes(32, "little"))
    L, Rt = mul(s, G), add(R, mul(h, A))
    return (L[0] * Rt[2] - Rt[0] * L[2]) % p == 0 and (L[1] * Rt[2] - Rt[1] * L[2]) % p == 0
m = open(sys.argv[1], "rb").read()
try: sig = base64.b64decode(open(sys.argv[2]).read().strip(), validate=True)
except ValueError: sig = b""
if not verify(base64.b64decode(os.environ["RELEASE_PUB"]), m, sig): sys.exit("bad signature")
for n, h in json.loads(m)["files"].items():
    if re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", n) and re.fullmatch(r"[0-9a-f]{64}", h): print(n, h)
PY
)" || { echo "The download isn't signed by Vibeclo — not installed. Please try again later or report it: support@sakura5.app"; exit 1; }
[[ -n "$FILES" ]] || { echo "The download looks wrong — please try again later."; exit 1; }
while read -r f h; do
  curl -fsSL "$BASE/bridge/$f" -o "$tmp/$f"
  [[ "$( (command -v sha256sum >/dev/null && sha256sum "$tmp/$f" || shasum -a 256 "$tmp/$f") | cut -d' ' -f1)" == "$h" ]] || { echo "$f didn't download correctly — please try again later."; exit 1; }
done <<< "$FILES"
grep -q "CLO 2 bridge" "$tmp/clo_bridge.py" || { echo "The download looks wrong — please try again later."; exit 1; }
mkdir -p "$APP"
while read -r f h; do mv "$tmp/$f" "$APP/$f"; done <<< "$FILES"
mv "$tmp/VERSION" "$APP/VERSION"; mv "$tmp/MANIFEST.json" "$APP/MANIFEST.json"
chmod +x "$APP/install.sh" "$APP/restart.sh" "$APP/clo-resume"
echo "Vibeclo bridge $(cat "$APP/VERSION") → $APP (signature verified)"
exec bash "$APP/install.sh"
