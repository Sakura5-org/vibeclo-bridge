"""Phase 5: artifacts + secrets vault, all on the Mac.  ~/.clo2/venv/bin/python -I bridge/tests/extras.py"""
import os, pathlib, subprocess, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import Bridge, check, done, HERE

b = Bridge(7795, "extras")
a = b.pair("Extras phone")
env = {**os.environ, "CLO2_DIR": str(b.tmp / "id"), "CLO2_STORE": str(b.tmp / "store.db")}
tool = lambda *args, **kw: subprocess.run([sys.executable, "-I", str(HERE / "clo2_tools.py"), *args], env=env, capture_output=True, text=True, **kw)

page = b.tmp / "chart.html"
page.write_text("<!doctype html><title>t</title><h1>Xin chào artifact</h1>")
r = tool("artifact", str(page), "--title", "Biểu đồ")
link = r.stdout.strip()
aid = link.rsplit("/", 1)[-1]
check("clo2-artifact prints a clo2:// link", link.startswith("clo2://artifact/") and r.returncode == 0, r.stderr)
got = a.op("artifact", aid=aid)
check("phone loads the artifact through the tunnel", got.get("status") == 200 and "Xin chào artifact" in got["body"]["html"])
lib = a.doc(f"clo/local/artifacts/{aid}")
check("artifact listed in the library", lib and lib["title"]["stringValue"] == "Biểu đồ" and lib["url"]["stringValue"] == link)
r2 = tool("artifact", str(page), "--title", "Biểu đồ v2", "--id", aid)
check("--id updates the same artifact", r2.stdout.strip() == link and a.doc(f"clo/local/artifacts/{aid}")["title"]["stringValue"] == "Biểu đồ v2")
check("unknown / malformed artifact id → 404", a.op("artifact", aid="../../etc")["status"] == 404 and a.op("artifact", aid="nope123456")["status"] == 404)

VALUE = "s3cr3t-" + os.urandom(6).hex()
check("phone saves a secret", a.op("secret.set", name="TEST_TOKEN", value=VALUE, label="token thử")["status"] == 200)
doc = a.doc("clo/local/secrets/TEST_TOKEN")
check("store keeps the name/label only", doc and doc["label"]["stringValue"] == "token thử" and VALUE not in str(doc))
blob = b"".join(p.read_bytes() for p in b.tmp.rglob("*") if p.is_file() and p.suffix != ".bin" and p.name != "leak.txt")
check("value appears in no store/log/other file on disk", VALUE.encode() not in blob)
check("vault file is 0600", oct((b.tmp / "id" / "secrets.bin").stat().st_mode & 0o777) == "0o600")
check("clo2-secret get", tool("secret", "get", "TEST_TOKEN").stdout == VALUE)
check("clo2-secret list shows names, not values", "TEST_TOKEN" in tool("secret", "list").stdout and VALUE not in tool("secret", "list").stdout)
r = tool("secret", "run", "TEST_TOKEN", "--", "sh", "-c", 'printf %s "$TEST_TOKEN" | wc -c')
check("clo2-secret run passes it as env", r.stdout.strip() == str(len(VALUE)), r.stderr)
check("clo2-secret ask prints the card link", "clo2-secret://ask?name=GH_TOKEN" in tool("secret", "ask", "GH_TOKEN", "--label", "GitHub").stdout)
check("reveal returns it to the paired phone", a.op("secret.reveal", name="TEST_TOKEN")["body"].get("value") == VALUE)
check("bad name refused", a.op("secret.set", name="bad name!", value="x")["status"] == 400)

leak = b.tmp / "leak.txt"
leak.write_text(VALUE)   # the prompt names only the file: the value comes out of a command, as a real leak would
a.queue("x-leak", f"Use the Bash tool to run exactly: cat {leak}  — then reply with the command output verbatim.")
st, f = a.wait("x-leak")
reply = f.get("reply", {}).get("stringValue", "")
check("a turn that prints the secret: reply scrubbed", st == "done" and VALUE not in reply and "‹secret TEST_TOKEN›" in reply, (st, reply[-200:]))
time.sleep(25)
sess = [p for p in (b.tmp / "store.db", b.tmp / "store.db-wal") if p.exists()]
check("…and the mirrored transcript never holds it", all(VALUE.encode() not in p.read_bytes() for p in sess))
check("delete removes it", a.op("secret.delete", name="TEST_TOKEN")["status"] == 200 and a.op("secret.reveal", name="TEST_TOKEN")["status"] == 404
      and a.doc("clo/local/secrets/TEST_TOKEN") is None)
done()
