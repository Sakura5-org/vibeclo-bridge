"""Pairing for N days (web: "Nhớ trình duyệt này", default 30): the Mac keeps expiresAt; past it the device is removed
and its socket closes with 4001.  ~/.clo2/venv/bin/python -I bridge/tests/keep.py"""
import json, pathlib, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import Bridge, check, done, recv

b = Bridge(7803, "keep")
a = b.pair("Keep 30", keep=30)
app = b.pair("App forever")
odd = b.pair("Odd keep", keep=12)
devs = {d["name"]: d for d in b.state()["devices"]}
exp = devs["Keep 30"]["expiresAt"] - devs["Keep 30"]["addedAt"]
check("keep 30 → expires 30 days after pairing", abs(exp - 30 * 86400) < 5, exp)
check("no keep (the app) → no expiry", devs["App forever"]["expiresAt"] == 0)
check("a value not offered → no expiry", devs["Odd keep"]["expiresAt"] == 0)
check("works before it expires", a.call("GET", "clo/local/terminalSessions?pageSize=1")["status"] == 200)

# make it expire: edit the saved devices and restart the bridge (it loads them at start)
f = b.tmp / "id" / "devices.json"
saved = json.loads(f.read_text())
for d in saved:
    if d["name"] == "Keep 30": d["expiresAt"] = time.time() - 1
f.write_text(json.dumps(saved))
b.stop(); b.start()
a.ws = __import__("websockets.sync.client", fromlist=["connect"]).connect(a.url, max_size=None, ping_timeout=90)
recv(a.ws)
a.send({"id": "x1", "method": "GET", "url": "clo/local/terminalSessions?pageSize=1"})
closed = None
try:
    for _ in range(10): recv(a.ws, 10)
except Exception as e:
    closed = getattr(getattr(e, "rcvd", None), "code", None) or str(e)
check("expired → its socket is closed with 4001", closed == 4001 or "4001" in str(closed), closed)
names = [d["name"] for d in b.state()["devices"]]
check("expired device removed from the Mac", "Keep 30" not in names and "App forever" in names, names)
b.stop(); done()
