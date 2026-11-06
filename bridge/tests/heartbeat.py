"""CLO 2 relay heartbeat: a link whose relay stops routing frames back (the "deaf" link) is dropped and reconnected;
a relay that never answers echo (older relay) is left alone. Fake relay on localhost, short timings."""
import json, os, pathlib, shutil, sys, threading, time
ROOT = pathlib.Path("/tmp/clo2-heartbeat"); shutil.rmtree(ROOT, ignore_errors=True); ROOT.mkdir()
os.environ["CLO2_DIR"] = str(ROOT)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import check, done
from websockets.sync.server import serve
import tunnel

state = {"answer": True, "conns": 0, "echoes": 0}


def relay(ws):
    state["conns"] += 1
    for raw in ws:
        m = json.loads(raw)
        if m.get("op") == "echo":
            state["echoes"] += 1
            if state["answer"]:
                ws.send(json.dumps({"op": "echo", "n": m["n"]}))


def start(port):
    srv = serve(relay, "127.0.0.1", port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


tunnel.Tunnel.ECHO_EVERY, tunnel.Tunnel.ECHO_WAIT = 1, 1.5
logs = []
srv = start(7795)
t = tunnel.Tunnel("ws://127.0.0.1:7795", "HB Mac", handle=lambda *a: None, log=lambda *a: logs.append(" ".join(map(str, a))))
threading.Thread(target=t.run, daemon=True).start()
time.sleep(4)
check("answering relay: link stays up, echo confirmed", state["conns"] == 1 and t.online and t.echo_ok, (state, logs))

state["answer"] = False   # relay keeps the socket open but frames no longer come back
for _ in range(20):
    if state["conns"] >= 2: break
    time.sleep(0.5)
check("deaf relay → bridge drops the link and reconnects", state["conns"] >= 2, (state, logs))
check("logged why", any("tunnel deaf" in l for l in logs), logs)

# a fresh link to a relay that never answers echo (older relay): no flapping
before = state["conns"]
time.sleep(6)
check("relay without echo support → no reconnect loop", state["conns"] == before and t.online, (state["conns"], before))

state["answer"] = True
time.sleep(3)
check("echo answered again on the same link", t.echo_ok and state["conns"] == before, state)
srv.shutdown()
done()
