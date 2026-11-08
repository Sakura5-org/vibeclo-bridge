"""Codex (OpenAI Codex CLI) sessions from the app: new turn, resume, the Standard sandbox, Full access, stop, and the
session mirrored with agent "codex". Real Codex turns (needs `codex` logged in; CODEX_TEST_HOME=… for another CODEX_HOME).
  ~/.clo2/venv/bin/python -I bridge/tests/codex.py"""
import os, pathlib, secrets, shutil, subprocess, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import Bridge, check, done

extra = {"codex_home": os.environ["CODEX_TEST_HOME"]} if os.environ.get("CODEX_TEST_HOME") else {}   # default: ~/.codex
PROJ = pathlib.Path(f"/tmp/clo2-cx-{secrets.token_hex(3)}").resolve(); PROJ.mkdir()
subprocess.run(["git", "init", "-q", str(PROJ)])
OUT = pathlib.Path.home() / f"clo2-cx-outside-{secrets.token_hex(3)}.txt"
YOLO = pathlib.Path.home() / f"clo2-cx-yolo-{secrets.token_hex(3)}.txt"
import atexit
atexit.register(lambda: [shutil.rmtree(PROJ, ignore_errors=True), OUT.unlink(missing_ok=True), YOLO.unlink(missing_ok=True)])

b = Bridge(7801, "codex", extra=extra)
a = b.pair("Codex phone")


def turn(text, sid="", profile="codex", t=300):
    rid = secrets.token_hex(8)
    a.queue(rid, text, sid=sid, cwd=str(PROJ), profile=profile)
    st, f = a.wait(rid, t=t)
    g = lambda k: f.get(k, {}).get("stringValue", "")
    return st, g("reply"), g("resultSessionId"), g("error"), rid


st, reply, sid, err, _ = turn("Create a file hello.txt in the current folder containing the word hi, then reply with exactly: done")
print("   reply:", reply[-300:])
check("new Codex session: turn done, file made in the project", st == "done" and sid and (PROJ / "hello.txt").exists(), (st, err))
check("…its commands show as 🔧 lines, the answer after them", "🔧" in reply and reply.strip().endswith("done"), reply)
for _ in range(20):
    doc = a.doc(f"clo/local/terminalSessions/{sid}")
    if doc: break
    time.sleep(1)
doc = doc or {}
g = lambda k: (doc.get(k) or {}).get("stringValue", "")
check("session listed in the app as a Codex session (title, folder)", g("agent") == "codex" and "hello.txt" in g("title")
      and g("cwd").endswith(PROJ.name), {k: g(k) for k in ("agent", "title", "cwd")})
tr = [(x["mapValue"]["fields"]["role"]["stringValue"]) for x in (doc.get("transcript") or {}).get("arrayValue", {}).get("values", [])]
check("…its transcript: your message, then Codex", tr[:2] == ["user", "assistant"], tr)

st, reply, sid2, err, _ = turn("What is the name of the file you created? Reply with just the file name.", sid=sid, profile="claude-yolo")
check("resume: same session, remembers it (even with Claude picked in the app)", st == "done" and sid2 == sid and "hello.txt" in reply, (st, reply[-200:], err))

st, reply, _, err, _ = turn(f"Write the text x into the file {OUT} (outside this folder) using a shell command, then reply ok.")
check("Standard: can't write outside the project folder", not OUT.exists(), (st, reply[-300:]))

st, reply, sid3, err, _ = turn(f"Write the text x into the file {YOLO} using a shell command, then reply ok.", profile="codex-yolo")
check("Full access: it can", st == "done" and YOLO.exists(), (st, reply[-300:], err))

rid = secrets.token_hex(8)
a.queue(rid, "Run the shell command `sleep 120`, then reply ok.", cwd=str(PROJ), profile="codex")
for _ in range(60):
    if "codex session" in b.log() and a.doc(f"clo/local/terminalInbox/{rid}").get("status", {}).get("stringValue") == "running": break
    time.sleep(1)
time.sleep(8)
a.call("PATCH", f"clo/local/terminalInbox/{rid}?updateMask.fieldPaths=status", {"fields": {"status": {"stringValue": "cancel"}}})
st, f = a.wait(rid, t=60)
check("stop works", st == "cancelled", st)
machine = subprocess.run(["hostname", "-s"], capture_output=True, text=True).stdout.strip()
feats = str((a.doc(f"clo/local/bridge/{machine}") or {}).get("features", ""))
check("the bridge lists Codex in its features", "'codex'" in feats, feats[:300])
done()
