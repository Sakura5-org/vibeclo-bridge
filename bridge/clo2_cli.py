"""`clo2` — CLO 2 on this Mac, from Terminal (talks to the running bridge on 127.0.0.1 only).

  clo2 pair                 show a QR code here; scan it with the Vibeclo app (or pair over Wi-Fi / Tailscale)
  clo2 status               connection, settings, paired devices
  clo2 devices              paired devices · clo2 revoke NAME|ID  removes one
  clo2 settings             choose where devices may pair from and what Claude may do (interactive)
  clo2 settings pair-via tailnet|wifi|both|any · clo2 settings access standard|full [--yes]
  clo2 tailscale            let the web app on your other computers reach this Mac over Tailscale (sets up
                            `tailscale serve` on :8443 once; run it here in Terminal)
  clo2 skill install|remove teach Claude Code about Vibeclo (~/.claude/skills/clo2)
  clo2 uninstall            stop Vibeclo and remove its agents, commands and skill (data in ~/.clo2 stays)
"""
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.request

PAGE = os.environ.get("CLO2_PAGE", "http://127.0.0.1:7788")
HOME = pathlib.Path.home()
HERE = pathlib.Path(__file__).resolve().parent
SKILL = HOME / ".claude/skills/clo2/SKILL.md"
MARK = "<!-- written by CLO 2 (clo2 skill remove deletes it) -->"

PAIR_VIA = [("tailnet", "Tailscale only — devices on your tailnet"), ("wifi", "Same Wi-Fi only"),
            ("both", "Tailscale or same Wi-Fi (recommended)"), ("any", "Anywhere — also pairing links sent over the internet")]
ACCESS = [("full", "Full access (default) — claude --dangerously-skip-permissions: Claude runs ANY command without "
                   "asking"),
          ("standard", "Standard — claude --permission-mode acceptEdits: edits files in the project; scripts, installs "
                       "and network commands are refused")]

WARN_ACCESS = """
  ⚠️  Full access ("yolo") means Claude Code runs every command without asking: it can delete files, install
     software, read anything your user can read, and push code. A paired phone or browser can start such a turn.
     Use it only on a Mac you own, in projects under git with backups, and revoke lost devices at once
     (clo2 devices / the Vibeclo menu bar icon).
"""

SKILL_TEXT = f"""---
name: clo2
description: Vibeclo (formerly CLO 2) — the user drives Claude Code on this Mac from their phone/browser. Use when the user mentions Vibeclo or CLO 2,
  pairing a phone, artifacts (clo2://artifact links), the secrets vault (clo2-secret), or who/what can access this Mac.
---
{MARK}
# CLO 2 on this Mac

The user may be reading this session in the Vibeclo app (phone) or web app. Turns started from there run as
`claude -p` through the Vibeclo bridge (LaunchAgent `com.clo2.bridge`, data in `~/.clo2`).

## Commands (Terminal)
- `clo2 pair` — QR code to pair a phone/browser · `clo2 status` · `clo2 devices` · `clo2 revoke NAME`
- `clo2 settings` — where devices may pair from (Tailscale / Wi-Fi / both / anywhere) and access level
  (Standard, or Full access = yolo). Never switch to Full access without the user explicitly asking.
- `clo2 uninstall`

## Showing results on the phone
- `clo2-artifact FILE.html --title "…"` publishes a self-contained HTML page; put the printed `clo2://artifact/…` link on
  its own line in the reply — the app shows a live preview. Rerun with `--id <id>` to update.

## Secrets
- `clo2-secret list` shows names only. Use a value without printing it: `X=$(clo2-secret get NAME) cmd`,
  `eval "$(clo2-secret env NAME)"`, `clo2-secret run NAME -- cmd`. Never echo, log, commit or write a value.
- Missing secret: put the line printed by `clo2-secret ask NAME --label "…"` in the reply (the app shows a secure input).
"""


