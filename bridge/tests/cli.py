"""`clo2` command against a throwaway bridge: pair prints a QR and notices the new device, status / devices / revoke,
settings (non-interactive) reach the bridge and gate yolo, skill install/remove only touches CLO 2's own file."""
import os, pathlib, subprocess, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Bridge, Device, check, done, HERE

sys.path.insert(0, str(HERE))
b = Bridge(7795, "cli")
home = pathlib.Path(tempfile.mkdtemp())


def clo2(*args, t=60):
    env = {**os.environ, "CLO2_PAGE": b.page, "HOME": str(home)}
    return subprocess.run([sys.executable, "-I", str(HERE / "clo2_cli.py"), *args], env=env, capture_output=True,
                          text=True, timeout=t, stdin=subprocess.DEVNULL)


out = clo2("status").stdout
check("status: online, defaults Tailscale-or-Wi-Fi + Full access", "online" in out and "Tailscale or same Wi-Fi" in out
      and "Access: Full access" in out, out)

# pair: QR in the terminal, then it notices the device that pairs with that code (scan → `a` → approved at once)
res = {}
th = threading.Thread(target=lambda: res.update(r=clo2("pair", t=90)))
th.start()
for _ in range(40):
    if b.state().get("pairing"):
        break
    time.sleep(0.5)
time.sleep(1)
d = Device(b, "CLI phone")   # asks with the sent link → waits for approval (the QR's `a` would pair at once)
out2 = clo2("devices").stdout
check("devices: nothing paired before approval", "CLI phone" not in out2, out2)
d.approve()
th.join(90)
out = res["r"].stdout
check("pair: prints a QR block", "█" in out or "▀" in out or "▄" in out, out[:300])
check("…and 'Only pair devices you own'", "Only pair devices you own" in out)
check("pair noticed the new device", "Paired: CLI phone" in out, out[-300:])
check("devices lists it", "CLI phone" in clo2("devices").stdout)
out = clo2("revoke", "CLI phone").stdout
check("revoke by name", "Revoked CLI phone" in out and not b.state()["devices"], out)

# settings, non-interactive
out = clo2("settings", "pair-via", "tailnet").stdout
check("settings pair-via tailnet", b.state()["pairVia"] == "tailnet", out)
clo2("settings", "access", "standard")
check("settings access standard", b.state()["access"] == "standard")
r = clo2("settings", "access", "full")
check("access full without a terminal and without --yes → refused", r.returncode != 0 and b.state()["access"] == "standard", r.stderr)
out = clo2("settings", "access", "full", "--yes").stdout
check("settings access full --yes", b.state()["access"] == "full", out)
clo2("settings", "access", "standard")
check("bridge heartbeat field follows", b.state()["access"] == "standard")
check("bad value refused", clo2("settings", "access", "root").returncode != 0)
check("bad value via API refused", b.post("/api/settings", {"key": "access", "value": "root"}) == 400)

# skill: install, leave someone else's file alone, remove
out = clo2("skill", "install").stdout
sk = home / ".claude/skills/clo2/SKILL.md"
check("skill installed", sk.exists() and "name: clo2" in sk.read_text(), out)
clo2("skill", "remove")
check("skill removed", not sk.exists())
sk.parent.mkdir(parents=True, exist_ok=True)
sk.write_text("my own skill")
clo2("skill", "install")
check("someone else's skill file left alone", sk.read_text() == "my own skill")
clo2("skill", "remove")
check("…and not removed", sk.exists())

b.stop()
out = clo2("status")
check("bridge down → clear message", out.returncode != 0 and "isn't running" in out.stderr, out.stderr)
done()
