# Vibeclo bridge

The small helper that connects the **Vibeclo** iPhone app to **Claude Code** on your own computer (macOS or Linux).
Start, follow and steer your Claude Code sessions from your phone — your code, sessions and secrets never leave your
computer.

```bash
curl -fsSL https://clo.sakura5.app/install.sh | bash
```

Then open Vibeclo on your iPhone and pair: on the same Wi-Fi your computer shows up in the app (tap **Pair** and type
the 4-digit code into the dialog on the computer), or run `clo2 pair` and scan the QR code.

Requirements: [Claude Code](https://claude.com/claude-code) signed in with your own account, Python 3.9+, macOS or
Linux. The bridge runs Claude Code under **your** login; Vibeclo never sees your Anthropic credentials.

## Check it before you install

Everything the installer downloads is the code in this repository. To see that for yourself:

```bash
git clone https://github.com/Sakura5-org/vibeclo-bridge && cd vibeclo-bridge
./verify-release.sh
```

It checks the release signature (with the check from this repo's `install.sh`), that every file the server hands out is
byte-for-byte `bridge/` here, that the one-line installer on the server is this repo's `install.sh` — and, once
installed, that the copy on your computer hasn't changed. Prefer not to pipe into bash? Read and run it from the clone:
`bash install.sh` (it downloads the same signed release).

## How it works

```
iPhone (Vibeclo) ──E2E encrypted──▶ relay (Cloudflare, blind) ──▶ bridge on your computer ──▶ claude -p --resume …
        └──────────── same Wi-Fi / Tailscale: direct, still E2E encrypted ─────────────┘
```

- **No account, no cloud storage.** Sessions, transcripts, uploads, artifacts and secrets live on your computer
  (`~/.clo2`, and Claude Code's own `~/.claude`).
- **End-to-end encrypted.** Pairing exchanges X25519 keys (QR code, or a 4-digit code you confirm on the computer);
  every frame is AES-256-GCM. The relay (`relay/`) only forwards ciphertext between your computer and the devices you
  approved; it stores token hashes, never messages. On the same network the phone talks to the bridge directly.
- **You stay in control.** Approve or revoke devices from the menu bar icon or `clo2 devices`. Choose what Claude may
  do: *Standard* (`--permission-mode acceptEdits`) or *Full access* (`--dangerously-skip-permissions`) — `clo2 settings`.
- **Notifications** show the session title and a one-line summary of what Claude did — encrypted on your computer
  for that one phone (a key derived from its pairing) and opened by the app on the phone. Apple and the relay only
  carry ciphertext next to a fixed text ("Claude is done") and a random session id.

## Commands

| | |
|---|---|
| `clo2 pair` | show a QR code to pair a phone |
| `clo2 status` | connection, settings, paired devices |
| `clo2 devices` / `clo2 revoke NAME` | list / remove paired devices |
| `clo2 settings` | where devices may pair from (Wi-Fi, Tailscale, anywhere) and the permission mode |
| `clo2 uninstall` | stop the bridge and remove its agents (your data in `~/.clo2` stays — delete it to wipe) |

Logs: `~/Library/Logs/clo2-bridge.log` (macOS) · `journalctl --user -u clo2-bridge` (Linux).

## Updates

Installs from the one-line installer update themselves (hourly check), always waiting until no Claude turn is running:

- Every release is **signed** (Ed25519). The bridge only installs a release whose signature matches the public key built
  into `bridge/updater.py`, whose sequence number is higher than the installed one (no downgrades) and whose files all
  match their SHA-256. Neither the web host nor the relay can push code to installed bridges.
- A new release is **on trial** (`bridge/trial.py`): if it fails to start three times before running healthy, the
  previous release is restored and the bad one is skipped.
- Python dependencies are installed with upper version bounds, so a new major version never arrives unreviewed.
- Don't want automatic updates? Put `{"auto_update": false}` in `~/.clo2/app/config.json`, or use **Check for updates**
  in the menu bar.

The first install is protected the same way: `install.sh` checks the release manifest's Ed25519 signature against the
same public key (standard-library Python, before anything is installed) and every file's SHA-256, then installs a
LaunchAgent (macOS) or a systemd user service (Linux). Read `install.sh` first if you like.

## Repository

- `bridge/` — the bridge: `clo_bridge.py` (sessions, turns, queue), `tunnel.py` (pairing, encryption, relay and direct
  paths), `localstore.py` (local store), `pairpage.py` (http://127.0.0.1:7788), `clo2_menubar.py`, `clo2_cli.py`,
  `updater.py` + `trial.py` (signed updates, rollback), `install.sh`, `restart.sh`, `tests/`.
- `relay/` — the Cloudflare Worker + Durable Object relay (one room per computer, abuse limits, push via APNs).
- `install.sh` — the one-line installer.

Tests start throwaway bridges with their own ports and identity and never touch an installed one:

```bash
python3 -m venv .venv && .venv/bin/pip install "cryptography>=42,<51" "websockets>=13,<18" "segno>=1.6,<2"
.venv/bin/python bridge/tests/update.py      # also: installer.py, rollback.py, turns.py, files.py, edge_cases.py, lan.py, …
```

Some tests talk to the public relay and run real (cheap) Claude Code turns.

## Security

Please report vulnerabilities privately — see [SECURITY.md](SECURITY.md).

## License

MIT — see [LICENSE](LICENSE). Vibeclo is independent and not affiliated with or endorsed by Anthropic. Claude and
Claude Code are trademarks of Anthropic.
