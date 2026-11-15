"""A session started in Claude Code's Code Mode can't be resumed with `claude -p`: the turn ends with a clear error
that says where it can be continued (not claude's raw stderr).  ~/.clo2/venv/bin/python -I bridge/tests/code_mode.py"""
import os, pathlib, sys, uuid
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import Bridge, check, done

fake = pathlib.Path("/tmp/clo2-fake-claude-codemode")
fake.write_text("#!/bin/sh\necho \"Error: This session was started in Code Mode, which isn't available because "
                "it doesn't work with -p, so it can't be resumed.\" >&2\nexit 1\n")
fake.chmod(0o755)
b = Bridge(7799, "codemode", extra={"claude": str(fake)})
a = b.pair("Code Mode phone")
a.queue("cm-1", "hello", sid=str(uuid.uuid4()))
st, f = a.wait("cm-1", t=60)
err = f.get("error", {}).get("stringValue", "")
check("Code Mode session → error status", st == "error", st)
check("error explains it (terminal + new session), not claude's raw text", "only a terminal on the Mac" in err and "start a new session" in err, err)
b.stop()
fake.unlink()
done()
