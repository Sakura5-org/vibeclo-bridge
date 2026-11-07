"""Claude Code memory from the phone: list / edit / delete notes (tunnel ops memory.*), and a session "dream" that updates
its project's memory with an undo record. Uses a throwaway project in /tmp (its ~/.claude/projects entry is removed after).
Real turn + real Sonnet call (a few cents).   ~/.clo2/venv/bin/python -I bridge/tests/memory.py"""
import json, pathlib, re, secrets, shutil, subprocess, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import Bridge, check, done

PROJ = pathlib.Path(f"/tmp/clo2-memtest-{secrets.token_hex(3)}").resolve()
PROJ.mkdir(parents=True)
subprocess.run(["git", "init", "-q", str(PROJ)], check=True)
KEY = re.sub(r"[^A-Za-z0-9]", "-", str(PROJ))
CLAUDE_DIR = pathlib.Path.home() / ".claude/projects" / KEY
MEM = CLAUDE_DIR / "memory"


def cleanup():
    shutil.rmtree(CLAUDE_DIR, ignore_errors=True); shutil.rmtree(PROJ, ignore_errors=True)
    shutil.rmtree(pathlib.Path("/private") / str(PROJ).lstrip("/"), ignore_errors=True)


import atexit; atexit.register(cleanup)
b = Bridge(7800, "memory")
a = b.pair("Memory phone")

# ---- notes by hand
note = "---\nname: deploy\ndescription: deploy with make ship\nmetadata:\n  type: project\n---\n\nUse `make ship`.\n"
check("write a note", a.op("memory.write", key=KEY, name="deploy.md", text=note)["status"] == 200 and (MEM / "deploy.md").read_text() == note)
a.op("memory.write", key=KEY, name="MEMORY.md", text="- [Deploy](deploy.md) — make ship\n- [Other](other.md) — x\n")
a.op("memory.write", key=KEY, name="other.md", text="other")
r = a.op("memory.projects")["body"]
mine = next((p for p in r["projects"] if p["key"] == KEY), None)
check("projects list it (cwd, file count, index size)", mine and mine["files"] == 3 and mine["indexLines"] == 2 and r["limits"]["lines"] == 200, mine)
fs = a.op("memory.files", key=KEY)["body"]["files"]; print("   files:", [(f["name"], f.get("description")) for f in fs])
check("files: MEMORY.md first, frontmatter read", fs[0]["name"] == "MEMORY.md" and next(f for f in fs if f["name"] == "deploy.md")["description"] == "deploy with make ship")
check("delete drops the note and its index line", a.op("memory.delete", key=KEY, name="other.md")["status"] == 200
      and not (MEM / "other.md").exists() and "other.md" not in (MEM / "MEMORY.md").read_text() and "deploy.md" in (MEM / "MEMORY.md").read_text())
check("paths outside a memory folder refused",
      a.op("memory.write", key=KEY, name="../x.md", text="x")["status"] == 400
      and a.op("memory.write", key="../../etc", name="x.md", text="x")["status"] == 400
      and a.op("memory.files", key="/etc")["status"] == 400
      and a.op("memory.write", key=KEY, name="x.sh", text="x")["status"] == 400)

# ---- a session, then its dream
rid = secrets.token_hex(8)
a.queue(rid, "Context for this project: the database migration command is `./db migrate --safe`; plain `./db migrate` wiped "
        "the staging data last week, so never use it. Also I prefer short answers. Use no tools this turn; just reply: noted.", cwd=str(PROJ), profile="claude-yolo")
st, f = a.wait(rid, t=240)
sid = f.get("resultSessionId", {}).get("stringValue", "") or f.get("sessionId", {}).get("stringValue", "")
check("a turn in the project", st == "done" and sid, f)
did = secrets.token_hex(8)
a.queue(did, "", sid=sid, kind="dream")
st, f = a.wait(did, t=600)
reply = f.get("reply", {}).get("stringValue", "")
print("   summary:", reply)
dreams = a.op("memory.dreams", sessionId=sid)["body"]["dreams"]
d = dreams[0] if dreams else {}
check("dream done, record kept for the session", st == "done" and d.get("id") == did and d.get("key") == KEY, f.get("error", f))
text = "\n".join((MEM / x).read_text() for x in [p.name for p in MEM.glob("*.md")])
print("   changes:", [(c["file"], c["action"]) for c in d.get("changes", [])])
pre = {p.name: p.read_text() for p in MEM.glob("*.md")}
check("the lesson is in memory now (by the turn's own auto memory or the dream), listed in the index",
      "--safe" in text and all(f"({n})" in pre.get("MEMORY.md", "") for n in pre if n != "MEMORY.md"), pre.get("MEMORY.md"))
check("the old note is still there or merged, not lost", "make ship" in text)
r = a.op("memory.undo", dream=did)
after = {p.name: p.read_text() for p in MEM.glob("*.md")}
before = {c["file"]: c["before"] for c in d.get("changes", [])}
expect = {n: before.get(n, t) for n, t in pre.items() if before.get(n, t) is not None}
expect.update({n: b for n, b in before.items() if b is not None})
check("undo puts every changed note back as it was", r["status"] == 200 and not r["body"]["skipped"] and after == expect, (r, list(after)))
check("…and is recorded", a.op("memory.dreams", sessionId=sid)["body"]["dreams"][0]["undone"] is True)
check("no secret value / log of note text", "--safe" not in b.log())
done()
