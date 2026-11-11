"""A turn that answered but left a background task running: it ends "done" (not "error"), claude stays up for the task
(input kept open, no idle kill), a new message goes into that same claude, and the session lists the task in bgTasks.  ~/.clo2/venv/bin/python -I bridge/tests/idle.py"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Bridge, check, done

b = Bridge(7797, "idle", extra={"idle_timeout": 20, "linger_grace": 5})
a = b.pair("Idle phone")
b.post("/api/settings", {"key": "access", "value": "full"})
a.queue("idle-1", "Start this with the Bash tool and run_in_background: true:  sleep 300 ; then, without waiting for it, "
        "reply with exactly: STARTED")
st, f = a.wait("idle-1", t=200)
reply = f.get("reply", {}).get("stringValue", "")
check("answered, then idle with a background task → done (not error)", st == "done" and "STARTED" in reply,
      (st, f.get("error"), reply[-200:]))
check("…done as soon as it answered (not when the background task ends)", "idle kill" not in b.log(), b.log()[-800:])
sid = f.get("resultSessionId", {}).get("stringValue", "")
time.sleep(3)
sd = a.doc(f"clo/local/terminalSessions/{sid}") or {}
check("…and its session shows as not working any more (claude still up for the background task)",
      sd.get("state", {}).get("stringValue") == "idle" and "idle kill" not in b.log(), sd.get("state"))
t0 = time.time()
a.queue("idle-2", "Reply with exactly: NEXT", sid=sid)
st2, f2 = a.wait("idle-2", t=120)
check("a new message runs right away (the lingering background wait is stopped)",
      st2 == "done" and "NEXT" in f2.get("reply", {}).get("stringValue", "") and time.time() - t0 < 60,
      (st2, time.time() - t0, f2.get("reply", {}).get("stringValue", "")[-300:]))
check("…and the first turn stays done", a.wait("idle-1")[0] == "done")
a.queue("idle-3", "Start this with the Bash tool and run_in_background: true:  sleep 300 ; then, without waiting for it, "
        "reply with exactly: AGAIN", sid=sid)
st3, _ = a.wait("idle-3", t=200)
time.sleep(45)   # idle_timeout 20 s + watchdog tick
check("background task still running → no idle kill, turn stays done", a.wait("idle-3")[0] == "done" and "idle kill" not in b.log(),
      b.log()[-600:])
sd = a.doc(f"clo/local/terminalSessions/{sid}") or {}
bg = [v.get("stringValue") for v in sd.get("bgTasks", {}).get("arrayValue", {}).get("values", [])]
check("…and the session lists it under bgTasks (app: 'Running in background')", len(bg) >= 1, sd.get("bgTasks"))
done()
