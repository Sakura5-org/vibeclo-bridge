#!/usr/bin/env bash
# CLO 2 on this Mac: a private Python env + a per-user LaunchAgent (starts at login, restarts if it dies).
# Linux (Ubuntu): a systemd user service instead (clo2-bridge.service, lingering so it runs without a login); no menu bar.
# Asks (in the terminal) where devices may pair from, what Claude may do, and whether to add the Claude Code skill;
# then shows a QR code to pair your phone (`clo2 pair`).
#   ./install.sh            install + start
#   ./install.sh menubar    (re)install only the menu bar agent (the updater uses this; the bridge keeps running)
#   ./install.sh uninstall  stop + remove both agents (your data in ~/.clo2 stays; delete that folder to wipe it)
set -euo pipefail
LABEL=com.clo2.bridge
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$HOME/Library/Logs/clo2-bridge.log"
VENV="$HOME/.clo2/venv"

LINUX=""; [[ "$(uname -s)" == Linux ]] && LINUX=1
UNIT="$HOME/.config/systemd/user/clo2-bridge.service"
if [[ -n "$LINUX" ]]; then
  LOG="$HOME/.clo2/bridge.log"
  # over ssh `systemctl --user` can't find the user's manager without these
  export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
  export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"
fi

MLABEL=com.clo2.menubar
MPLIST="$HOME/Library/LaunchAgents/$MLABEL.plist"

# The menu bar: its own agent, so it survives bridge restarts. Exits 0 on "Thoát thanh menu" → stays off until next login.
menubar() {
  "$VENV/bin/pip" install -q --disable-pip-version-check "rumps>=0.4,<0.5" || { echo "warning: menu bar not installed (pip rumps failed)"; return 0; }
  mkdir -p "$HOME/Library/LaunchAgents"
  cat > "$MPLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$MLABEL</string>
  <key>ProgramArguments</key><array><string>$VENV/bin/python</string><string>-I</string><string>$DIR/clo2_menubar.py</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>LimitLoadToSessionType</key><string>Aqua</string>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/clo2-menubar.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/clo2-menubar.log</string>
</dict></plist>
PL
  [[ -n "${CLO2_NO_AGENT:-}" || -n "$LINUX" ]] && return 0
  launchctl bootout "gui/$(id -u)/$MLABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$MPLIST" 2>/dev/null || true
}

if [[ "${1:-}" == "menubar" ]]; then
  [[ -z "$LINUX" && -f "$DIR/clo2_menubar.py" && -x "$VENV/bin/python" ]] || exit 0
  menubar; exit 0
fi
if [[ -n "$LINUX" ]]; then
  [[ -n "${CLO2_NO_AGENT:-}" ]] || systemctl --user stop clo2-bridge 2>/dev/null || true
  if [[ "${1:-}" == "uninstall" ]]; then
    systemctl --user disable clo2-bridge 2>/dev/null || true
    rm -f "$UNIT"; systemctl --user daemon-reload; echo "removed clo2-bridge.service (data kept in ~/.clo2)"; exit 0
  fi
else
[[ -n "${CLO2_NO_AGENT:-}" ]] || launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
fi
if [[ "${1:-}" == "uninstall" ]]; then
  launchctl bootout "gui/$(id -u)/$MLABEL" 2>/dev/null || true
  rm -f "$PLIST" "$MPLIST"; echo "removed $LABEL + $MLABEL (data kept in ~/.clo2)"; exit 0
fi

command -v claude >/dev/null || [[ -x "$HOME/.local/bin/claude" ]] || echo "warning: Claude Code (claude) not found — install it first: https://claude.com/claude-code"
mkdir -p -m 700 "$HOME/.clo2"
[[ -x "$VENV/bin/python" ]] || python3 -m venv "$VENV" || {
  rm -rf "$VENV"; echo "Python's venv module is missing. On Ubuntu/Debian: sudo apt install python3-venv — then run this again."; exit 1; }
"$VENV/bin/pip" install -q --disable-pip-version-check "cryptography>=42,<51" "websockets>=13,<18" "segno>=1.6,<2"   # capped: a new major never arrives unreviewed

