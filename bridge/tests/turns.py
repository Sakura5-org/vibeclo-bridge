"""Phase 2: real Claude turns through the tunnel — new session, a long turn, a message sent meanwhile, a /btw fork
answered while the main turn still runs, next-step suggestions after it.  ~/.clo2/venv/bin/python -I bridge/tests/turns.py"""
import pathlib, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import Bridge, check, done

b = Bridge(7797, "turns")
a = b.pair("Turns phone")
b.post("/api/settings", {"key": "access", "value": "full"})   # these turns use Bash (`sleep`); Standard would refuse it

a.queue("t-ready", "Reply with exactly: READY")
st, f = a.wait("t-ready")
sid = f.get("resultSessionId", {}).get("stringValue", "")
check("new session turn → done with a session id", st == "done" and "READY" in f.get("reply", {}).get("stringValue", "") and sid, (st, f.get("error")))

a.queue("t-long", "Use the Bash tool to run `sleep 25`, then reply with exactly: LONG-DONE", sid=sid)
st, _ = a.wait("t-long", until=("running",), t=60)
check("long turn starts running", st == "running", st)
t0 = time.time()
a.queue("t-btw", "Câu hỏi bên lề (/btw): reply with exactly BTW-OK", sid=sid, fork=True)
st, f = a.wait("t-btw", t=90)
check("/btw fork answered while the main turn still runs",
      st == "done" and "BTW-OK" in f.get("reply", {}).get("stringValue", "") and a.doc("clo/local/terminalInbox/t-long")["status"]["stringValue"] == "running",
      (st, f.get("reply"), f.get("error")))
a.queue("t-more", "Also add the word EXTRA at the end of your reply, then ask me: Bạn muốn mình viết test hay deploy luôn?", sid=sid)
st, f = a.wait("t-long", t=180)
check("long turn finishes", st == "done", (st, f.get("error")))
st2, f2 = a.wait("t-more", t=180)
reply = f.get("reply", {}).get("stringValue", "") + f2.get("reply", {}).get("stringValue", "")
check("message sent mid-turn is handled (injected or queued after)", st2 in ("done",) and "EXTRA" in reply, (st2, reply[-200:]))

sug = []
for _ in range(45):
    s = a.doc(f"clo/local/terminalSessions/{sid}") or {}
    sug = s.get("suggestions", {}).get("arrayValue", {}).get("values", [])
    if sug: break
    time.sleep(2)
check("next-step suggestions written to the session", 1 <= len(sug) <= 3, s.keys() if not sug else "")
check("session transcript mirrored", int(s.get("turnCount", {}).get("integerValue", "0")) >= 4)

# Two messages sent while a turn runs: they wait, then go in together as ONE message (not one turn each)
a.queue("t-long3", "Use the Bash tool to run `sleep 20`, then reply with exactly: LONG3-DONE", sid=sid)
st, _ = a.wait("t-long3", until=("running",), t=60)
time.sleep(3)
a.queue("t-m1", "Also include the word ALPHA in your next reply.", sid=sid)
a.queue("t-m2", "And the word BETA too.", sid=sid)
st, f = a.wait("t-long3", t=180)
st1, f1 = a.wait("t-m1", t=60)
st2, f2 = a.wait("t-m2", t=60)
reply = f.get("reply", {}).get("stringValue", "")
check("messages sent mid-turn → one combined message after the turn", "waiting 2 message(s) → one message" in b.log(), b.log()[-600:])
check("…both answered in that turn (ALPHA + BETA)", st == "done" and "ALPHA" in reply and "BETA" in reply and st1 == st2 == "done",
      (st, st1, st2, reply[-200:]))

# "Stop & send now": the app writes the new message (interrupt) then cancels the running turn → the new one starts at once
a.queue("t-long2", "Use the Bash tool to run `sleep 40`, then reply with exactly: LONG2-DONE", sid=sid)
st, _ = a.wait("t-long2", until=("running",), t=60)
time.sleep(4)
a.queue("t-now", "Reply with exactly: NOW-OK", sid=sid, interrupt=True)
t0 = time.time()
a.call("PATCH", "clo/local/terminalInbox/t-long2?updateMask.fieldPaths=status", {"fields": {"status": {"stringValue": "cancel"}}})
started = None
while time.time() - t0 < 30:
    f = a.doc("clo/local/terminalInbox/t-now") or {}
    if f.get("status", {}).get("stringValue") in ("running", "done"):
        started = time.time() - t0
        break
    time.sleep(0.2)
# ~1.5 s is claude shutting down; the rest is this test's relay round trips (it used to be two 3 s polls on top)
check("stop & send now: new message running within 3.5 s of the stop", started is not None and started < 3.5, started)
st, f = a.wait("t-now", t=120)
check("…and answered", st == "done" and "NOW-OK" in f.get("reply", {}).get("stringValue", ""), (st, f.get("error")))
done()
