"""Notification summary, the whole way: test bridge → relay → APNs sandbox → the iOS simulator's Notification Service
Extension (CLONotify) decrypts the title + summary. Needs a booted simulator with a Debug build of the app installed
(Apple-silicon Mac: simulators get real sandbox tokens).
  ~/.clo2/venv/bin/python -I bridge/tests/push_sim.py [simulator-udid]"""
import base64, re, secrets, subprocess, sys, time, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import Bridge, check, done

SIM = sys.argv[1] if len(sys.argv) > 1 else "booted"
APP = "com.leo.clo2"
b = Bridge(7798, "pushsim")
a = b.pair("Simulator")
log = pathlib.Path("/tmp/clo2-pushsim/app.log")


def launch(*args):
    subprocess.run(["pkill", "-f", f"simctl launch --console-pty {SIM}"], capture_output=True)
    f = open(log, "w")
    return subprocess.Popen(["xcrun", "simctl", "launch", "--console-pty", "--terminate-running-process", SIM, APP, *args],
                            stdout=f, stderr=subprocess.STDOUT)


p = launch("-pushTestKey", base64.b64encode(a.key).decode())
token = ""
for _ in range(40):
    m = re.search(r"APNSTOKEN ([0-9a-f]{40,200})", log.read_text(errors="replace"))
    if m:
        token = m.group(1); break
    time.sleep(0.5)
check("simulator registered for remote notifications", bool(token), log.read_text(errors="replace")[-500:])
subprocess.run(["xcrun", "simctl", "launch", SIM, "com.apple.Preferences"], capture_output=True)   # app in background
r = a.call("PATCH", f"clo/local/devices/{token}", {"fields": {"token": {"stringValue": token}, "env": {"stringValue": "sandbox"}}})
check("phone registered its token on the Mac", r["status"] == 200)

rid = secrets.token_hex(8)
a.queue(rid, "Reply with one short sentence saying the push test worked.", profile="claude-yolo")
st, _ = a.wait(rid)
for _ in range(30):
    if re.search(r"push (done|input) " + token[:8] + r".*\+summary", b.log()):
        break
    time.sleep(0.5)
check("turn done, pushed with an encrypted summary", st == "done" and re.search(r"push (done|input) " + token[:8] + r".*\+summary", b.log()))
check("APNs accepted it (no error from the relay)", "push failed" not in b.log() and "dropped a dead device token" not in b.log(), b.log()[-800:])
time.sleep(6)
p.terminate()
p = launch("-dumpNotifications", "YES")
time.sleep(8)
out = [l for l in log.read_text(errors="replace").splitlines() if l.startswith("DELIVERED|")]
print("   delivered:", out[:3])
check("the extension replaced 'Claude is done' with the session title + summary",
      any(not l.endswith("|Claude is done") for l in out), out)
p.terminate()
done()
