"""CLO 2 menu bar against a throwaway bridge: the real AppKit menu shows the pending phone (not its code), and
typing the code from that menu pairs it (a wrong code doesn't); "Cancel pairing" closes pairing; no bridge → "isn't running"."""
import json, os, subprocess, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Bridge, Device, check, done, HERE

PY = sys.executable
def menubar(port, click=None, code=None):
    env = {**os.environ, "CLO2_PORT": str(port), **({"CLO2_SELFTEST_CLICK": click} if click else {}),
           **({"CLO2_SELFTEST_CODE": code} if code is not None else {})}
    return subprocess.run([PY, "-I", str(HERE / "clo2_menubar.py"), "--selftest"], env=env, capture_output=True, text=True, timeout=60).stdout

b = Bridge(7790, "menubar")
d = Device(b, "Menu phone")
p = d.pending()
out = menubar(7790)
check("status item with a symbol image", "status item: True · symbol image: True" in out, out[:200])
check("menu shows the pending phone, without the code", "Menu phone wants to connect" in out and p["check"] not in out, out)
check("badge shows 1 waiting", "title: ' 1'" in out, out[:120])
wrong = "0000" if p["check"] != "0000" else "1111"
out = menubar(7790, click="Type the code", code=wrong)
check("a wrong code from the menu doesn't pair", "clicked: Type the code" in out and b.state()["pending"], out[-200:])
out = menubar(7790, click="Type the code", code=p["check"])
check("typing the right code in the menu approves", "clicked: Type the code" in out and d.finish().get("op") == "paired", out[-200:])
out = menubar(7790)
check("menu lists the paired device", "Devices (1)" in out and "Menu phone" in out, out)
check("menu has pairing networks + permissions, Full access ticked (default)", "Allow pairing from" in out and "✓ Full access" in out, out)
b.post("/api/pair/start")
out = menubar(7790, click="Cancel pairing")
check("'Cancel pairing' stops pairing", "clicked: Cancel pairing" in out and not b.state()["pairing"], out[-200:])
out = menubar(7790, click="    Standard")
check("Permissions ▸ Standard sets it", b.state().get("access") == "standard", out[-200:])
menubar(7790, click="    Full access")
check("…and back to Full access", b.state().get("access") == "full")
b.stop()
out = menubar(7790)
check("no bridge → 'isn't running' + start item", "Vibeclo isn't running on this Mac" in out and "Start Vibeclo" in out, out)
done()