# Questions — read from the terminal itself (under `curl … | bash` stdin is the script). No terminal / tests: defaults.
ask() {
  local a=""
  if [[ -z "${CLO2_NO_AGENT:-}" ]] && { : </dev/tty; } 2>/dev/null; then printf '%s' "$1" >/dev/tty; read -r a </dev/tty || a=""; fi
  echo "$a"
}
setting() { "$VENV/bin/python" -I -c 'import json,pathlib,sys,os
p = pathlib.Path.home() / ".clo2/settings.json"
d = json.loads(p.read_text()) if p.exists() else {}
d[sys.argv[1]] = sys.argv[2]
p.write_text(json.dumps(d)); os.chmod(p, 0o600)' "$1" "$2"; }
if [[ ! -f "$HOME/.clo2/settings.json" ]]; then
  cat <<'TXT'

  Vibeclo lets your phone (and browsers you pair) run Claude Code on this Mac — reading and changing files and running
  commands as your user. Only pair devices you own; revoke them any time with `clo2 devices` or the menu bar icon.

  Which devices may ask to pair with this Mac? (Scanning the QR code shown on this Mac always works.)
    1. Tailscale only      2. Same Wi-Fi only      3. Tailscale or same Wi-Fi (recommended)
    4. Anywhere — also pairing links sent over the internet
TXT
  case "$(ask '  Choose 1–4 [3]: ')" in 1) via=tailnet;; 2) via=wifi;; 4) via=any;; *) via=both;; esac
  cat <<'TXT'

  What may Claude do for a paired device?
    1. Full access (default) — runs `claude --dangerously-skip-permissions`: Claude runs ANY command without asking.
       It can delete files, install software and read anything your user can. Keep projects in git, with backups.
    2. Standard — runs `claude --permission-mode acceptEdits`: edits files in the project; scripts, installs and
       network commands are refused.
TXT
  access=full
  [[ "$(ask '  Choose 1–2 [1]: ')" == 2 ]] && access=standard
  setting pairVia "$via"; setting access "$access"
  echo "  → pairing from: $via · access: $access   (change later: clo2 settings)"
  skill="$(ask $'\n  Teach Claude Code about Vibeclo (adds the skill ~/.claude/skills/clo2)? [Y/n] ')"
  # no one at the terminal: don't add it unasked (clo2 skill install does it later)
  if [[ -z "${CLO2_NO_AGENT:-}" ]] && { : </dev/tty; } 2>/dev/null && [[ "$skill" != [nN]* ]]; then
    "$VENV/bin/python" -I "$DIR/clo2_cli.py" skill install || echo "  (skill not added — run clo2 skill install later)"
  fi
fi
if [[ -n "$LINUX" ]]; then
  mkdir -p "$(dirname "$UNIT")"
  cat > "$UNIT" <<UN
[Unit]
Description=Vibeclo bridge (phone ↔ Claude Code on this computer)
After=network-online.target

[Service]
ExecStart=$VENV/bin/python -I $DIR/clo_bridge.py
WorkingDirectory=$HOME
Environment=PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
Restart=always
RestartSec=5
StandardOutput=append:$LOG
StandardError=append:$LOG

[Install]
WantedBy=default.target
UN
  if [[ -n "${CLO2_NO_AGENT:-}" ]]; then echo "(test: service not started)"; exit 0; fi
  loginctl enable-linger "$USER" 2>/dev/null || echo "note: runs only while you're logged in (loginctl enable-linger $USER needs admin)"
  systemctl --user daemon-reload
  systemctl --user enable --now clo2-bridge
  if command -v ufw >/dev/null && sudo -n ufw status 2>/dev/null | grep -q "Status: active" && ! sudo -n ufw status 2>/dev/null | grep -q 7789; then
    echo "note: the ufw firewall blocks phones on the same Wi-Fi (Tailscale still works). To allow them:"
    echo "      sudo ufw allow from 192.168.0.0/16 to any port 7789 proto tcp"
  fi
else
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$VENV/bin/python</string><string>-I</string><string>$DIR/clo_bridge.py</string></array>
  <key>WorkingDirectory</key><string>$HOME</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>HOME</key><string>$HOME</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict></plist>
PL
if [[ -n "${CLO2_NO_AGENT:-}" ]]; then menubar; echo "(test: LaunchAgents not loaded)"; exit 0; fi   # installer test
launchctl bootstrap "gui/$(id -u)" "$PLIST"
[[ -f "$DIR/clo2_menubar.py" ]] && menubar
fi
echo "Vibeclo running · log: $LOG"
echo "Commands: clo2 pair · clo2 status · clo2 devices · clo2 settings · clo2 uninstall"
# First install (nothing paired yet): show the QR right here, so the next step is just "scan it with the iPhone".
s=""
for _ in $(seq 1 30); do
  s="$(curl -fsS -m 2 http://127.0.0.1:7788/api/state 2>/dev/null)" && break; sleep 1
done
if [[ "$s" == *'"devices": []'* ]] && { : </dev/tty; } 2>/dev/null; then
  "$VENV/bin/python" -I "$DIR/clo2_cli.py" pair </dev/tty || true
else
  echo "Pair a phone: clo2 pair   (if “clo2” isn't found, open a new Terminal window or add ~/.local/bin to PATH)"
fi
