"""Phase 3: files through the tunnel (chunked op "upload") → Claude reads them.  ~/.clo2/venv/bin/python -I bridge/tests/files.py"""
import base64, os, pathlib, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import Bridge, check, done

b = Bridge(7796, "files")
a = b.pair("Files phone")
b.post("/api/settings", {"key": "access", "value": "full"})   # Claude may use Bash on the upload (Standard would refuse it)
UP = pathlib.Path.home() / ".clo2-uploads"

data = os.urandom(1_500_000)
size, rid = 384 * 1024, "files-" + os.urandom(3).hex()
chunks = [data[i:i + size] for i in range(0, len(data), size)]
t0 = time.time()
for i, c in enumerate(chunks):
    r = a.op("upload", rid=rid, name="../../evil/báo cáo.bin", index=i, total=len(chunks), data=base64.b64encode(c).decode(), t=120)
dest = UP / rid / "báo cáo.bin"
check(f"1.5 MB upload in {len(chunks)} chunks ({time.time()-t0:.0f} s)", r.get("status") == 200 and dest.exists() and dest.read_bytes() == data, r)
check("path traversal in the name is stripped", not (UP / "evil").exists() and dest.parent == UP / rid)
check("file is private (0600)", oct(dest.stat().st_mode & 0o777) == "0o600")
check("bad request id refused", a.op("upload", rid="../x", name="a", index=0, total=1, data="")["status"] == 400)
check("chunk out of order refused", a.op("upload", rid=rid + "b", name="z", index=3, total=5, data="AA==")["status"] == 409)

# resending a chunk (its reply was lost) must not duplicate data; a gap is still refused
small = os.urandom(30_000); cs = 12_000; parts = [small[i:i + cs] for i in range(0, len(small), cs)]
rr = "resend-" + os.urandom(3).hex()
up = lambda i, off=None: a.op("upload", rid=rr, name="r.bin", index=i, total=len(parts), offset=i * cs if off is None else off,
                              data=base64.b64encode(parts[i]).decode())
s0, s1, s1b = up(0)["status"], up(1)["status"], up(1)["status"]
check("resent middle chunk is accepted once (no duplicate bytes)", (s0, s1, s1b) == (200, 200, 200) and
      (UP / rr / "r.bin.part").stat().st_size == 2 * cs)
last, again = up(2), up(2)
check("resent last chunk after the file was finished → same path, file intact",
      last["status"] == again["status"] == 200 and again["body"].get("path") == last["body"].get("path") and (UP / rr / "r.bin").read_bytes() == small, again)
r2 = "gap-" + os.urandom(3).hex()
a.op("upload", rid=r2, name="g.bin", index=0, total=3, offset=0, data=base64.b64encode(parts[0]).decode())
check("chunk with the wrong offset (gap) → 409", a.op("upload", rid=r2, name="g.bin", index=2, total=3, offset=2 * cs,
      data=base64.b64encode(parts[2]).decode())["status"] == 409)
os.system(f"rm -rf {UP / rr} {UP / r2}")

a.queue(rid, "Use Bash: print only the byte count of the attached file (wc -c < file). Reply with just the number.", fileNames=["../../evil/báo cáo.bin"])
st, f = a.wait(rid, t=240)
check("Claude reads the uploaded file", st == "done" and str(len(data)) in f.get("reply", {}).get("stringValue", ""), (st, f.get("reply"), f.get("error")))
# A turn that ends before claude starts (folder not found) must not stay "running": it blocked restarts (updates) for good
bad = "nodir-" + os.urandom(3).hex()
a.queue(bad, "hi", cwd="/nonexistent/clo2-test")
st, f = a.wait(bad, t=30)
check("missing folder → error", st == "error" and "Folder not found" in f.get("error", {}).get("stringValue", ""), (st, f))
time.sleep(1)
(b.tmp / "id" / "restart").touch()
try:
    b.proc.wait(30); gone = True
except Exception:
    gone = False
check("…and a restart request still goes through (nothing left 'running')", gone)
done()
