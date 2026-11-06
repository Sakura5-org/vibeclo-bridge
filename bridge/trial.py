"""Release trial for one-line installs (~/.clo2/app): automatic rollback of a bad bridge release.

The updater copies the running release into `prev/` and writes `.trial.json` before it swaps a new signed release in.
`start()` runs first thing in clo_bridge.py — standard library only, before any other bridge module is imported, so
it still runs when the rest of a release is broken. Every start of a release on trial counts; a bridge that crashes
and is restarted by launchd/systemd MAX_STARTS times without reaching `ok()` (called once it has run healthy for a
while) gets the previous release back, and the bad release's `seq` goes to `.bad-seq` so the updater skips it. A
later, higher `seq` (the fix) is taken as usual. Git checkouts never have a trial file, so this is a no-op there.
"""
import json
import os
import pathlib

MAX_STARTS = 3
HERE = pathlib.Path(__file__).resolve().parent


def start(app=HERE, log=print):
    """Count this start; on the (MAX_STARTS + 1)th start of a release that never got healthy, roll back.
    → "rolled-back" | "trial" | None (no release on trial)."""
    app = pathlib.Path(app)
    path = app / ".trial.json"
    if not path.exists():
        return None
    try:
        t = json.loads(path.read_text())
    except (OSError, ValueError):
        t = {}
    t["starts"] = int(t.get("starts", 0)) + 1
    if t["starts"] <= MAX_STARTS:
        path.write_text(json.dumps(t))
        return "trial"
    prev = app / "prev"
    restored = 0
    if prev.is_dir():
        for f in prev.iterdir():
            if f.is_file():
                os.replace(f, app / f.name)
                restored += 1
    (app / ".bad-seq").write_text(str(t.get("seq", "")))
    path.unlink()
    log(f"release {t.get('version') or t.get('seq')} failed {MAX_STARTS} starts — restored the previous release "
        f"({restored} files); this release will be skipped")
    return "rolled-back"


def ok(app=HERE):
    """The release on trial ran healthy: keep it (prev/ stays as the manual fallback until the next update)."""
    try:
        (pathlib.Path(app) / ".trial.json").unlink()
    except FileNotFoundError:
        pass


def bad_seq(app=HERE):
    try:
        return int((pathlib.Path(app) / ".bad-seq").read_text().strip() or 0)
    except (OSError, ValueError):
        return 0