def api(path, body=None, timeout=5):
    req = urllib.request.Request(PAGE + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"X-CLO": "1", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def state():
    try:
        return api("/api/state")
    except Exception:
        start = ("systemctl --user restart clo2-bridge" if sys.platform.startswith("linux")
                 else "launchctl kickstart gui/%d/com.clo2.bridge" % os.getuid())
        sys.exit(f"Vibeclo isn't running on this Mac. Start it: {start} "
                 "(or reinstall: curl -fsSL https://clo2.sakura5.app/install.sh | bash)")


def tty_input(prompt):
    """Prompts work under `curl … | bash` too (stdin is the script there): read the terminal itself."""
    try:
        with open("/dev/tty") as t:
            sys.stdout.write(prompt)
            sys.stdout.flush()
            return t.readline().strip()
    except OSError:
        return ""


def choose(title, options, current):
    print(f"\n{title}")
    for i, (k, label) in enumerate(options, 1):
        print(f"  {i}. {label}" + ("   ← current" if k == current else ""))
    while True:
        a = tty_input(f"Choose 1–{len(options)} [Enter = keep]: ")
        if not a:
            return current
        if a.isdigit() and 1 <= int(a) <= len(options):
            return options[int(a) - 1][0]


def set_access(value, ask=True):
    if value == "full" and ask:
        print(WARN_ACCESS)
        if tty_input('  Type "yes" to allow Full access: ').lower() != "yes":
            print("  Kept Standard.")
            return
    api("/api/settings", {"key": "access", "value": value})
    print(f"Access: {dict(ACCESS)[value].split(' — ')[0]}")


def qr_text(link):
    try:
        import segno
    except ImportError:
        return None
    import io
    out = io.StringIO()
    segno.make(link, error="m").terminal(out=out, compact=True, border=2)
    return out.getvalue()


def cmd_pair():
    s = state()
    before = {d["id"] for d in s["devices"]}
    api("/api/pair/start", {})
    time.sleep(0.5)
    s = state()
    link = s.get("pairing")
    if not link:
        sys.exit("Couldn't start pairing — is this Mac online? (clo2 status)")
    print(f"\nPair a phone with {s.get('label') or 'this Mac'}\n")
    q = qr_text(link)
    print(q if q else "(QR needs the segno package — open this link on the phone instead)\n" + link)
    print("  iPhone: open Vibeclo → Scan QR code (or the Camera app). Scanning this code pairs at once.")
    print("  Same Wi-Fi / Tailscale: tap the Mac in the app, then type the 4-digit code in the dialog on this Mac.")
    print("  ⚠️  A paired device can run Claude Code on this Mac as you. Only pair devices you own.\n")
    print("Waiting… (Ctrl-C to stop; the code expires in 5 minutes)")
    try:
        while True:
            time.sleep(1.5)
            s = state()
            new = [d for d in s["devices"] if d["id"] not in before]
            if new:
                print(f"\n✓ Paired: {', '.join(d['name'] for d in new)}. Manage devices: clo2 devices")
                return
            if not s.get("pairing"):
                print("\nPairing ended (expired or cancelled). Run clo2 pair again.")
                return
    except KeyboardInterrupt:
        api("/api/pair/stop", {})
        print("\nStopped.")


def cmd_status():
    s = state()
    print(f"{s.get('label') or 'This Mac'} · {'online' if s.get('online') else 'offline (relay not reachable)'}")
    print(f"Pairing from: {dict(PAIR_VIA).get(s.get('pairVia'), s.get('pairVia'))}")
    print(f"Access: {dict(ACCESS).get(s.get('access'), s.get('access')).split(' — ')[0]}")
    print(f"Paired devices: {len(s['devices'])}" + (f" · waiting: {len(s['pending'])}" if s.get("pending") else ""))
    print(f"Claude Code skill: {'installed' if SKILL.exists() else 'not installed (clo2 skill install)'}")


def cmd_devices():
    devs = state()["devices"]
    if not devs:
        return print("No paired devices. Pair one: clo2 pair")
    for d in devs:
        seen = time.strftime("%Y-%m-%d %H:%M", time.localtime(d["lastSeen"])) if d.get("lastSeen") else "never"
        print(f"  {d['name']:<28} added {time.strftime('%Y-%m-%d', time.localtime(d['addedAt']))} · last used {seen} · id {d['id'][:10]}")
    print("Remove one: clo2 revoke NAME")


def cmd_revoke(who):
    devs = state()["devices"]
    hit = [d for d in devs if d["name"] == who or d["id"].startswith(who)]
    if len(hit) != 1:
        sys.exit(f"{'No device' if not hit else 'Several devices'} match “{who}” — see clo2 devices")
    api("/api/revoke", {"id": hit[0]["id"]})
    print(f"Revoked {hit[0]['name']}.")


def cmd_settings(args):
    yes = "--yes" in args
    args = [a for a in args if a != "--yes"]
    if len(args) == 2:
        key, value = {"pair-via": "pairVia", "access": "access"}.get(args[0], ""), args[1]
        if key == "access" and value in dict(ACCESS):
            if value == "full" and not yes and not sys.stdin.isatty():
                sys.exit("Full access needs a person at the terminal (or --yes): clo2 settings access full")
            return set_access(value, ask=not yes)
        if key == "pairVia" and value in dict(PAIR_VIA):
            api("/api/settings", {"key": "pairVia", "value": value})
            return print(f"Pairing from: {dict(PAIR_VIA)[value]}")
        sys.exit(__doc__)
    s = state()
    via = choose("Which devices may ask to pair with this Mac? (A QR code shown on this Mac always works.)", PAIR_VIA, s.get("pairVia"))
    if via != s.get("pairVia"):
        api("/api/settings", {"key": "pairVia", "value": via})
    access = choose("What may Claude do for a paired device?", ACCESS, s.get("access"))
    if access != s.get("access"):
        set_access(access)
    cmd_status()


def cmd_tailscale():
    sys.path.insert(0, str(HERE))
    import tunnel
    try:
        cfg = json.loads(os.environ.get("CLO_CONFIG") or "{}")
        host, login = tunnel.setup_ts_web(int(cfg.get("ts_web", 7779)))
    except Exception as e:
        sys.exit(f"Couldn't set it up: {e}")
    print(f"Done. On another computer signed in to Tailscale as {login or 'you'}: open https://clo2.sakura5.app →")
    print(f"“Pair over Tailscale” → {host.rsplit(':', 1)[0]} → type the 4 digits in the Vibeclo dialog on this Mac.")
    subprocess.run(["bash", str(HERE / "restart.sh")])


def skill_install(quiet=False):
    if SKILL.exists() and MARK not in SKILL.read_text(errors="ignore"):
        return print(f"{SKILL} exists and isn't CLO 2's — left alone.")
    SKILL.parent.mkdir(parents=True, exist_ok=True)
    SKILL.write_text(SKILL_TEXT)
    if not quiet:
        print(f"Claude Code skill installed: {SKILL}")


def skill_remove():
    if SKILL.exists() and MARK in SKILL.read_text(errors="ignore"):
        SKILL.unlink()
        try:
            SKILL.parent.rmdir()
        except OSError:
            pass
        print("Claude Code skill removed.")


def cmd_uninstall():
    if tty_input("Stop Vibeclo on this Mac and remove it (your data in ~/.clo2 stays)? [y/N] ").lower() != "y":
        return
    subprocess.run(["bash", str(HERE / "install.sh"), "uninstall"])
    skill_remove()
    for f in (HOME / ".local/bin").glob("clo2*"):
        if "# CLO 2 (written by the bridge)" in f.read_text(errors="ignore"):
            f.unlink()
    print("Vibeclo removed. Delete ~/.clo2 to wipe pairing keys, secrets and artifacts too.")


def main(argv):
    cmd, args = (argv[0] if argv else "help"), argv[1:]
    if cmd == "pair":
        cmd_pair()
    elif cmd == "status":
        cmd_status()
    elif cmd == "devices":
        cmd_devices()
    elif cmd == "revoke" and args:
        cmd_revoke(" ".join(args))
    elif cmd == "settings":
        cmd_settings(args)
    elif cmd == "tailscale":
        cmd_tailscale()
    elif cmd == "skill" and args[:1] == ["install"]:
        skill_install()
    elif cmd == "skill" and args[:1] == ["remove"]:
        skill_remove()
    elif cmd == "uninstall":
        cmd_uninstall()
    else:
        print(__doc__.strip())


if __name__ == "__main__":
    main(sys.argv[1:])
