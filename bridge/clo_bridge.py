#!/usr/bin/env python3
"""CLO 2 bridge — lets the CLO 2 app (phone / browser) see and continue this Mac's Claude Code sessions.

Runs on the Mac (launchd). No cloud account: everything is stored on this Mac (localstore.py, SQLite with the
Firestore REST shape CLO 1 used), and paired devices reach it through an end-to-end encrypted relay (tunnel.py).
Control page (pair / revoke devices): http://127.0.0.1:7788

  clo/{uid}/terminalSessions/{sessionId}  title, cwd, transcript (last N turns), editedAt, machine
  clo/{uid}/terminalInbox/{requestId}     app → bridge: text, sessionId|"", cwd, profile, status…
  clo/{uid}/bridge/{machine}              heartbeat

A request runs `claude -p --resume <sessionId>` in the session's cwd (same session id, so
`claude --resume <id>` in a terminal continues it), streaming the reply back. Its stdin stays open
(stream-json input) until the first result: messages sent to that session meanwhile are written
straight in, and Claude reads them after its next tool call — like typing during a turn in a terminal.

  python3 -I clo_bridge.py            # run loop
  python3 -I clo_bridge.py --once     # one session scan + inbox pass (debug)
"""
import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))   # -I (isolated) leaves the script dir off sys.path
import trial as _trial  # noqa: E402  stdlib only, first: a new release that keeps crashing is rolled back here
_TRIAL = _trial.start()
import localstore, tunnel as tunnel_mod, pairpage, clo2_tools, updater  # noqa: E402
import base64, hashlib, uuid, tempfile, datetime as dt, json, os, re, shutil, pathlib, platform, signal, socket, subprocess, sys, threading, time, urllib.error, urllib.parse, urllib.request

HOME = pathlib.Path.home()
CONFIG = {
    "project": "local",
    "relay": "wss://clo2-relay.synthvale.com",
    "store": str(HOME / ".clo2/store.db"),
    "port": 7788,
    "lan_port": 7789,          # direct path for phones on the same network (0 = relay only)
    "ts_web": 7779,            # web app over Tailscale HTTPS (tailscale serve :8443 → 127.0.0.1:ts_web; 0 = off)
    "self_update": False,      # git checkouts: fast-forward from the upstream (owner's Macs; off by default)
    "auto_update": True,       # one-line installs (~/.clo2/app): signed releases from clo.sakura5.app
    "update_every": 3600,
    "claude": None,  # auto: ~/.local/bin, /opt/homebrew/bin, PATH
    "label": None,   # friendly machine name shown in the app (default: hostname)
    "accept_untargeted": False,
    "compact_at": 500000,          # context tokens; above this the bridge runs /compact before the next app turn
    "subagent_model": "claude-sonnet-5-5",  # sub-agents spawned by app turns run on the cheaper model
    "browser_headless": True,  # Playwright MCP for requests sent with the 🌐 toggle
    "browser_profile": str(HOME / ".clo-browser-profile"),  # persistent: logins survive between turns  # also run requests with no "machine" field (pre-multi-Mac app builds)
    "machine": platform.node().split(".")[0] or "mac",
    "max_sessions": 40,
    "max_turns": 300,
    "max_text": 20000,
    "scan_every": 20,
    "poll_every": 3,
    "job_timeout": 3600,
    "linger_grace": 60,   # s after claude answered but stays up (a background task): then the turn counts as done
    "idle_timeout": 1200,  # no stream event this long (stalled API call) → kill the turn so the session queue moves on
}
cfg_file = pathlib.Path(__file__).with_name("config.json")
if cfg_file.exists():
    CONFIG.update(json.loads(cfg_file.read_text()))
if os.environ.get("CLO_CONFIG"):  # tests / overrides
    CONFIG.update(json.loads(os.environ["CLO_CONFIG"]))

CONFIG["claude"] = CONFIG["claude"] or shutil.which(
    "claude", path=f"{HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", "")) or "claude"

BASE = "local/documents"
DOC_PREFIX = f"projects/{CONFIG['project']}/databases/(default)/documents"
PROJECTS = HOME / ".claude/projects"
RESTART = tunnel_mod.DIR / "restart"  # restart.sh: take no new turns, exit once running ones finish (launchd starts the new code)
RESTART_HOLD = 120  # s: after this, a long turn in one session no longer blocks the others; restart waits for an idle moment


def restart_holding():
    """New turns wait for the restarted bridge — but only briefly, so one long turn can't stall every session."""
    try:
        return time.time() - RESTART.stat().st_mtime < RESTART_HOLD
    except OSError:
        return False
STATE = tunnel_mod.DIR  # open/<sid>: pid of a terminal opened by clo-resume · busy/<sid>: bridge is running a turn


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (OSError, ValueError):
        return False


def open_in_terminal(sid):
    """True while the session is open in an interactive terminal: held by `clo-resume` (stale locks are removed), or a
    plain `claude` started in a terminal that is writing this session. A turn from the app then waits (or "run now")
    instead of a second claude writing the same transcript alongside it."""
    f = STATE / "open" / sid
    try:
        pid = int(f.read_text().strip() or 0)
    except (OSError, ValueError):
        pid = 0
    if pid and pid_alive(pid):
        return True
    if pid:
        f.unlink(missing_ok=True)
    return sid in terminal_sids()


_tty_sids = {"at": 0.0, "sids": frozenset()}


def wrote_from_terminal(sid, since):
    """The transcript has an interactive ("entrypoint":"cli") line written after `since`. claude_procs() matches a plain
    `claude` to the newest transcript in its folder — that may be a session the app drives there; only a line the
    terminal itself wrote proves the terminal holds it."""
    t = next(PROJECTS.glob(f"*/{sid}.jsonl"), None)
    if not t:
        return False
    try:
        with open(t, "rb") as fh:
            fh.seek(max(0, t.stat().st_size - 512 * 1024))
            tail = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return False
    for line in reversed(tail):
        if '"entrypoint":"cli"' not in line:
            continue
        try:
            at = dt.datetime.fromisoformat(json.loads(line).get("timestamp", "").replace("Z", "+00:00")).timestamp()
        except (ValueError, AttributeError):
            continue
        return at >= since - 5
    return False


def terminal_sids(max_age=5):
    """Session ids of `claude` processes running in a terminal (tty), not CLO turns. Cached a few seconds: ps + lsof."""
    if time.time() - _tty_sids["at"] > max_age:
        try:
            held = set()
            for p in claude_procs():
                if p["source"] != "terminal":
                    continue
                if p["sid"] and {"--resume", "-r", "--session-id"} & set(p["args"].split()):
                    cands = [p["sid"]]
                else:   # plain `claude` (it may have /resume'd another one since): its folder's transcripts written since it started
                    d = PROJECTS / re.sub(r"[^A-Za-z0-9]", "-", p["cwd"]) if p["cwd"] else None
                    files = sorted(d.glob("*.jsonl"), key=lambda x: -x.stat().st_mtime) if d and d.is_dir() else []
                    cands = [x.stem for x in files[:20] if x.stat().st_mtime >= p["started"] - 5]
                sid = next((c for c in cands if wrote_from_terminal(c, p["started"])), "")
                if sid:
                    held.add(sid)
            sids = frozenset(held)
        except Exception as e:
            log("terminal scan failed:", type(e).__name__, str(e)[:200])
            sids = _tty_sids["sids"]
        _tty_sids.update(at=time.time(), sids=sids)
    return _tty_sids["sids"]


def log(*a):
    print(dt.datetime.now().strftime("%H:%M:%S"), *a, flush=True)


def api(method, url, body=None):
    """Firestore-REST-shaped call, answered by the local store on this Mac."""
    code, out = localstore.handle(method, url, body)
    if code == 404:
        return None
    if code >= 400:
        raise RuntimeError(f"{method} {url.split('/documents')[-1]} → {code} {out}")
    return out


# ---------------------------------------------------------------- Firestore value encoding
def enc(v):
    if v is None: return {"nullValue": None}
    if isinstance(v, bool): return {"booleanValue": v}
    if isinstance(v, int): return {"integerValue": str(v)}
    if isinstance(v, float): return {"doubleValue": v}
    if isinstance(v, dt.datetime): return {"timestampValue": v.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")}
    if isinstance(v, list): return {"arrayValue": {"values": [enc(x) for x in v]}}
    if isinstance(v, dict): return {"mapValue": {"fields": {k: enc(x) for k, x in v.items()}}}
    return {"stringValue": str(v)}


def dec(v):
    (kind, x), = v.items()
    if kind == "integerValue": return int(x)
    if kind == "arrayValue": return [dec(i) for i in x.get("values", [])]
    if kind == "mapValue": return {k: dec(i) for k, i in x.get("fields", {}).items()}
    if kind == "nullValue": return None
    return x


def fields(d): return {k: enc(v) for k, v in d.items()}
def unfields(doc): return {k: dec(v) for k, v in (doc or {}).get("fields", {}).items()}
def now(): return dt.datetime.now(dt.timezone.utc)


def set_doc(path, data, merge_keys=None):
    url = f"{BASE}/{path}"
    if merge_keys:
        url += "?" + "&".join(f"updateMask.fieldPaths={k}" for k in merge_keys)
    return api("PATCH", url, {"fields": fields(data)})


# ---------------------------------------------------------------- push (through the relay, which holds the APNs key)
_live_sent = {}  # session id → (phase, time) last pushed


def devices(uid):
    r = api("GET", f"{BASE}/clo/{uid}/devices?pageSize=50") or {}
    return [unfields(d) for d in r.get("documents", [])]


_tunnel = {"t": None}   # the running Tunnel: pushes go out through the relay (it holds the APNs key)


def notify(uid, kind, sid="", title="", text=""):
    """Alert on every paired device that registered for push. The relay writes a fixed text ("Claude is done"); the
    session title and a short summary of the turn travel end-to-end encrypted for that one phone (`enc`, opened by the
    app's Notification Service Extension) — Apple and the relay never see them. Phones whose doc has no `pub` yet
    (registered before this) or an app without the extension show the fixed text."""
    tun = _tunnel["t"]
    if not tun:
        return
    for d in devices(uid):
        if d.get("token"):
            msg = {"op": "push", "token": d["token"], "env": d.get("env") or "production", "kind": kind, "sid": sid}
            if d.get("pub") and (title or text):
                try:
                    msg["enc"] = tunnel_mod.seal_push(tun.key_for(d["pub"]), {"t": title[:120], "b": text[:240]})
                except Exception as e:
                    log("push: can't encrypt for", d["token"][:8], e)
            log("push", kind, d["token"][:8], "+summary" if "enc" in msg else "(fixed text)")
            tun.ctl(msg)


def _plain(s):
    """Markdown → plain text for a notification line."""
    s = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"(\*\*|__|`)", "", s)
    s = re.sub(r"^\s*(?:[-*+]|\d+\.|#+)\s+", "", s)
    return re.sub(r"\s+", " ", s).strip()


def push_summary(reply):
    """The notification text for a finished turn: Claude's closing `⤷ …` line (NOTIFY_HINT), else the first prose
    paragraph of the reply (no tool lines, code, tables or quotes), cut to fit."""
    lines = [l.rstrip() for l in (reply or "").splitlines()]
    for l in reversed(lines):
        if l.lstrip().startswith("⤷"):
            out = _plain(l.lstrip().lstrip("⤷"))
            if out:
                return out if len(out) <= 220 else out[:219].rsplit(" ", 1)[0] + "…"
    para, code = [], False
    for l in lines:
        t = l.strip()
        if t.startswith("```"):
            code = not code
            continue
        if code or t.startswith(("🔧", "|", ">", "clo2://", "🖼")):
            if para:
                break
            continue
        if not t:
            if para:
                break
            continue
        para.append(t)
    out = _plain(" ".join(para))
    return out if len(out) <= 220 else out[:219].rsplit(" ", 1)[0] + "…"


def live_update(uid, sid, title, activity, working, force=False):
    """Live Activity for this session: only its phase (working / done) — the text on the lock screen is the relay's."""
    tun = _tunnel["t"]
    if not tun:
        return
    phase = "working" if working else ("error" if activity == "error" else "done")
    last = _live_sent.get(sid)
    if not force and last and last[0] == phase:
        return
    doc = api("GET", f"{BASE}/clo/{uid}/liveActivities/{sid}")
    if not doc:
        return
    la = unfields(doc)
    _live_sent[sid] = (phase, time.time())
    tun.ctl({"op": "live", "token": la.get("token"), "env": la.get("env") or "production", "phase": phase})
    if not working:
        api("DELETE", f"{BASE}/clo/{uid}/liveActivities/{sid}")


def on_push_result(m):
    """Relay's answer from APNs: forget tokens Apple says are dead."""
    if m.get("reason") in ("BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic", "ExpiredToken"):
        tok = m.get("token") or ""
        try:
            api("DELETE", f"{BASE}/clo/local/devices/{tok}")
            for d in (api("GET", f"{BASE}/clo/local/liveActivities?pageSize=300") or {}).get("documents", []):
                if unfields(d).get("token") == tok:
                    api("DELETE", f"{BASE}/{d['name'].split('/documents/', 1)[1]}")
        except Exception as e:
            log("push cleanup failed", e)
        log("push: dropped a dead device token", m.get("reason"))
    elif m.get("status") not in (200, None):
        log("push failed", m.get("status"), m.get("reason", ""))


# ---------------------------------------------------------------- uid of the allow-listed account
def lookup_uid():
    return "local"   # one owner per Mac: whoever paired their devices with it


ARTIFACT_HINT = ("The user reads this chat in the Vibeclo phone app/web. When a visual or interactive result helps (report, chart, "
                 "dashboard, mockup, slides, mini app), write one self-contained HTML file (inline CSS/JS; CDN libraries ok) and run "
                 "`clo2-artifact FILE.html --title \"…\"`; put the printed clo2://artifact/… link on its own line in your reply — the app "
                 "shows it as a live preview. To update, rerun with `--id <same id>`. This command IS your artifact tool: when the user "
                 "asks to show or open an HTML page here — including an existing .html file on this Mac — run it on that file and reply "
                 "with the link. Artifacts stay on this Mac; the app loads them through its encrypted connection. A page can call an "
                 "API with a key from the Vibeclo vault without the key ever being in the page: `await clo2.fetch(\"https://api…\", "
                 "{method, headers, body, secret: \"NAME\"})` sends `Authorization: Bearer <NAME>`; or put `{{secret:NAME}}` where "
                 "the key goes (header, URL, body string). The Mac adds the key and makes the call (https only, no redirects); it "
                 "returns a normal Response. The user approves each page × secret × host once on the phone.")

NOTIFY_HINT = (" End every reply with one last line `⤷ <summary>`: at most ~120 characters, in the user's language — what you "
               "did and, if anything, what the user needs to do next. Their phone shows that line as the notification.")

SECRET_HINT = (" Secrets: the user keeps passwords/API keys/tokens in the Vibeclo vault on this Mac. `clo2-secret list` shows the "
               "names (never values). Use one without printing it: `X=$(clo2-secret get NAME) cmd`, `eval \"$(clo2-secret env NAME)\"`, or "
               "`clo2-secret run NAME -- cmd`; never echo, log, commit or write a value to a file you show. If you need one that isn't "
               "stored, never ask the user to paste it in chat: put `clo2-secret ask NAME --label \"what it is\"`'s printed line in your "
               "reply on its own line — the app shows a secure input card — then end the turn (or `clo2-secret wait NAME`).")


# ---------------------------------------------------------------- Claude Code transcripts
SECRET = re.compile(r"(AIza[0-9A-Za-z_\-]{30,}|AQ\.[0-9A-Za-z_\-]{30,}|sk-[A-Za-z0-9_\-]{20,}|GOCSPX-[0-9A-Za-z_\-]{20,}"
                    r"|ghp_[0-9A-Za-z]{30,}|xox[abpr]-[0-9A-Za-z\-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----)")


ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
TAG = lambda name: re.compile(rf"<{name}>(.*?)</{name}>", re.S)


def slash_turn(txt):
    """Claude Code stores slash commands as tagged user lines → readable turns (None = drop the line)."""
    if "<local-command-caveat>" in txt or "<system-reminder>" in txt and "<command-name>" not in txt:
        return None
    if "<task-notification>" in txt:  # background task finished: Claude Code files it as a user line; it isn't the user
        summary = TAG("summary").search(txt)
        status = (TAG("status").search(txt) or [None, ""])[1].strip()
        word = {"completed": "done", "failed": "failed", "killed": "stopped", "stopped": "stopped"}.get(status, status or "done")
        return {"role": "assistant", "text": f"*⚙︎ Background task {word}: {summary.group(1).strip() if summary else ''}*"}
    m = TAG("local-command-stdout").search(txt)
    if m:
        out = ANSI.sub("", m.group(1)).strip()
        return {"role": "assistant", "text": f"```\n{out}\n```"} if out else None
    m = TAG("command-name").search(txt)
    if m:
        args = TAG("command-args").search(txt)
        return {"role": "user", "text": (m.group(1).strip() + " " + (args.group(1).strip() if args else "")).strip()}
    return {"role": "user", "text": txt}


def redact(text):
    for name, value in _secret_values["values"].items():  # stored secrets never reach Firestore even if a command printed one
        if value in text:
            text = text.replace(value, f"‹secret {name}›")
    return SECRET.sub("‹redacted›", text)


def text_of(content):
    if isinstance(content, str):
        return content
    out = []
    for b in content or []:
        t = b.get("type")
        if t == "text":
            out.append(b.get("text", ""))
        elif t == "tool_use":
            inp = b.get("input") or {}
            hint = inp.get("description") or inp.get("url") or inp.get("command") or inp.get("file_path") or inp.get("pattern") or inp.get("prompt") or ""
            hint = " ⏎ ".join(l.strip() for l in str(hint).splitlines() if l.strip())  # one line: multi-line commands leaked into chat as text
            out.append(f"🔧 {b.get('name')}: {hint[:160]}")
    return "\n".join(x for x in out if x)


PHOTO_HEAD = "Ảnh đính kèm từ điện thoại (xem bằng công cụ Read):"
FILE_HEAD = "File đính kèm từ điện thoại:"


def last_at(s):
    try:
        return dt.datetime.fromisoformat(s["lastAt"].replace("Z", "+00:00")) if s.get("lastAt") else None
    except ValueError:
        return None


# USD per million tokens: input, output, cache read. Cache writes bill 1.25× input (5 min) or 2× (1 h).
PRICES = {"opus": (5, 25, 0.5), "sonnet": (3, 15, 0.3), "haiku": (1, 5, 0.1), "fable": (5, 25, 0.5)}


def usage_cost(model, u):
    inp, out, read = next((v for k, v in PRICES.items() if k in (model or "")), PRICES["opus"])
    cc = u.get("cache_creation") or {}
    w1h = cc.get("ephemeral_1h_input_tokens") or 0
    w5m = (u.get("cache_creation_input_tokens") or 0) - w1h
    return ((u.get("input_tokens") or 0) * inp + (u.get("output_tokens") or 0) * out
            + (u.get("cache_read_input_tokens") or 0) * read + w5m * inp * 1.25 + w1h * inp * 2) / 1_000_000


def parse_session(path):
    """→ dict(title, cwd, turns[{role,text,ts}], activity, state) or None for empty/sidechain files."""
    title, custom, cwd, turns, by_msg = None, None, None, [], {}
    activity, state = "", "idle"
    seg_cost, seg_turn, billed = 0.0, None, set()  # cost of the current reply (all its API calls), shown on its last text
    with open(path, errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            t = d.get("type")
            if t == "custom-title":
                custom = d.get("customTitle") or custom  # user's rename wins over later ai-title lines
                continue
            if t in ("ai-title", "summary"):
                title = d.get("aiTitle") or d.get("summary") or title
                continue
            if t == "system" and d.get("subtype") == "local_command" and not d.get("isSidechain"):
                turn = slash_turn(str(d.get("content") or ""))  # newer CLIs log slash output as a system line
                if turn:
                    turn["ts"] = d.get("timestamp", "")
                    turns.append(turn)
                continue
            att = d.get("attachment") or {}
            if t == "attachment" and att.get("type") == "queued_command":  # typed/sent while a turn was running
                t, d = "user", {**d, "message": {"role": "user", "content": att.get("prompt") or ""}}
            if t not in ("user", "assistant") or d.get("isSidechain"):
                continue
            cwd = d.get("cwd") or cwd
            msg = d.get("message") or {}
            content = msg.get("content")
            if t == "user" and d.get("isCompactSummary"):   # the long "continued from a previous conversation" summary
                turns.append({"role": "assistant", "text": "*⚙︎ Context compacted*", "ts": d.get("timestamp", "")})
                continue
            if t == "user":
                if isinstance(content, list) and any(b.get("type") == "tool_result" for b in content):
                    state = "working"  # tool finished, model continues
                    continue
                txt = text_of(content).strip()
                tags = []
                if txt.startswith(FILE_HEAD):  # bridge-added upload paths → show the user's own text (app matches on it)
                    head, _, txt = txt.partition("\n\n")
                    tags.append("📎 " + ", ".join(pathlib.Path(l).name for l in head.splitlines()[1:]))
                photos = []
                if txt.startswith(PHOTO_HEAD):
                    head, _, txt = txt.partition("\n\n")
                    photos = [l.strip() for l in head.splitlines()[1:] if l.strip().startswith("/")]
                if (photos or tags) and txt.strip() in ("Xem ảnh đính kèm.", "Xem file đính kèm.", "See the attached photo.", "See the attached file."):
                    txt = ""  # the app's placeholder for attachment-only turns, not something the user typed
                if tags:
                    txt = (txt.strip() + "\n" + " · ".join(tags)).strip()
                if photos:  # published as real images when mirrored (see shown() in scan_sessions)
                    txt = (txt.strip() + "\n" + "\n".join(f"🖼path:{x}" for x in photos)).strip()
                turn = slash_turn(txt) if txt.startswith("<") else ({"role": "user", "text": txt} if txt else None)
                if not turn:
                    continue
                turn["ts"] = d.get("timestamp", "")
                turns.append(turn)
                activity, state = "Thinking…", "working"
                seg_cost, seg_turn = 0.0, None
            else:
                mid = msg.get("id")
                if mid and mid not in billed and msg.get("usage"):  # one line per content block, same usage repeated
                    billed.add(mid)
                    seg_cost += usage_cost(msg.get("model"), msg["usage"])
                    if seg_turn:
                        seg_turn["cost"] = seg_cost
                txt = text_of(content).strip()
                if not txt:
                    continue
                last_line = txt.splitlines()[-1]
                activity = last_line[:160]
                state = "idle" if msg.get("stop_reason") == "end_turn" else "working"
                if mid and mid in by_msg:
                    by_msg[mid]["text"] += "\n" + txt
                    seg_turn = seg_turn or by_msg[mid]  # message resumed after a user line (e.g. injected reminder)
                else:
                    turn = {"role": "assistant", "text": txt, "ts": d.get("timestamp", "")}
                    turns.append(turn)
                    if mid:
                        by_msg[mid] = turn
                    if seg_turn:
                        seg_turn.pop("cost", None)
                    seg_turn = turn
                seg_turn["cost"] = seg_cost
    if not turns:
        return None
    title = custom or title
    if not title:
        first = next((x["text"] for x in turns if x["role"] == "user"), "Claude Code session")
        title = first.splitlines()[0][:60]
    last = max((t.get("ts") or "" for t in turns), default="")
    return {"title": title, "cwd": cwd or str(HOME), "turns": turns, "activity": activity, "state": state, "lastAt": last}


_uploaded = {}  # session id → mtime uploaded
_fork_ids = set()  # side-branch sessions: never listed (their output lives in the request doc)
CHILDREN = tunnel_mod.DIR / "children.json"  # sub-session id → parent session id


def children():
    try:
        return json.loads(CHILDREN.read_text())
    except (OSError, ValueError):
        return {}


SESSION_DIRS = tunnel_mod.DIR / "session-dirs.json"  # session id → extra folders (--add-dir) from its environment


def session_dirs(sid=None, dirs=None):
    try:
        m = json.loads(SESSION_DIRS.read_text())
    except (OSError, ValueError):
        m = {}
    if dirs:
        m[sid] = dirs
        SESSION_DIRS.parent.mkdir(parents=True, exist_ok=True)
        SESSION_DIRS.write_text(json.dumps(m))
    return m.get(sid, []) if sid else m


def add_child(child, parent):
    m = children()
    m[child] = parent
    CHILDREN.parent.mkdir(parents=True, exist_ok=True)
    CHILDREN.write_text(json.dumps(m))


# ---------------------------------------------------------------- images the agent produces → shown in the app
IMAGE_PATH = re.compile(r"(?<![\w/])(/(?:Users|home|private|tmp|var/folders)/[^\s`'\"()<>\[\]]+?\.(?:png|jpe?g|webp|gif|heic))", re.I)
_published = {}  # (path, mtime) → image id


def publish_image(uid, path, upload=False):
    """Thumbnail (≤1280 px JPEG, <700 KB) into clo/{uid}/terminalImages/{id}; returns the id or None."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    if st.st_size > 40 * 1024 * 1024 or ("/.clo2-uploads/" in path and not upload):
        return None  # huge, or an upload path quoted by the agent (the user's turn already shows it)
    key = (path, st.st_mtime)
    if key in _published:
        return _published[key]
    iid = hashlib.sha1(f"{path}|{st.st_mtime}".encode()).hexdigest()[:20]
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "t.jpg")
        data = b""
        for side, q in ((1280, 80), (1024, 65), (720, 55)):
            if LINUX:   # no sips: ImageMagick (first frame, only shrinks), else ffmpeg
                cmd = (["convert", path + "[0]", "-auto-orient", "-resize", f"{side}x{side}>", "-quality", str(q), out]
                       if shutil.which("convert") else
                       ["ffmpeg", "-y", "-loglevel", "error", "-i", path, "-frames:v", "1", "-vf",
                        f"scale='min({side},iw)':'min({side},ih)':force_original_aspect_ratio=decrease", "-q:v", "4", out])
            else:
                cmd = ["sips", "-s", "format", "jpeg", "-s", "formatOptions", str(q), "-Z", str(side), path, "--out", out]
            try:
                subprocess.run(cmd, capture_output=True, timeout=60)
            except (OSError, subprocess.TimeoutExpired):
                return None   # no image tool here: show the session without the picture rather than not at all
            data = open(out, "rb").read() if os.path.exists(out) else b""
            if data and len(data) < 700_000:
                break
        if not data or len(data) >= 700_000:
            return None
    api("PATCH", f"{BASE}/clo/{uid}/terminalImages/{iid}", {"fields": {
        "data": {"bytesValue": base64.b64encode(data).decode()}, "name": enc(os.path.basename(path)),
        "path": enc(path), "createdAt": enc(now())}})
    _published[key] = iid
    return iid


def attach_images(uid, text):
    """Appends "🖼 <id> <name>" lines for image files an assistant turn mentions (written, read, or linked)."""
    seen, lines = set(), []
    for m in IMAGE_PATH.finditer(text or ""):
        path = m.group(1).rstrip(".,;:")
        if path in seen:
            continue
        seen.add(path)
        try:
            iid = publish_image(uid, path)
        except Exception as e:
            log("image publish failed", path, e)
            iid = None
        if iid:
            lines.append(f"🖼 {iid} {os.path.basename(path)}")
        if len(lines) >= 6:
            break
    return text + ("\n" + "\n".join(lines) if lines else "")


def context_tokens(sid):
    """Prompt size of the session's last model call (input + cache read + cache write) — what each new turn re-sends."""
    for f in PROJECTS.glob(f"*/{sid}.jsonl"):
        try:
            with open(f, "rb") as fh:
                fh.seek(max(0, f.stat().st_size - 400_000))
                tail = fh.read().decode(errors="replace").splitlines()[1:]
        except OSError:
            return 0
        for line in reversed(tail):
            if '"compact_boundary"' in line:
                return 0  # compacted after the last model call: the next turn starts from the summary
            if '"usage"' not in line:
                continue
            try:
                u = (json.loads(line).get("message") or {}).get("usage") or {}
            except ValueError:
                continue
            if u:
                return int(u.get("input_tokens", 0)) + int(u.get("cache_read_input_tokens", 0)) + int(u.get("cache_creation_input_tokens", 0))
    return 0


# Standard access: Claude edits files in the project and may use these without asking; anything else that needs
# permission is refused (claude -p can't ask). The clo2 tools keep artifacts and the secrets vault working.
STANDARD_TOOLS = ["Bash(clo2-artifact:*)", "Bash(clo2-secret:*)", "Bash(git status:*)", "Bash(git diff:*)",
                  "Bash(git log:*)", "Bash(git show:*)", "Bash(ls:*)", "Bash(pwd)", "WebSearch", "WebFetch"]


def permission_args(profile):
    """yolo only when the device asks for it (profile "…-yolo") AND the Mac's owner allowed full access."""
    if profile.endswith("yolo") and tunnel_mod.setting("access") == "full":
        return ["--dangerously-skip-permissions"]
    return ["--permission-mode", "acceptEdits", "--allowedTools", ",".join(STANDARD_TOOLS)]


def compact_session(uid, rid, sid, cwd, profile):
    """/compact in a separate turn before the user's, so long sessions stop re-sending their whole history."""
    tokens = context_tokens(sid)
    path = f"clo/{uid}/terminalInbox/{rid}"
    set_doc(path, {"reply": f"🗜 Long session ({tokens // 1000}k tokens) — compacting before running…", "editedAt": now()}, ["reply", "editedAt"])
    log("compact", sid[:8], tokens)
    cmd = [CONFIG["claude"], "-p", "--output-format", "stream-json", "--verbose", "--resume", sid] + permission_args(profile)
    try:
        subprocess.run(cmd, cwd=cwd, env=profile_env(profile), input="/compact", capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        log("compact timed out", sid[:8])
    after = context_tokens(sid)
    log("compacted", sid[:8], tokens, "→", after)
    return tokens, after


def scan_sessions(uid, force=None, limit=None):
    files = sorted((p for p in PROJECTS.glob("*/*.jsonl")), key=lambda p: p.stat().st_mtime, reverse=True)
    picked = files[: limit or CONFIG["max_sessions"]] + [p for p in files if p.stem == force]
    for p in picked:
        sid, mtime = p.stem, p.stat().st_mtime
        if sid in _fork_ids:
            continue
        # re-upload when the terminal lock flips too, when "working" lapses (90 s without a write: nothing else would
        # touch the file, e.g. claude idles on a background task) and when the bridge says its turn is over
        key = (mtime, open_in_terminal(sid), time.time() - mtime < 90, answered(sid, mtime))
        if _uploaded.get(sid) == key and sid != force:
            continue
        try:
            upload_session(uid, sid, p, mtime, key, limit)
        except Exception as e:  # one bad transcript must not stall the other sessions (or the loop)
            _uploaded[sid] = key  # don't retry every few seconds; retried when the file changes
            log("session failed", sid[:8], e)


ASKS = re.compile(r"\?\s*$|(?i:\b(bạn|anh|chị)\s+(muốn|chọn|có muốn|ok|đồng ý|xác nhận|cần)|có nên|hay không|nhé\s*$|"
                  r"cho mình biết|báo mình|chờ bạn|cần bạn|should i|do you want|would you like|let me know|which (one|option))")


def asks_user(turns):
    """Last reply ends by asking the user something (a question, a choice, "ok thì mình làm") → "Cần bạn trả lời"."""
    if not turns or turns[-1]["role"] != "assistant":
        return False
    lines = [l.strip() for l in turns[-1]["text"].splitlines() if l.strip() and not l.startswith(("🔧", "🖼"))]
    return any(ASKS.search(l) for l in lines[-3:])


_app_sids = {"at": 0.0, "ids": set()}


def app_sessions(uid, max_age=60):
    """Session ids a phone/browser ever sent a turn to (terminalInbox sessionId / resultSessionId)."""
    if time.time() - _app_sids["at"] > max_age:
        ids = set()
        for d in (api("GET", f"{BASE}/clo/{uid}/terminalInbox?mask.fieldPaths=sessionId&mask.fieldPaths=resultSessionId") or {}).get("documents", []):
            f = unfields(d)
            ids.update(x for x in (f.get("sessionId"), f.get("resultSessionId")) if x)
        _app_sids.update(at=time.time(), ids=ids)
    return _app_sids["ids"]


def is_background(uid, sid, path):
    """A script's `claude -p` run (cron, skills pipelines…), not a conversation: never opened in a terminal
    (no "entrypoint":"cli" line) and never sent a turn from the app/web. The app files these under "Task chạy ngầm"."""
    if sid in app_sessions(uid) or sid in app_sessions(uid, max_age=10):   # a brand-new app session: re-read soon
        return False
    try:
        with open(path, "rb") as fh:
            return b'"entrypoint":"cli"' not in fh.read()
    except OSError:
        return False


def upload_session(uid, sid, p, mtime, key, limit):
        s = parse_session(p)
        _uploaded[sid] = key
        if not s:
            return
        mt = CONFIG["max_text"]
        def shown(x):
            lines = x["text"].split("\n")
            uploads = [l[len("🖼path:"):] for l in lines if l.startswith("🖼path:")]
            body = "\n".join(l for l in lines if not l.startswith("🖼path:"))
            text = redact(body[:mt]) + ("…" if len(body) > mt else "")
            if x["role"] == "assistant":
                text = attach_images(uid, text)  # 🖼 lines after the cut, never truncated
            for path in uploads:  # the user's own photos
                iid = publish_image(uid, path, upload=True)
                text += f"\n🖼 {iid} {os.path.basename(path)}" if iid else "\n📷 photo"
            return text.strip()
        turns = [{"role": x["role"], "text": shown(x), "ts": x["ts"], **({"cost": round(x["cost"], 4)} if x.get("cost") else {})}
                 for x in s["turns"][-CONFIG["max_turns"]:]]
        doc = {"title": redact(s["title"]), "cwd": s["cwd"], "project": pathlib.Path(s["cwd"]).name or s["cwd"],
               "machine": CONFIG["machine"], "machineLabel": CONFIG["label"] or CONFIG["machine"], "turnCount": len(s["turns"]), "transcript": turns,
               # Last real message, not file mtime: CLIs rewrite/touch old transcripts (resume, title, snapshots).
               "editedAt": last_at(s) or dt.datetime.fromtimestamp(mtime, dt.timezone.utc),
               # A session counts as busy if the model is mid-turn and the file was written recently.
               "state": "working" if s["state"] == "working" and time.time() - mtime < 90 and not answered(sid, mtime) else "idle",
               "openOnMac": key[1],
               "needsInput": s["state"] != "working" and asks_user(s["turns"]),
               "totalCost": round(sum(x.get("cost") or 0 for x in s["turns"]), 2),  # whole session, not just the mirrored tail
               "parentId": children().get(sid, ""),
               "background": is_background(uid, sid, p),
               "contextTokens": context_tokens(sid),
               "activity": redact(s["activity"]), "activityAt": dt.datetime.fromtimestamp(mtime, dt.timezone.utc),
               "syncedAt": now()}  # every write: app/web list only what changed since their last look (Firestore bills reads)
        try:
            live_update(uid, sid, doc["title"], doc["activity"], s["state"] == "working")
        except Exception as e:
            log("live update failed", e)
        # Shrink until under Firestore's 1 MiB document limit.
        while len(json.dumps(fields(doc))) > 900_000 and len(doc["transcript"]) > 4:
            doc["transcript"] = doc["transcript"][len(doc["transcript"]) // 4:]
        set_doc(f"clo/{uid}/terminalSessions/{sid}", doc, list(doc.keys()))
        if not limit:
            log("session ↑", sid[:8], s["title"][:50])


# ---------------------------------------------------------------- running requests
_running = {}  # request id → Popen (None while starting)
_wake = threading.Event()  # set when a device writes to the inbox or a turn ends: the main loop polls at once
_activity = {"at": time.time()}  # last time the inbox had something for this Mac (fast polling for 10 min after)
_run_sid = {}  # request id → session id of a non-fork running turn
_stopped = {}  # request ids killed by /stop (claude may exit 143 instead of dying by signal)
_busy_sessions = set()
_workers = []
_live = {}  # session id → turn still accepting messages: proc, rid, lock, open, browser, injected request ids
_answered = {}      # session id → its transcript's mtime when the bridge called the turn over while claude still runs
                    # (background task): shown idle until the file is written again (claude resumed: working again)


def answered(sid, mtime):
    return mtime <= _answered.get(sid, -1) + 1
_lingering = set()  # request ids that answered but whose claude stays up for a background task it started
_injecting = set()  # request ids being written into a live turn (pending in Firestore until done)
_injected_ids = set()  # request ids delivered into a still-running turn (status "injected")


# ---------------------------------------------------------------- only requests the owner signed (via the CLO backend) run
# The backend signs a request (Ed25519) only for the owner signed in with Google; this Mac holds just the public key.
# Anything else that can write Firestore (a leaked service account, a custom token, a prompt-injected agent) can't
# make a Mac run something. Each signed request runs once (a "done" doc set back to pending is refused).
SIGN_PUB = pathlib.Path(__file__).resolve().parent / "inbox-signing.pub"
SIGN_MAX_AGE = 14 * 86400
OWN = STATE / "own-requests.json"   # requests this bridge queued itself (sub-session reports)
RAN = STATE / "ran-requests.json"   # signed request ids already run here → refused if they come back
_pub = {}


def _load(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _mark(path, rid, drop=False):
    m = _load(path)
    cutoff = time.time() - SIGN_MAX_AGE - 86400
    m = {k: v for k, v in m.items() if v > cutoff}
    if drop:
        m.pop(rid, None)
    else:
        m[rid] = time.time()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(m))


def ed25519_verify(pub, sig, msg):
    """RFC 8032 Ed25519 verification in plain Python (no `cryptography` needed — the MacBook's python3 doesn't have it)."""
    p = 2 ** 255 - 19
    L = 2 ** 252 + 27742317777372353535851937790883648493
    d = -121665 * pow(121666, p - 2, p) % p
    I = pow(2, (p - 1) // 4, p)

    def add(P, Q):
        x1, y1, z1, t1 = P; x2, y2, z2, t2 = Q
        a, b = (y1 - x1) * (y2 - x2) % p, (y1 + x1) * (y2 + x2) % p
        c, dd = t1 * 2 * d * t2 % p, z1 * 2 * z2 % p
        e, f, g, h = b - a, dd - c, dd + c, b + a
        return (e * f % p, g * h % p, f * g % p, e * h % p)

    def mul(s, P):
        Q = (0, 1, 1, 0)
        while s:
            if s & 1:
                Q = add(Q, P)
            P, s = add(P, P), s >> 1
        return Q

    def decode(b):
        if len(b) != 32:
            return None
        y = int.from_bytes(b, "little") & ((1 << 255) - 1)
        sign = b[31] >> 7
        if y >= p:
            return None
        x2 = (y * y - 1) * pow(d * y * y + 1, p - 2, p) % p
        x = pow(x2, (p + 3) // 8, p)
        if (x * x - x2) % p:
            x = x * I % p
        if (x * x - x2) % p or (x == 0 and sign):
            return None
        if x & 1 != sign:
            x = p - x
        return (x, y, 1, x * y % p)

    def same(P, Q):
        return (P[0] * Q[2] - Q[0] * P[2]) % p == 0 and (P[1] * Q[2] - Q[1] * P[2]) % p == 0

    if len(sig) != 64:
        return False
    A, R = decode(pub), decode(sig[:32])
    S = int.from_bytes(sig[32:], "little")
    if not A or not R or S >= L:
        return False
    gy = 4 * pow(5, p - 2, p) % p
    G = decode(gy.to_bytes(32, "little"))
    h = int.from_bytes(hashlib.sha512(sig[:32] + pub + msg).digest(), "little") % L
    return same(mul(S, G), add(R, mul(h, A)))


def verified(uid, rid, req):
    """What to run for this request. Only paired devices (end-to-end encrypted tunnel) can write the store, so a request
    runs as written — except one this bridge queued itself (a sub-session report), which runs exactly what it wrote."""
    own = _load(OWN).get(rid)
    if own and req.get("fromChild"):
        # Queued by this bridge: run exactly what it wrote, with the folder/profile it chose.
        keep = {k: v for k, v in req.items() if k in ("status", "createdAt", "editedAt", "force", "interrupt")}
        return {**keep, **own["req"]} if hashlib.sha256(json.dumps(own["req"], sort_keys=True).encode()).hexdigest() == own["h"] else None
    return req


# ---------------------------------------------------------------- secrets (clo/{uid}/secrets/{name})
# The app/web encrypt each value on the phone/browser to every Mac's X25519 key (published in the heartbeat as secretKey);
# Firestore and the backend only ever hold ciphertext, signed by the backend for the owner. A Mac's private key lives in its
# login Keychain. Box = b64url(ephemeral pub 32 ‖ nonce 12 ‖ ChaCha20-Poly1305 ciphertext+tag), key = SHA-256("clo-secret v1"
# ‖ X25519 shared ‖ ephemeral pub ‖ Mac pub), AAD "clo-secret|<name>|<machine>". Pure Python: the MacBook has no `cryptography`.
P25519 = 2 ** 255 - 19
LINUX = tunnel_mod.LINUX
file_key = clo2_tools.file_key
KEYCHAIN = ("clo-secrets", "box-key")  # service, account
SECRET_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")



def x25519(k, u):
    k = bytearray(k)
    k[0] &= 248; k[31] &= 127; k[31] |= 64
    k, x1 = int.from_bytes(k, "little"), int.from_bytes(u, "little") & ((1 << 255) - 1)
    p = P25519
    x2, z2, x3, z3, swap = 1, 0, x1, 1, 0
    for t in range(254, -1, -1):
        bit = (k >> t) & 1
        if swap ^ bit:
            x2, x3, z2, z3 = x3, x2, z3, z2
        swap = bit
        a, b, c, d = x2 + z2, x2 - z2, x3 + z3, x3 - z3
        aa, bb, da, cb = a * a % p, b * b % p, d * a % p, c * b % p
        e = (aa - bb) % p
        x3, z3 = (da + cb) ** 2 % p, x1 * (da - cb) ** 2 % p
        x2, z2 = aa * bb % p, e * (aa + 121665 * e) % p
    if swap:
        x2, z2 = x3, z3
    return (x2 * pow(z2, p - 2, p) % p).to_bytes(32, "little")


def _chacha_block(key, counter, nonce):
    import struct
    s = [0x61707865, 0x3320646e, 0x79622d32, 0x6b206574, *struct.unpack("<8I", key), counter, *struct.unpack("<3I", nonce)]
    x = s[:]
    M = 0xffffffff

    def qr(a, b, c, d):
        x[a] = (x[a] + x[b]) & M; x[d] ^= x[a]; x[d] = (x[d] << 16 | x[d] >> 16) & M
        x[c] = (x[c] + x[d]) & M; x[b] ^= x[c]; x[b] = (x[b] << 12 | x[b] >> 20) & M
        x[a] = (x[a] + x[b]) & M; x[d] ^= x[a]; x[d] = (x[d] << 8 | x[d] >> 24) & M
        x[c] = (x[c] + x[d]) & M; x[b] ^= x[c]; x[b] = (x[b] << 7 | x[b] >> 25) & M
    for _ in range(10):
        qr(0, 4, 8, 12); qr(1, 5, 9, 13); qr(2, 6, 10, 14); qr(3, 7, 11, 15)
        qr(0, 5, 10, 15); qr(1, 6, 11, 12); qr(2, 7, 8, 13); qr(3, 4, 9, 14)
    return struct.pack("<16I", *((x[i] + s[i]) & M for i in range(16)))


def _chacha20(key, counter, nonce, data):
    out = bytearray()
    for i in range(0, len(data), 64):
        block = _chacha_block(key, counter + i // 64, nonce)
        out += bytes(a ^ b for a, b in zip(data[i:i + 64], block))
    return bytes(out)


def _poly1305(key, msg):
    r = int.from_bytes(key[:16], "little") & 0x0ffffffc0ffffffc0ffffffc0fffffff
    acc, p = 0, (1 << 130) - 5
    for i in range(0, len(msg), 16):
        acc = (acc + int.from_bytes(msg[i:i + 16] + b"\x01", "little")) * r % p
    return ((acc + int.from_bytes(key[16:], "little")) % (1 << 128)).to_bytes(16, "little")


def _aead_mac(key, nonce, aad, ct):
    import struct
    pad = lambda b: b"\x00" * (-len(b) % 16)
    return _poly1305(_chacha_block(key, 0, nonce)[:32], aad + pad(aad) + ct + pad(ct) + struct.pack("<QQ", len(aad), len(ct)))


def aead_seal(key, nonce, plain, aad):
    ct = _chacha20(key, 1, nonce, plain)
    return ct + _aead_mac(key, nonce, aad, ct)


def aead_open(key, nonce, sealed, aad):
    import hmac
    ct, tag = sealed[:-16], sealed[-16:]
    if len(sealed) < 16 or not hmac.compare_digest(_aead_mac(key, nonce, aad, ct), tag):
        raise ValueError("secret box: bad tag")
    return _chacha20(key, 1, nonce, ct)


def _b64u(b): return base64.urlsafe_b64encode(b).decode().rstrip("=")
def _unb64u(s): return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _box_key(shared, epub, pub):
    return hashlib.sha256(b"clo-secret v1" + shared + epub + pub).digest()


def secret_seal(pub, name, machine, value):
    """Same as the app/web do (used by tests)."""
    e = os.urandom(32)
    epub = x25519(e, (9).to_bytes(32, "little"))
    nonce = os.urandom(12)
    key = _box_key(x25519(e, pub), epub, pub)
    return _b64u(epub + nonce + aead_seal(key, nonce, value.encode(), f"clo-secret|{name}|{machine}".encode()))


def secret_open(priv, name, machine, box):
    raw = _unb64u(box)
    epub, nonce, sealed = raw[:32], raw[32:44], raw[44:]
    pub = x25519(priv, (9).to_bytes(32, "little"))
    return aead_open(_box_key(x25519(priv, epub), epub, pub), nonce, sealed, f"clo-secret|{name}|{machine}".encode()).decode()


_box = {}


def _keychain_key(account, label):
    """32 random bytes kept in the login Keychain under (clo-secrets, account); created on first use."""
    if account in _box:
        return _box[account]
    if LINUX:   # no login Keychain: a 0600 file in ~/.clo2/keys (like the pairing identity next to it)
        _box[account] = file_key(f"{KEYCHAIN[0]}-{account}")
        return _box[account]
    r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN[0], "-a", account, "-w"], capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        key = _unb64u(r.stdout.strip())
    else:
        key = os.urandom(32)
        r = subprocess.run(["security", "add-generic-password", "-U", "-s", KEYCHAIN[0], "-a", account, "-l", label, "-w", _b64u(key)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("Keychain: " + (r.stderr.strip() or "add failed"))
        log("secrets: created", account, "in the login Keychain")
    _box[account] = key
    return key


def box_private_key():
    """This Mac's X25519 private key (secrets are sealed to it)."""
    return _keychain_key(KEYCHAIN[1], "CLO secrets (this Mac's decryption key)")


def box_public_key():
    if "pub" not in _box:
        _box["pub"] = _b64u(x25519(box_private_key(), (9).to_bytes(32, "little")))
    return _box["pub"]


# Ed25519 signing (RFC 8032), pure Python: a Mac signs what it re-seals for other devices and secrets added with `clo-secret set`.
_EP = 2 ** 255 - 19
_EL = 2 ** 252 + 27742317777372353535851937790883648493
_ED = -121665 * pow(121666, _EP - 2, _EP) % _EP


def _ed_add(P, Q):
    p, d = _EP, _ED
    x1, y1, z1, t1 = P; x2, y2, z2, t2 = Q
    a, b = (y1 - x1) * (y2 - x2) % p, (y1 + x1) * (y2 + x2) % p
    c, dd = t1 * 2 * d * t2 % p, z1 * 2 * z2 % p
    e, f, g, h = b - a, dd - c, dd + c, b + a
    return (e * f % p, g * h % p, f * g % p, e * h % p)


def _ed_mul(s, P):
    Q = (0, 1, 1, 0)
    while s:
        if s & 1:
            Q = _ed_add(Q, P)
        P, s = _ed_add(P, P), s >> 1
    return Q


def _ed_base():
    p = _EP
    y = 4 * pow(5, p - 2, p) % p
    x2 = (y * y - 1) * pow(_ED * y * y + 1, p - 2, p) % p
    x = pow(x2, (p + 3) // 8, p)
    if (x * x - x2) % p:
        x = x * pow(2, (p - 1) // 4, p) % p
    if x & 1:
        x = p - x
    return (x, y, 1, x * y % p)


def _ed_encode(P):
    zi = pow(P[2], _EP - 2, _EP)
    x, y = P[0] * zi % _EP, P[1] * zi % _EP
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _ed_secret(seed):
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a = (a & ((1 << 254) - 8)) | (1 << 254)
    return a, h[32:]


def ed25519_public(seed):
    return _ed_encode(_ed_mul(_ed_secret(seed)[0], _ed_base()))


def ed25519_sign(seed, msg):
    a, prefix = _ed_secret(seed)
    A = _ed_encode(_ed_mul(a, _ed_base()))
    r = int.from_bytes(hashlib.sha512(prefix + msg).digest(), "little") % _EL
    R = _ed_encode(_ed_mul(r, _ed_base()))
    k = int.from_bytes(hashlib.sha512(R + A + msg).digest(), "little") % _EL
    return R + ((r + k * a) % _EL).to_bytes(32, "little")


def sign_seed():
    return _keychain_key("sign-key", "CLO secrets (this Mac's signing key)")


def sign_public_key():
    if "signpub" not in _box:
        _box["signpub"] = _b64u(ed25519_public(sign_seed()))
    return _box["signpub"]


def _signed_payload(doc, key):
    signed, sig = doc.get("signed"), doc.get("sig")
    if not isinstance(signed, str) or not isinstance(sig, str):
        return None
    try:
        return json.loads(signed) if ed25519_verify(key, _unb64u(sig), signed.encode()) else None
    except ValueError:
        return None


def _backend_key():
    if "key" not in _pub:
        _pub["key"] = base64.b64decode(SIGN_PUB.read_text().strip())
    return _pub["key"]


# Approved devices: clo/{uid}/vault/devices, signed by the backend for the owner (app/web "Duyệt"). id → {kind, label, box, sign}.
# New secrets are sealed to these; Macs re-seal existing secrets for devices that lack them (clo/{uid}/secretShares).
_registry = {"at": 0.0, "devices": None}


def vault_devices(uid, max_age=30):
    if time.time() - _registry["at"] < max_age:
        return _registry["devices"]
    d = api("GET", f"{BASE}/clo/{uid}/vault/devices") or {}
    p = _signed_payload(unfields(d), _backend_key()) if d.get("fields") else None
    ok = p and p.get("type") == "vaultDevices" and p.get("uid") == uid and isinstance(p.get("devices"), dict)
    _registry.update(at=time.time(), devices=p["devices"] if ok else None)
    return _registry["devices"]


def secret_doc_verified(uid, name, doc, devices=None):
    """Signed payload of a secret doc ({name, label, kind, boxes, signedAt[, by]}): signed by the backend (app/web), or by an
    approved Mac (`clo-secret set`, payload `by`). None otherwise."""
    try:
        by = json.loads(doc.get("signed") or "{}").get("by")
    except ValueError:
        return None
    if by:
        dev = (devices or {}).get(by) or {}
        p = _signed_payload(doc, _unb64u(dev["sign"])) if dev.get("sign") else None
        if p and p.get("by") != by:
            return None
    else:
        p = _signed_payload(doc, _backend_key())
    if not p or p.get("type") != "secret" or p.get("uid") != uid or p.get("name") != name or not isinstance(p.get("boxes"), dict):
        return None
    return p


def share_message(uid, name, device, signed_at, box):
    return f"clo-share|{uid}|{name}|{device}|{signed_at}|{box}".encode()


def _list(uid, coll):
    out, page = [], ""
    while True:
        r = api("GET", f"{BASE}/clo/{uid}/{coll}?pageSize=300" + (f"&pageToken={page}" if page else "")) or {}
        out += [(d["name"].rsplit("/", 1)[-1], unfields(d)) for d in r.get("documents", [])]
        page = r.get("nextPageToken")
        if not page:
            return out


def secrets_fetch(uid, with_meta=False):
    """{name: {label, kind, value|None, machines, editedAt}} for every signed secret; value is None if nothing here can open it
    (not sealed to this Mac, and no Mac has re-sealed it yet). with_meta adds the payload and the valid shares (for reshare)."""
    me, devices = CONFIG["machine"], vault_devices(uid)
    shares = {}
    for sid, f in _list(uid, "secretShares"):
        dev = (devices or {}).get(f.get("by")) or {}
        if dev.get("sign") and isinstance(f.get("box"), str):
            try:
                ok = ed25519_verify(_unb64u(dev["sign"]), _unb64u(f.get("sig") or ""),
                                    share_message(uid, f.get("name"), f.get("device"), f.get("signedAt"), f["box"]))
            except ValueError:
                ok = False
            if ok:
                shares[(f.get("name"), f.get("device"), f.get("signedAt"))] = f["box"]
        shares.setdefault(("__ids__",), []).append((sid, f.get("name"), f.get("signedAt")))
    out = {}
    for name, f in _list(uid, "secrets"):
        p = secret_doc_verified(uid, name, f, devices)
        if not p:
            continue
        box, value = p["boxes"].get(me) or shares.get((name, me, p.get("signedAt"))), None
        if isinstance(box, str):
            try:
                value = secret_open(box_private_key(), name, me, box)
            except (ValueError, RuntimeError) as e:
                log("secret", name, "unreadable here:", e)
        readers = set(p["boxes"]) | {k[1] for k in shares if len(k) == 3 and k[0] == name and k[2] == p.get("signedAt")}
        out[name] = {"label": p.get("label", ""), "kind": p.get("kind", ""), "value": value,
                     "machines": sorted(readers), "editedAt": p.get("signedAt", "")}
        if with_meta:
            out[name]["payload"] = p
    if with_meta:
        out["__shares__"] = shares
    return out


def seal_for_devices(name, value, devices):
    return {i: secret_seal(_unb64u(d["box"]), name, i, value) for i, d in devices.items() if isinstance(d.get("box"), str)}


def reshare(uid):
    """Seal secrets this Mac can read for approved devices that don't have them (a Mac/iPhone added later), signed with this
    Mac's key; drop shares of deleted/changed secrets. Only once this Mac itself is approved with its current keys."""
    me, devices = CONFIG["machine"], vault_devices(uid)
    mine = (devices or {}).get(me) or {}
    if mine.get("sign") != sign_public_key() or mine.get("box") != box_public_key():
        return 0
    data = secrets_fetch(uid, with_meta=True)
    shares = data.pop("__shares__")
    current = {(n, s["payload"].get("signedAt")) for n, s in data.items()}
    for sid, name, signed_at in shares.get(("__ids__",), []):
        if (name, signed_at) not in current:
            api("DELETE", f"{BASE}/clo/{uid}/secretShares/{sid}")
    made = 0
    for name, s in data.items():
        if s["value"] is None:
            continue
        p = s["payload"]
        for dev_id, dev in devices.items():
            if dev_id == me or dev_id in p["boxes"] or (name, dev_id, p.get("signedAt")) in shares or not isinstance(dev.get("box"), str):
                continue
            box = secret_seal(_unb64u(dev["box"]), name, dev_id, s["value"])
            sig = _b64u(ed25519_sign(sign_seed(), share_message(uid, name, dev_id, p.get("signedAt"), box)))
            set_doc(f"clo/{uid}/secretShares/{name}__{dev_id}", {"name": name, "device": dev_id, "signedAt": p.get("signedAt"),
                                                                   "box": box, "by": me, "sig": sig, "editedAt": now()})
            made += 1
            log("secret", name, "re-sealed for", dev.get("label") or dev_id)
    return made


def secret_set(uid, name, value, label="", kind=""):
    """`clo-secret set`: add/replace a secret from this Mac — sealed to every approved device, signed with this Mac's key."""
    me, devices = CONFIG["machine"], vault_devices(uid, max_age=0)
    mine = (devices or {}).get(me) or {}
    if mine.get("sign") != sign_public_key():
        raise RuntimeError("this Mac isn't approved in the vault yet (open the CLO app → Secrets → Approve)")
    payload = {"type": "secret", "uid": uid, "name": name, "label": label, "kind": kind, "boxes": seal_for_devices(name, value, devices),
               "signedAt": now().isoformat().replace("+00:00", "Z"), "by": me}
    signed = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    set_doc(f"clo/{uid}/secrets/{name}", {"name": name, "label": label, "kind": kind, "machines": sorted(payload["boxes"]),
                                          "signed": signed, "sig": _b64u(ed25519_sign(sign_seed(), signed.encode())), "editedAt": now()})
    return sorted(payload["boxes"])


_secret_values = {"at": 0.0, "values": {}}  # name → value, for scrubbing transcripts
_registry_seen = {"at": 0.0, "signed": None}


def registry_changed(uid):
    """One read every 20 s: has the approved-device list changed since the last full secrets refresh?"""
    if time.time() - _registry_seen["at"] < 20:
        return False
    _registry_seen["at"] = time.time()
    d = api("GET", f"{BASE}/clo/{uid}/vault/devices?mask.fieldPaths=signed") or {}
    signed = (d.get("fields") or {}).get("signed", {}).get("stringValue")
    changed, _registry_seen["signed"] = signed != _registry_seen["signed"], signed
    if changed:
        _registry["at"] = 0  # re-read the full list too
    return changed


def refresh_secret_values(uid):
    _secret_values["at"] = time.time()
    try:
        reshare(uid)
    except Exception as e:
        log("reshare failed:", e)
    _secret_values["values"] = {n: s["value"] for n, s in secrets_fetch(uid).items() if s["value"] and len(s["value"]) >= 6}


def refuse(uid, rid, why):
    log("refused", rid[:8], why)
    set_doc(f"clo/{uid}/terminalInbox/{rid}", {"status": "error", "error": f"Bridge refused: {why}", "editedAt": now()},
            ["status", "error", "editedAt"])


def user_line(text):
    return json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n"


def age(ts):
    try:
        ts = re.sub(r"(\.\d{6})\d+", r"\1", str(ts)).replace("Z", "+00:00")
        return (now() - dt.datetime.fromisoformat(ts)).total_seconds()
    except ValueError:
        return 1e9


def close_input(live):
    """No more messages into this turn: claude finishes what it already read, then exits on EOF."""
    with live["lock"]:
        if live["open"]:
            live["open"] = False
            try:
                live["proc"].stdin.close()
            except (OSError, ValueError):
                pass


def seal(rid):
    for live in list(_live.values()):
        if live["rid"] == rid:
            close_input(live)


_poll = {"n": 0}  # inbox poll counter: a waiting message not seen in the latest polls was deleted / changed


def wait_for_turn(rid, req, sid):
    """A message for a session whose turn is running: it waits on that turn (still "pending" in the app's queue). When
    the turn finishes, every waiting message goes in together as ONE next message (flush_waiting), not one turn each."""
    live = _live.get(sid)
    if not live or not live["open"] or (req.get("browser") and not live["browser"]):
        return False
    live["waiting"][rid] = (req, _poll["n"])
    return True


def flush_waiting(uid, live):
    """Turn finished: hand the messages that waited on it to the same claude as one message. False → nothing waited."""
    with live["lock"]:   # held through the status writes, so the turn can't finish and mark them first
        items = [(r, q) for r, (q, n) in live["waiting"].items() if n >= _poll["n"] - 1 and r not in _injected_ids]
        live["waiting"].clear()
        if not live["open"] or not items:
            return False
        items.sort(key=lambda x: str(x[1].get("createdAt") or ""))
        try:
            live["proc"].stdin.write(user_line("\n\n".join(prompt_for(r, q) for r, q in items)))
            live["proc"].stdin.flush()
        except (OSError, ValueError):
            return False
        for r, _ in items:
            live["injected"].append(r)
            _injected_ids.add(r)
            _mark(RAN, r)
            set_doc(f"clo/{uid}/terminalInbox/{r}", {"status": "injected", "injectedInto": live["rid"], "editedAt": now()},
                    ["status", "injectedInto", "editedAt"])
    log("waiting", len(items), "message(s) → one message into", live["rid"][:8])
    return True


def profile_env(profile):
    env = dict(os.environ)
    env["PATH"] = f"{HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + env.get("PATH", "")
    env.setdefault("API_TIMEOUT_MS", "300000")
    if CONFIG.get("subagent_model"):
        env["CLAUDE_CODE_SUBAGENT_MODEL"] = CONFIG["subagent_model"]  # a stalled model request errors/retries instead of hanging the turn
    for k in ("CLAUDE_CODE_USE_VERTEX", "CLOUD_ML_REGION", "ANTHROPIC_VERTEX_PROJECT_ID", "ANTHROPIC_MODEL"):
        env.pop(k, None)
    return env   # CLO 2: always the user's own Claude Code login (no Vertex / cloud billing)


def prompt_for(rid, req):
    """The request's text, with uploaded photos/files saved here and their paths prepended."""
    prompt = req.get("text", "")
    images = [x for x in (req.get("images") or []) if isinstance(x, str)][:4]
    if images:
        updir = HOME / ".clo2-uploads" / rid
        updir.mkdir(parents=True, exist_ok=True)
        paths = []
        for i, b64img in enumerate(images, 1):
            f = updir / f"image-{i}.jpg"
            f.write_bytes(base64.b64decode(b64img))
            paths.append(str(f))
        prompt = PHOTO_HEAD + "\n" + "\n".join(paths) + "\n\n" + prompt
    files = [x for x in (req.get("files") or []) if isinstance(x, str)][:5]  # build 24: inline base64
    if not files and req.get("fileNames"):   # CLO 2: sent beforehand through the tunnel (op "upload")
        updir = HOME / ".clo2-uploads" / rid
        paths = [str(updir / upload_name(n)) for n in req.get("fileNames") if (updir / upload_name(n)).exists()]
        if paths:
            prompt = FILE_HEAD + "\n" + "\n".join(paths) + "\n\n" + prompt
    elif files:
        names = list(req.get("fileNames") or [])
        updir = HOME / ".clo2-uploads" / rid
        updir.mkdir(parents=True, exist_ok=True)
        paths = []
        for i, b64 in enumerate(files):
            name = pathlib.Path(str(names[i]) if i < len(names) else "").name.lstrip(".") or f"file-{i + 1}"
            f = updir / name
            f.write_bytes(base64.b64decode(b64))
            paths.append(str(f))
        prompt = FILE_HEAD + "\n" + "\n".join(paths) + "\n\n" + prompt
    return prompt


def fix_cwd(cwd):
    """Typed folders arrive mangled: "~//Users/me/x" → "/Users/me//Users/me/x", "Users/me/x", "~/x/". Make them a real path."""
    c = cwd.strip()
    if c.startswith("~"):
        c = str(HOME) + "/" + c[1:].lstrip("/")
    i = c.rfind("/Users/")
    if i > 0:
        c = c[i:]                      # a full path pasted after the home prefix
    elif c.startswith("Users/"):
        c = "/" + c
    elif not c.startswith("/"):
        c = str(HOME / c)
    c = re.sub(r"/+", "/", c).rstrip("/") or "/"
    if not os.path.isdir(c) and os.path.isdir(HOME / c.lstrip("/")):
        c = str(HOME / c.lstrip("/"))  # "/letcode/clo2" meant ~/letcode/clo2
    return c


def run_request(uid, rid, req):
    path = f"clo/{uid}/terminalInbox/{rid}"
    sid = req.get("sessionId") or ""
    profile = (req.get("profile") or "claude").replace("vertex", "claude")   # CLO 2 has no Vertex profiles
    cwd = fix_cwd(req.get("cwd") or str(HOME))
    if req.get("mkdir") and not sid and not os.path.exists(cwd) and os.path.normpath(cwd).startswith(str(HOME) + "/"):
        cwd = os.path.normpath(cwd)
        os.makedirs(cwd, exist_ok=True)   # "Start a new project" in the app: a new folder under the home folder
        log("new project folder", cwd)
    if not os.path.isdir(cwd):
        set_doc(path, {"status": "error", "error": f"Folder not found: {cwd}", "editedAt": now()}, ["status", "error", "editedAt"])
        return
    fork = bool(req.get("fork")) and bool(sid)
    child = bool(req.get("child")) and bool(sid) and not fork  # sub-session: a fork that stays a session and reports back
    compacted = None
    compact_at = int(req.get("compactAt") or CONFIG.get("compact_at") or 0)  # app picks 200k / 400k / 1M
    if sid and not fork and not child and compact_at and not (req.get("text") or "").strip().startswith("/compact") \
            and context_tokens(sid) > compact_at:
        compacted = compact_session(uid, rid, sid, cwd, profile)
    cmd = [CONFIG["claude"], "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
    if sid:
        cmd += ["--resume", sid]
    if fork or child:
        cmd += ["--fork-session"]  # side branch: same context, separate session, main history untouched
    add_dirs = [d for d in (req.get("addDirs") or session_dirs(sid) if sid else req.get("addDirs") or []) if isinstance(d, str) and os.path.isdir(d)]
    for d in add_dirs:
        cmd += ["--add-dir", d]  # environment's extra folders (remembered per session: every turn needs them again)
    if req.get("worktree") and not sid:
        cmd += ["--worktree"]  # fresh git worktree under <cwd>/.claude/worktrees for this new session
    if req.get("effort") in ("low", "medium", "high", "xhigh", "max"):
        cmd += ["--effort", req["effort"]]  # picked in the app's composer
    cmd += permission_args(profile)
    if (HOME / ".local/bin/clo2-artifact").exists():
        cmd += ["--append-system-prompt", ARTIFACT_HINT + (SECRET_HINT if (HOME / ".local/bin/clo2-secret").exists() else "") + NOTIFY_HINT]
    browser = bool(req.get("browser")) or (CONFIG.get("browser_always", True) and not fork)  # Claude opens it only if needed
    if browser:
        # Browser only for these turns, so normal terminal sessions don't carry the tool definitions.
        npx = shutil.which("npx", path=f"/opt/homebrew/bin:/usr/local/bin:{HOME}/.local/bin") or "npx"
        args = ["-y", "@playwright/mcp@latest", "--user-data-dir", CONFIG["browser_profile"]]
        if CONFIG["browser_headless"]:
            args.append("--headless")
        cmd += ["--mcp-config", json.dumps({"mcpServers": {"browser": {"command": npx, "args": args}}})]
        if "--allowedTools" in cmd:   # one list (Standard access already has one)
            i = cmd.index("--allowedTools") + 1
            cmd[i] += ",mcp__browser"
        else:
            cmd += ["--allowedTools", "mcp__browser"]
    log("run", rid[:8], "session", sid[:8] or "(new)", profile, "+browser" if browser else "", cwd)
    set_doc(path, {"status": "running", "startedAt": now(), "editedAt": now()}, ["status", "startedAt", "editedAt"])
    proc = subprocess.Popen(cmd, cwd=cwd, env=profile_env(profile), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True)
    _running[rid] = proc
    if sid and not fork and not child:
        _run_sid[rid] = sid
    proc.stdin.write(user_line(prompt_for(rid, req)))
    proc.stdin.flush()
    live = None
    if sid and not fork and not child:
        live = _live[sid] = {"proc": proc, "rid": rid, "lock": threading.Lock(), "open": True,
                             "browser": browser, "injected": [], "waiting": {}}
    else:
        proc.stdin.close()
    killer = threading.Timer(CONFIG["job_timeout"], lambda: os.killpg(proc.pid, signal.SIGTERM))
    killer.start()
    seen = {"at": time.time(), "answered": False, "idle": False}

    def idle_watch():
        while proc.poll() is None:
            if time.time() - seen["at"] > CONFIG["idle_timeout"]:
                seen["idle"] = True
                log("idle kill", rid[:8], "(after its answer — e.g. a background task left waiting)" if seen["answered"] else "")
                os.killpg(proc.pid, signal.SIGTERM)
                return
            time.sleep(15)
    threading.Thread(target=idle_watch, daemon=True).start()
    parts, new_sid, last_push, result_err = [], sid, 0.0, None
    finished = {"v": False, "lock": threading.Lock(), "timer": None}

    def finish(status, err, cancelled):
        """Turn over: final status + reply, push, suggestions. Runs once — when the process ends, or `linger_grace`
        after claude answered if it stays up only for a background task it started."""
        with finished["lock"]:
            if finished["v"]:
                return
            finished["v"] = True
        if new_sid and not fork and proc.poll() is None:   # claude lives on (background task): show it as not working
            f = next(PROJECTS.glob(f"*/{new_sid}.jsonl"), None)
            if f:
                _answered[new_sid] = f.stat().st_mtime
        set_doc(path, {"reply": redact("\n\n".join(parts)), "resultSessionId": new_sid, "status": status, "error": err or "",
                       "finishedAt": now(), "editedAt": now()},
                ["reply", "resultSessionId", "status", "error", "finishedAt", "editedAt"])
        log("done", rid[:8], status, new_sid[:8] if new_sid else "", "(fork)" if fork else "")
        for irid in (live or {}).get("injected", []):  # their replies are part of this turn's reply and the session history
            if cancelled:
                # Killed before it answered them ("Dừng & gửi" written in just before its cancel landed): run them next instead.
                set_doc(f"clo/{uid}/terminalInbox/{irid}", {"status": "pending", "injectedInto": "", "editedAt": now()},
                        ["status", "injectedInto", "editedAt"])
                _mark(RAN, irid, drop=True)  # this bridge put it back: it may run once more
                log("requeued", irid[:8], "(turn stopped)")
            else:
                set_doc(f"clo/{uid}/terminalInbox/{irid}", {"status": "done", "mergedInto": rid, "reply": "", "editedAt": now()},
                        ["status", "mergedInto", "reply", "editedAt"])
            _injected_ids.discard(irid)
        try:
            last = next((l for l in reversed(redact("\n\n".join(parts)).splitlines()) if l.strip() and not l.startswith("🔧")), "")
            if status != "cancelled" and not fork:   # the user stopped it themselves (stop / "Gửi ngay") — no need to tell them
                psid = new_sid or sid
                ptitle = (unfields(api("GET", f"{BASE}/clo/{uid}/terminalSessions/{psid}") or {}).get("title") or "") if psid else ""
                ptext = (redact(err)[:200] if status == "error" and err else "") or push_summary(redact("\n\n".join(parts)))
                notify(uid, "error" if status == "error" else "input" if last.rstrip().endswith("?") else "done", psid, ptitle, ptext)
            if new_sid and not fork:
                live_update(uid, new_sid, "", "error" if status == "error" else "", False, force=True)
        except Exception as e:
            log("notify failed", e)
        reply = redact("\n\n".join(parts))
        if status == "done" and new_sid and not fork and reply.strip():
            if child and new_sid != sid:
                title_child(new_sid, req)
            parent = children().get(new_sid)
            if parent and not req.get("fromChild"):
                report_to_parent(uid, new_sid, parent, req, reply)
            if CONFIG.get("suggest", True) and not req.get("fromChild"):
                threading.Thread(target=suggest_next, args=(uid, new_sid, req.get("text") or "", reply), daemon=True).start()
        if new_sid and not fork:
            try:
                scan_sessions(uid, force=new_sid)   # the app sees the finished state now, not at the next file write
            except Exception as e:
                log("rescan failed", e)

    try:
        for line in proc.stdout:
            seen["at"] = time.time()
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("session_id"):
                new_sid = ev["session_id"]
                if not sid and not fork:
                    _run_sid[rid] = new_sid   # a new session: its id is known now (a later message can find this run)
                if fork and new_sid != sid:
                    _fork_ids.add(new_sid)  # before the periodic scan can see the file
                if child and new_sid != sid and new_sid not in children():
                    add_child(new_sid, sid)
                if add_dirs and new_sid != sid and not fork:
                    session_dirs(new_sid, add_dirs)
            if ev.get("type") == "system" and ev.get("subtype") == "init":
                remember_slash(ev)
            if ev.get("type") in ("assistant", "user") or ev.get("subtype") == "task_notification":
                if finished["timer"]:   # working again (e.g. a background task it waited on ended): not done yet
                    finished["timer"].cancel()
                    finished["timer"] = None
            if ev.get("type") == "assistant":
                # the last message ended the turn (not a tool call): Claude has answered, whatever runs on after it
                seen["answered"] = (ev.get("message") or {}).get("stop_reason") == "end_turn"
                txt = text_of((ev.get("message") or {}).get("content")).strip()
                if txt:
                    parts.append(txt)
            elif ev.get("type") == "result":
                seen["answered"] = not ev.get("is_error")
                more = bool(live) and flush_waiting(uid, live)   # messages sent meanwhile → one more turn, all at once
                if live and not more:
                    close_input(live)  # nothing waited: claude exits on EOF
                result_err = str(ev.get("result") or ev.get("subtype")) if ev.get("is_error") else None
                if not result_err and not parts and isinstance(ev.get("result"), str) and ev["result"].strip():
                    parts.append(ANSI.sub("", ev["result"]).strip())
                if not more and parts and not finished["v"] and not result_err and not _stopped.get(rid):
                    # Answered. If claude stays up for a background task it started, give it `linger_grace` to go on
                    # (a short wait it continues after); then the turn is done — push now, not 20 min later as an error.
                    def linger():
                        if proc.poll() is None and not _stopped.get(rid):
                            finish("done", None, False)
                            _lingering.add(rid)
                            log("answered", rid[:8], "— claude stays up for a background task; done")
                    finished["timer"] = threading.Timer(CONFIG["linger_grace"], linger)
                    finished["timer"].daemon = True
                    finished["timer"].start()
            if time.time() - last_push > 1.5:
                set_doc(path, {"reply": redact("\n\n".join(parts)), "resultSessionId": new_sid, "editedAt": now()},
                        ["reply", "resultSessionId", "editedAt"])
                last_push = time.time()
        proc.wait()
    finally:
        killer.cancel()
        if live:
            close_input(live)
            if _live.get(sid) is live:
                del _live[sid]
        _running.pop(rid, None)
        _run_sid.pop(rid, None)
    cancelled = proc.returncode in (-signal.SIGTERM, -signal.SIGKILL) or _stopped.pop(rid, False)
    if seen["idle"] and seen["answered"] and not result_err:
        cancelled = False   # stopped only because it sat idle after answering: the turn is done, not an error
        proc.returncode = 0
    elif seen["idle"] and not result_err:
        result_err = f"No reply from Claude for {CONFIG['idle_timeout'] // 60} min — the turn was stopped"
    err = result_err or (None if proc.returncode == 0 or cancelled else (proc.stderr.read()[-1500:] or f"exit {proc.returncode}"))
    status = "cancelled" if cancelled else "error" if err else "done"
    if finished["timer"]:
        finished["timer"].cancel()
    _lingering.discard(rid)
    if finished["v"]:
        log("done", rid[:8], "background work after the answer ended", f"({status})")
        set_doc(path, {"reply": redact("\n\n".join(parts)), "editedAt": now()}, ["reply", "editedAt"])
    else:
        finish(status, err, cancelled)
    if fork:
        # The branch's output lives in the request doc; drop its session file so it never lists as a session.
        if new_sid and new_sid != sid:
            for f in PROJECTS.glob(f"*/{new_sid}.jsonl"):
                f.unlink(missing_ok=True)
            api("DELETE", f"{BASE}/clo/{uid}/terminalSessions/{new_sid}")  # in case a scan raced us
        return
    try:
        scan_sessions(uid, force=new_sid)
    except Exception as e:
        log("rescan failed", e)


CHILD_REPORT = ("📥 Report from child session «{title}» (session {sid}):\n\n{reply}\n\n"
                "(Automatic message: the child session just finished a turn. Take the result into context and reply with one short acknowledgement; "
                "only continue if the main task is waiting for exactly this result.)")


def title_child(sid, req):
    """A fork starts with the parent's history (and title): name it after its own task."""
    task = re.sub(r"(?s)^.*?VIỆC:\s*", "", req.get("text") or "").strip().splitlines()
    title = "↳ " + (task[0] if task else "Child session")[:80]
    path = next(PROJECTS.glob(f"*/{sid}.jsonl"), None)
    if path:
        with open(path, "a") as f:
            f.write(json.dumps({"type": "custom-title", "customTitle": title, "sessionId": sid}, ensure_ascii=False) + "\n")


def report_to_parent(uid, child_sid, parent_sid, req, reply):
    """Sub-session finished a turn (and isn't asking the user) → its result goes to the parent as a message."""
    if asks_user([{"role": "assistant", "text": reply}]):
        return  # waiting for the user: shows under "Cần bạn trả lời" instead
    path = next(PROJECTS.glob(f"*/{child_sid}.jsonl"), None)
    title = (parse_session(path) or {}).get("title", "") if path else ""
    body = reply if len(reply) <= 6000 else "…" + reply[-6000:]
    text = CHILD_REPORT.format(title=title or child_sid[:8], sid=child_sid[:8], reply=body)
    rid = str(uuid.uuid4())
    report = {"text": text, "sessionId": parent_sid, "cwd": req.get("cwd") or str(HOME), "machine": CONFIG["machine"],
              "profile": req.get("profile") or "claude", "browser": False, "fork": False, "fromChild": child_sid}
    m = {k: v for k, v in _load(OWN).items() if isinstance(v, dict) and v.get("t", 0) > time.time() - SIGN_MAX_AGE}
    m[rid] = {"t": time.time(), "req": report, "h": hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest()}
    OWN.write_text(json.dumps(m))
    set_doc(f"clo/{uid}/terminalInbox/{rid}", {**report, "status": "pending", "createdAt": now(), "editedAt": now()})
    log("child report", child_sid[:8], "→", parent_sid[:8])


SUGGEST_PROMPT = """You suggest next steps for a user who is driving Claude Code from their phone.
Below are their last message and Claude Code's last reply. Suggest at most 3 SHORT messages (<= 70 characters) the user is
most likely to send next: an answer to a question/choice Claude just raised, or the next sensible step (check, commit, deploy, fix the rest).
Write as the user would type it, in the same language as the user. No generic suggestions like "thanks". If nothing is worth suggesting, return [].
Print only a JSON array of strings.

TIN NGƯỜI DÙNG:
{ask}

TRẢ LỜI CỦA CLAUDE CODE:
{reply}"""


def suggest_next(uid, sid, ask, reply):
    """2–3 likely next messages after a finished app turn → chips above the composer (shown while turnCount matches)."""
    try:
        reply = "\n".join(l for l in reply.splitlines() if not l.startswith(("🔧", "🖼")))
        prompt = SUGGEST_PROMPT.format(ask=ask[-1500:], reply=reply[-3500:])
        # Bare context (no MCP servers, skills, CLAUDE.md/settings, default system prompt): ~2k tokens instead of the
        # user's full Claude Code context (~250k cached) — about 1/6 of the cost on their plan, same suggestions.
        r = subprocess.run([CONFIG["claude"], "-p", "--output-format", "json", "--no-session-persistence", "--tools", "",
                            "--strict-mcp-config", "--disable-slash-commands", "--setting-sources", "",
                            "--system-prompt", "You suggest the user's next chat messages. Reply with only a JSON array of strings.",
                            "--model", CONFIG.get("suggest_model") or CONFIG.get("subagent_model") or "sonnet"],
                           cwd=str(HOME), env=profile_env("claude"), input=prompt, capture_output=True, text=True, timeout=120)
        text = json.loads(r.stdout).get("result") or ""
        m = re.search(r"\[.*\]", text, re.S)
        items = [redact(str(x)).strip()[:120] for x in (json.loads(m.group(0)) if m else []) if str(x).strip()][:3]
        path = next(PROJECTS.glob(f"*/{sid}.jsonl"), None)
        s = parse_session(path) if path else None
        if not s:
            return
        set_doc(f"clo/{uid}/terminalSessions/{sid}", {"suggestions": items, "suggestFor": len(s["turns"]), "syncedAt": now()},
                ["suggestions", "suggestFor", "syncedAt"])
        log("suggest", sid[:8], len(items))
    except Exception as e:
        log("suggest failed", e)


def stop_session(uid, rid, sid, docs):
    """Kill the session's running turn (busy lock is freed by its worker) and drop turns queued before /stop."""
    stopped = 0
    for d in docs:
        other = d["name"].rsplit("/", 1)[-1]
        q = d["_req"]
        if other == rid or not sid or q.get("sessionId") != sid or q.get("fork") or q.get("child") or (q.get("machine") or "") != CONFIG["machine"]:
            continue
        proc = _running.get(other)
        if proc:
            _stopped[other] = True
            seal(other)
            os.killpg(proc.pid, signal.SIGTERM)
            stopped += 1
        elif other not in _running and q.get("status") == "pending":
            set_doc(f"clo/{uid}/terminalInbox/{other}", {"status": "cancelled", "editedAt": now()}, ["status", "editedAt"])
            stopped += 1
    for other, proc in list(_running.items()):  # running docs aren't in the pending/cancel query
        if proc and _run_sid.get(other) == sid:
            _stopped[other] = True
            seal(other)
            os.killpg(proc.pid, signal.SIGTERM)
            stopped += 1
    log("stop", sid[:8], stopped)
    set_doc(f"clo/{uid}/terminalInbox/{rid}", {"status": "done", "reply": f"Stopped {stopped} turn(s)." if stopped else "No turn is running.",
                                              "editedAt": now()}, ["status", "reply", "editedAt"])


def poll_inbox(uid):
    rows = api("POST", f"{BASE}/clo/{uid}:runQuery", {"structuredQuery": {
        "from": [{"collectionId": "terminalInbox"}],
        "where": {"fieldFilter": {"field": {"fieldPath": "status"}, "op": "IN",
                                  "value": {"arrayValue": {"values": [enc("pending"), enc("cancel")]}}}}}}) or []
    docs = sorted((r["document"] for r in rows if r.get("document")),
                  key=lambda d: str(unfields(d).get("createdAt") or ""))  # queue order = send order
    held = set()  # sessions with an earlier message still waiting: later ones keep their place behind it
    _poll["n"] += 1
    ran = _load(RAN)
    checked = []
    for doc in docs:
        rid = doc["name"].rsplit("/", 1)[-1]
        req = unfields(doc)
        if req.get("status") == "cancel":
            doc["_req"] = req  # stopping a turn needs no signature
        else:
            target = req.get("machine") or ""
            if target != CONFIG["machine"] and not (target == "" and CONFIG["accept_untargeted"]):
                continue  # another Mac's request: that Mac checks it
            if rid in _running or rid in _injecting or rid in _injected_ids:
                doc["_req"] = req
            elif rid in ran:
                refuse(uid, rid, "this request already ran (not running it again)")
                continue
            else:
                try:
                    v = verified(uid, rid, req)
                except Exception as e:  # a bug here must show on the phone, not stall the whole queue silently
                    refuse(uid, rid, f"signature check failed: {e}")
                    continue
                if v is None:
                    refuse(uid, rid, "no valid signature from the app (only messages sent from the owner's app/web run)")
                    continue
                doc["_req"] = v
        checked.append(doc)
    if checked:
        _activity["at"] = time.time()
    docs = checked
    for doc in docs:
        rid = doc["name"].rsplit("/", 1)[-1]
        req = doc["_req"]
        if req.get("kind") == "rename":
            if (req.get("machine") or "") == CONFIG["machine"] and req.get("status") == "pending":
                _mark(RAN, rid)
                rename_session(uid, rid, req)
            continue
        if req.get("kind") == "chat":
            if (req.get("machine") or "") == CONFIG["machine"] and req.get("status") == "pending" and rid not in _running:
                _running[rid] = None
                _mark(RAN, rid)
                threading.Thread(target=run_chat, args=(uid, rid, req), daemon=True).start()
            elif req.get("status") == "cancel" and _running.get(rid):
                _running[rid].kill()
            continue
        if req.get("kind") == "scanOpenLoops":
            if (req.get("machine") or "") == CONFIG["machine"] and req.get("status") == "pending" and rid not in _running \
                    and not restart_holding():
                def loops(rid=rid, req=req):
                    try:
                        scan_open_loops(uid, rid, req)
                    except Exception as e:
                        log("open loops failed", e)
                        set_doc(f"clo/{uid}/terminalInbox/{rid}", {"status": "error", "error": str(e)[:800], "editedAt": now()},
                                ["status", "error", "editedAt"])
                    finally:
                        _running.pop(rid, None)
                _running[rid] = None
                _mark(RAN, rid)
                threading.Thread(target=loops, daemon=True).start()
            continue
        if req.get("kind") == "killProc":
            if (req.get("machine") or "") == CONFIG["machine"] and req.get("status") == "pending":
                _mark(RAN, rid)
                try:
                    kill_proc(uid, rid, req)
                except Exception as e:
                    set_doc(f"clo/{uid}/terminalInbox/{rid}", {"status": "error", "error": str(e)[:500], "editedAt": now()},
                            ["status", "error", "editedAt"])
            continue
        if req.get("kind") == "installSkill":
            if (req.get("machine") or "") == CONFIG["machine"] and req.get("status") == "pending":
                _mark(RAN, rid)
                try:
                    install_skill(uid, rid, req)
                except Exception as e:
                    set_doc(f"clo/{uid}/terminalInbox/{rid}", {"status": "error", "error": str(e)[:500], "editedAt": now()},
                            ["status", "error", "editedAt"])
            continue
        if req.get("status") == "cancel" and rid not in _running and (req.get("machine") or "") not in ("", CONFIG["machine"]):
            continue
        if req.get("status") == "cancel":
            proc = _running.get(rid)
            if proc:
                log("stop", rid[:8])
                _stopped[rid] = True  # app interrupt: report "stopped", not "exit 143"
                seal(rid)
                os.killpg(proc.pid, signal.SIGTERM)
            else:
                set_doc(f"clo/{uid}/terminalInbox/{rid}", {"status": "cancelled", "editedAt": now()}, ["status", "editedAt"])
            continue
        target = req.get("machine") or ""
        if target != CONFIG["machine"] and not (target == "" and CONFIG["accept_untargeted"]):
            continue  # another Mac's job
        if restart_holding():
            continue  # restarting: new turns wait (≤ RESTART_HOLD) for the new process
        sid = req.get("sessionId") or ""
        fork = bool(req.get("fork")) or bool(req.get("child"))
        if (req.get("text") or "").strip().lower() in ("/stop", "/cancel") and req.get("status") == "pending":
            _mark(RAN, rid)
            stop_session(uid, rid, sid, docs)  # Esc for the phone: claude has no /stop, and slash turns run on a fork
            continue
        if rid in _injecting or rid in _injected_ids:
            continue
        if req.get("interrupt") and req.get("status") == "pending" and sid and not fork and rid not in _running:
            # "Stop & send now": stop the session's running turn here, without waiting for the app's separate cancel
            for other, osid in list(_run_sid.items()):
                proc = _running.get(other)
                if osid == sid and proc and other not in _stopped:
                    log("stop", other[:8], "for", rid[:8])
                    _stopped[other] = True
                    seal(other)
                    os.killpg(proc.pid, signal.SIGTERM)
        for other, osid in list(_run_sid.items()):
            proc = _running.get(other)
            if sid and osid == sid and other in _lingering and proc and other not in _stopped and rid not in _running \
                    and not fork and req.get("status") == "pending" and not req.get("kind"):
                # it already answered and only waits on a background task: the new message runs now
                log("stop", other[:8], "(answered, background task still running) for", rid[:8])
                _stopped[other] = True
                os.killpg(proc.pid, signal.SIGTERM)
        if sid and not fork and sid in _busy_sessions and rid not in _running:
            # Session mid-turn: hand the message to the running claude instead of waiting for the turn to end.
            # Slash commands still run as their own turn, after it.
            if sid not in held and not req.get("kind") and not req.get("interrupt") and not (req.get("text") or "").strip().startswith("/") \
                    and wait_for_turn(rid, req, sid):
                continue
            held.add(sid)
        if rid in _running or (sid and not fork and sid in _busy_sessions):
            continue  # one turn at a time per session (side branches run alongside); stays pending until free
        if sid and not fork and not req.get("force") and open_in_terminal(sid):
            continue  # open in a terminal via clo-resume: wait until it exits (or the app sends "run anyway")
        lock = sid if sid and not fork else ""
        if lock:
            req = merge_queued(uid, rid, req, sid, docs)
            _busy_sessions.add(lock)
            (STATE / "busy").mkdir(parents=True, exist_ok=True)
            (STATE / "busy" / lock).write_text(str(os.getpid()))

        def worker(rid=rid, req=req, sid=lock):
            try:
                run_request(uid, rid, req)
            except Exception as e:
                log("request failed", rid[:8], e)
                try:
                    set_doc(f"clo/{uid}/terminalInbox/{rid}", {"status": "error", "error": str(e)[:1500], "editedAt": now()},
                            ["status", "error", "editedAt"])
                except Exception:
                    pass
            finally:
                _running.pop(rid, None)   # also when run_request returned before starting claude (folder not found…)
                _busy_sessions.discard(sid)
                if sid:
                    (STATE / "busy" / sid).unlink(missing_ok=True)
                _wake.set()   # a queued message for this session (e.g. "stop & send now") starts right away
        _running[rid] = None
        _mark(RAN, rid)
        t = threading.Thread(target=worker, daemon=True)
        _workers.append(t)
        t.start()


def merge_queued(uid, rid, req, sid, docs):
    """Several turns waiting for one session → one run (one history re-send instead of N). The others are marked merged."""
    others = []
    for d in docs:
        orid, o = d["name"].rsplit("/", 1)[-1], d["_req"]
        if orid == rid or o.get("status") != "pending" or o.get("sessionId") != sid or o.get("fork") or o.get("child") or o.get("kind"):
            continue
        if (o.get("machine") or "") != (req.get("machine") or "") or (o.get("text") or "").strip().lower() in ("/stop", "/cancel"):
            continue
        if (o.get("text") or "").strip().startswith("/"):
            break  # keep slash commands as their own turn, and everything after them in order
        others.append((orid, o))
    if not others:
        return req
    merged = dict(req)
    merged["text"] = "\n\n".join([req.get("text") or ""] + [o.get("text") or "" for _, o in others]).strip()
    for k in ("images", "fileNames", "fileObjects", "files"):
        merged[k] = (req.get(k) or []) + [x for _, o in others for x in (o.get(k) or [])]
    for orid, _ in others:
        _mark(RAN, orid)
        set_doc(f"clo/{uid}/terminalInbox/{orid}", {"status": "done", "mergedInto": rid, "reply": "", "editedAt": now()},
                ["status", "mergedInto", "reply", "editedAt"])
    log("merged", len(others), "queued turns into", rid[:8])
    return merged


def prune_missing(uid):
    """Session docs of this Mac whose transcript file no longer exists (deleted tests, forks, cleanups)."""
    r = api("GET", f"{BASE}/clo/{uid}/terminalSessions?pageSize=300&mask.fieldPaths=machine") or {}
    for d in r.get("documents", []):
        sid = d["name"].rsplit("/", 1)[-1]
        if unfields(d).get("machine") == CONFIG["machine"] and not any(PROJECTS.glob(f"*/{sid}.jsonl")):
            api("DELETE", f"https://firestore.googleapis.com/v1/{d['name']}")
            _uploaded.pop(sid, None)
            log("pruned missing session", sid[:8])


_projects = {"at": 0, "list": []}
SKIP_DIRS = {"Library", "node_modules", ".Trash", "Applications", "Pictures", "Music", "Movies", ".cache", ".npm", ".git"}


def project_dirs():
    """Git repos up to 3 levels under ~ (for the app's folder picker); rescanned every 10 min."""
    if time.time() - _projects["at"] < 600:
        return _projects["list"]
    found = []

    def walk(d, depth):
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name.lower())
        except OSError:
            return
        for e in entries:
            if not e.is_dir(follow_symlinks=False) or e.name in SKIP_DIRS or (e.name.startswith(".") and e.name != ".claude"):
                continue
            if os.path.isdir(os.path.join(e.path, ".git")):
                found.append(e.path)
            elif depth < 3:
                walk(e.path, depth + 1)

    walk(str(HOME), 1)
    _projects.update(at=time.time(), list=found[:300])
    return _projects["list"]


def recent_cwds():
    """Project folders of this Mac's recent sessions (for "new session" in the app)."""
    seen = []
    for p in sorted(PROJECTS.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:60]:
        try:
            with open(p, errors="replace") as f:
                for line in f:
                    if '"cwd"' in line:
                        cwd = json.loads(line).get("cwd")
                        if cwd and cwd not in seen and os.path.isdir(cwd):
                            seen.append(cwd)
                        break
        except (OSError, ValueError):
            pass
        if len(seen) >= 10:
            break
    return seen


def rename_session(uid, rid, req):
    """Writes Claude Code's own custom-title entry, so the terminal's --resume picker shows it too."""
    sid, title = req.get("sessionId") or "", (req.get("text") or "").strip()[:120]
    path = next(PROJECTS.glob(f"*/{sid}.jsonl"), None) if sid else None
    if not path or not title:
        set_doc(f"clo/{uid}/terminalInbox/{rid}", {"status": "error", "error": "Session not found", "editedAt": now()},
                ["status", "error", "editedAt"])
        return
    with open(path, "a") as f:
        f.write(json.dumps({"type": "custom-title", "customTitle": title, "sessionId": sid}, ensure_ascii=False) + "\n")
    set_doc(f"clo/{uid}/terminalInbox/{rid}", {"status": "done", "editedAt": now()}, ["status", "editedAt"])
    scan_sessions(uid, force=sid)
    log("renamed", sid[:8], title)


# ---------------------------------------------------------------- skills (~/.claude/skills) ↔ app, and between Macs
# Each Mac mirrors its skills to clo/{uid}/skills/{machine}__{name}; the app installs one onto another Mac on request.
# Nothing ignored by .cloignore, no binary/oversized file and no file that looks like it holds a key ever leaves the Mac.
SKILLS = HOME / ".claude/skills"
SKILL_IGNORE_FILE = SKILLS / ".cloignore"
SKILL_IGNORE_DEFAULT = """# CLO skill sync: paths matching these globs (gitignore-style, relative to each skill) never leave this Mac.
# A line ending in / ignores a directory. Files that look like they hold a key are blocked regardless.
.env
.env.*
*.env
*.key
*.pem
*.p8
*.p12
*.pfx
*credential*
*secret*
*token*
service-account*.json
*-sa.json
id_rsa*
.git/
node_modules/
__pycache__/
.venv/
venv/
*.pyc
.DS_Store
"""
SKILL_SKIP = {"synced", ".trash"}  # claude.ai-managed copies and Claude Code's trash
SKILL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SKILL_SECRET = re.compile(SECRET.pattern + r"|(?i:\b[A-Z0-9_]*(?:API_?KEY|SECRET|TOKEN|PASSWORD|PRIVATE_KEY)[A-Z0-9_]*\s*[=:]\s*['\"]?[A-Za-z0-9_\-./+]{16,})")
SKILL_FILE_MAX, SKILL_TOTAL_MAX = 200_000, 600_000
_skill_hash = {}  # doc id → content hash uploaded
_skills = {"at": 0.0}


def skill_ignores():
    if not SKILL_IGNORE_FILE.exists():
        SKILL_IGNORE_FILE.write_text(SKILL_IGNORE_DEFAULT)
    return [l.strip() for l in SKILL_IGNORE_FILE.read_text().splitlines() if l.strip() and not l.lstrip().startswith("#")]


def skill_ignored(rel, is_dir, patterns):
    import fnmatch
    parts = rel.split("/")
    for p in patterns:
        dir_only = p.endswith("/")
        p = p.rstrip("/")
        if dir_only and not is_dir:
            continue
        if fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(parts[-1], p):
            return True
    return False


def read_skill(entry, patterns):
    """→ doc fields for one skill directory, or None if it isn't a skill (no SKILL.md)."""
    root = entry.resolve()
    # View-only when it lives in a Git repo elsewhere (sync that repo instead); other links (e.g. ~/.agents/skills) install as copies.
    linked = (entry.is_symlink() or SKILLS.resolve() not in root.parents) and any((d / ".git").exists() for d in (root, *root.parents))
    if not (root / "SKILL.md").is_file():
        return None
    files, total = [], 0
    for d, dirs, names in os.walk(root):
        rel_d = os.path.relpath(d, root)
        dirs[:] = sorted(x for x in dirs if not skill_ignored(os.path.normpath(os.path.join(rel_d, x)), True, patterns))
        for n in sorted(names):
            rel = os.path.normpath(os.path.join(rel_d, n))
            f = pathlib.Path(d) / n
            if skill_ignored(rel, False, patterns):
                continue
            item = {"path": rel, "size": 0, "exec": False, "text": "", "blocked": ""}
            if f.is_symlink():
                item["blocked"] = "symlink"
            else:
                st = f.stat()
                item["size"], item["exec"] = st.st_size, bool(st.st_mode & 0o111)
                if st.st_size > SKILL_FILE_MAX or total + st.st_size > SKILL_TOTAL_MAX:
                    item["blocked"] = "too large"
                else:
                    try:
                        text = f.read_text()
                    except (UnicodeDecodeError, OSError):
                        text = None
                    if text is None or "\x00" in text:
                        item["blocked"] = "binary file"
                    elif SKILL_SECRET.search(text):
                        item["blocked"] = "looks like it contains a key"
                    else:
                        item["text"] = text
                        total += st.st_size
            files.append(item)
    md = next((x for x in files if x["path"] == "SKILL.md"), {})
    desc = ""
    m = re.match(r"^---\n(.*?)\n---", md.get("text", ""), re.S)
    if m:
        dm = re.search(r"^description:\s*(.+)$", m.group(1), re.M)
        desc = (dm.group(1).strip().strip("'\"") if dm else "")[:400]
    shipped = [(x["path"], x["text"], x["exec"]) for x in files if not x["blocked"]]
    digest = hashlib.sha256(json.dumps(shipped).encode()).hexdigest()[:16]  # what an install copies → equal on both Macs
    state = hashlib.sha256(json.dumps([linked, files], sort_keys=True).encode()).hexdigest()[:16]
    return {"machine": CONFIG["machine"], "label": CONFIG["label"] or CONFIG["machine"], "name": entry.name,
            "description": desc, "linked": linked, "hash": digest, "files": files,
            "blocked": sum(1 for x in files if x["blocked"]), "state": state, "editedAt": now()}


def scan_skills(uid):
    """Mirror this Mac's skills; drop docs of skills removed here."""
    if not SKILLS.is_dir():
        return
    patterns = skill_ignores()
    mine = set()
    for entry in sorted(SKILLS.iterdir()):
        if entry.name.startswith(".") or entry.name in SKILL_SKIP or not SKILL_NAME.match(entry.name) or not entry.is_dir():
            continue
        try:
            doc = read_skill(entry, patterns)
        except Exception as e:
            log("skill read failed", entry.name, e)
            continue
        if not doc:
            continue
        did = f"{CONFIG['machine']}__{entry.name}"
        mine.add(did)
        if _skill_hash.get(did) != doc["state"]:
            set_doc(f"clo/{uid}/skills/{did}", doc)
            _skill_hash[did] = doc["state"]
            log("skill synced", entry.name, f"({doc['blocked']} blocked)" if doc["blocked"] else "")
    r = api("GET", f"{BASE}/clo/{uid}/skills?pageSize=300&mask.fieldPaths=machine") or {}
    for d in r.get("documents", []):
        did = d["name"].rsplit("/", 1)[-1]
        if unfields(d).get("machine") == CONFIG["machine"] and did not in mine:
            api("DELETE", f"https://firestore.googleapis.com/v1/{d['name']}")
            _skill_hash.pop(did, None)
            log("skill removed", did)


def write_skill(src, dest, replace=False):
    """Write a mirrored skill's shipped files to dest via a temp dir → (count, skipped). replace: the old copy goes to
    ~/.claude/skills/.trash; files only this Mac has (.env, keys, ignored or blocked ones) are carried into the new copy."""
    files = [f for f in src.get("files") or [] if not f.get("blocked")]
    if not any(f.get("path") == "SKILL.md" for f in files):
        raise ValueError("SKILL.md is blocked or missing")
    SKILLS.mkdir(parents=True, exist_ok=True)
    tmp = pathlib.Path(tempfile.mkdtemp(prefix=".clo-install-", dir=SKILLS))
    try:
        shipped = set()
        for f in files:
            rel = os.path.normpath(str(f.get("path") or ""))
            if not rel or rel.startswith(("/", "..")) or "/../" in f"/{rel}/":
                raise ValueError(f"unexpected path: {rel}")
            shipped.add(rel)
            out = tmp / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(str(f.get("text") or ""))
            out.chmod(0o755 if f.get("exec") else 0o644)
        if replace:
            old = read_skill(dest, skill_ignores()) or {}
            was_shipped = {x["path"] for x in old.get("files", []) if not x["blocked"]}
            for d, _, names in os.walk(dest):
                for n in names:
                    rel = os.path.normpath(os.path.relpath(os.path.join(d, n), dest))
                    if rel not in shipped and rel not in was_shipped:  # local-only file: keep it
                        (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(os.path.join(d, n), tmp / rel, follow_symlinks=False)
        tmp.chmod(0o755)
        if replace:
            trash = SKILLS / ".trash"
            trash.mkdir(exist_ok=True)
            dest.rename(trash / f"{dest.name}-{time.strftime('%Y%m%d-%H%M%S')}")
        tmp.rename(dest)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return len(files), len(src.get("files") or []) - len(files)


def install_skill(uid, rid, req):
    """Manual install (builds ≤ 57): copy a skill mirrored by another Mac. Never overwrites an existing skill."""
    path = f"clo/{uid}/terminalInbox/{rid}"

    def finish(status, msg):
        set_doc(path, {"status": status, ("reply" if status == "done" else "error"): msg, "editedAt": now()},
                ["status", "reply" if status == "done" else "error", "editedAt"])
        log("skill install", rid[:8], status, msg)
    src_id = str(req.get("skill") or "")
    src = unfields(api("GET", f"{BASE}/clo/{uid}/skills/{urllib.parse.quote(src_id, safe='')}") or {}) if "/" not in src_id else {}
    name = src.get("name") or ""
    if not src or not SKILL_NAME.match(name):
        return finish("error", "Source skill not found")
    if src.get("linked"):
        return finish("error", f"{name} is a link to another folder on {src.get('label')} — sync that source through Git")
    dest = SKILLS / name
    if dest.exists() or dest.is_symlink():
        same = (read_skill(dest, skill_ignores()) or {}).get("hash") == src.get("hash")
        return finish("done" if same else "error",
                      f"{name} already exists on this Mac" + (" (identical)" if same else " with different content — not overwritten"))
    try:
        n, skipped = write_skill(src, dest)
    except Exception as e:
        return finish("error", f"Install failed: {e}")
    _sync_base()[name] = src.get("hash")
    _save_base()
    finish("done", f"Installed {name} ({n} files) into ~/.claude/skills/{name}"
                   + (f" · skipped {skipped} blocked file(s) — add them on this Mac yourself if needed" if skipped else ""))
    _skills["at"] = 0  # mirror the new skill right away


# Auto sync between Macs — three-way, per skill name, against the last content both sides agreed on ("base"):
#   only another Mac has it → install · same content → record base · only the other side changed → update here
#   (only this side changed → the other Mac updates) · both changed → conflict, nothing is overwritten
#   deleted here after syncing → tombstone; the other Macs delete their unchanged copy (into .trash)
# Never touches symlinked skills, skills living in a Git repo (they sync through Git), or names with a conflict.
SKILL_BASE = STATE / "skill-sync.json"
_skill_base = {}


def _sync_base():
    if not _skill_base and SKILL_BASE.exists():
        _skill_base.update(json.loads(SKILL_BASE.read_text() or "{}"))
    return _skill_base


def _save_base():
    STATE.mkdir(parents=True, exist_ok=True)
    SKILL_BASE.write_text(json.dumps(_skill_base, indent=1, sort_keys=True))


def sync_skills(uid):
    base = _sync_base()
    patterns = skill_ignores()
    r = api("GET", f"{BASE}/clo/{uid}/skills?pageSize=300&mask.fieldPaths=name&mask.fieldPaths=machine"
                   "&mask.fieldPaths=hash&mask.fieldPaths=linked&mask.fieldPaths=editedAt") or {}
    remote = {}  # name → newest copy on another Mac
    for d in r.get("documents", []):
        f = unfields(d)
        f["id"] = d["name"].rsplit("/", 1)[-1]
        if f.get("machine") != CONFIG["machine"] and SKILL_NAME.match(f.get("name") or "") and not f.get("linked"):
            if f["name"] not in remote or str(f.get("editedAt")) > str(remote[f["name"]].get("editedAt")):
                remote[f["name"]] = f
    t = api("GET", f"{BASE}/clo/{uid}/skillTombstones?pageSize=300") or {}
    tombs = {x["name"].rsplit("/", 1)[-1]: unfields(x) for x in t.get("documents", [])}
    changed = False

    def local(name):
        p = SKILLS / name
        if not p.exists() and not p.is_symlink():
            return None, None
        doc = read_skill(p, patterns) if p.is_dir() else None
        managed = doc is not None and not p.is_symlink() and not doc["linked"]
        return (doc or {}).get("hash"), managed
    # Deleted here since the last sync → tell the other Macs.
    for name in list(base):
        if local(name)[0] is None and name not in tombs:
            set_doc(f"clo/{uid}/skillTombstones/{name}", {"machine": CONFIG["machine"], "hash": base[name], "deletedAt": now()})
            tombs[name] = {"hash": base[name]}
            log("skill deleted here → tombstone", name)
        if local(name)[0] is None:
            base.pop(name, None)
            changed = True
    for name, tomb in tombs.items():
        h, managed = local(name)
        if h is None:
            continue
        if managed and h == base.get(name):  # unchanged copy of a skill deleted elsewhere
            trash = SKILLS / ".trash"
            trash.mkdir(exist_ok=True)
            (SKILLS / name).rename(trash / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}")
            base.pop(name, None)
            changed = True
            log("skill removed (deleted on another Mac)", name)
        elif name not in base:  # created again / edited here after the delete: it lives on
            api("DELETE", f"{BASE}/clo/{uid}/skillTombstones/{name}")
    for name, src in remote.items():
        if name in tombs:
            continue
        h, managed = local(name)
        want = src.get("hash")
        try:
            if h is None:
                full = unfields(api("GET", f"{BASE}/clo/{uid}/skills/{urllib.parse.quote(src['id'], safe='')}") or {})
                if full.get("hash") != want:
                    continue  # changed mid-read; next pass
                write_skill(full, SKILLS / name)
                log("skill auto-installed", name, "from", src.get("machine"))
            elif h == want:
                if base.get(name) == h:
                    continue
            elif managed and h == base.get(name):
                full = unfields(api("GET", f"{BASE}/clo/{uid}/skills/{urllib.parse.quote(src['id'], safe='')}") or {})
                if full.get("hash") != want:
                    continue
                write_skill(full, SKILLS / name, replace=True)
                log("skill auto-updated", name, "from", src.get("machine"))
            else:
                continue  # this side changed (the other Mac pulls it) or both changed / unmanaged: leave as is
        except Exception as e:
            log("skill sync failed", name, e)
            continue
        base[name] = want
        changed = True
    if changed:
        _save_base()
        _skills["at"] = 0


# ---------------------------------------------------------------- saved links / snippets (clo/{uid}/saved/{id})
# The app, web and `clo-cloud save` add entries; links saved from the phone get their page title filled in here.
_saved = {"at": 0.0}


def page_title(url):
    """og:title or <title> of a public page ("" if it can't be read)."""
    import html as htmllib
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (CLO bridge)"})
        with urllib.request.urlopen(req, timeout=15) as r:
            if "html" not in (r.headers.get("Content-Type") or ""):
                return ""
            page = r.read(400_000).decode("utf-8", "replace")
    except Exception:
        return ""
    for pat in (r'<meta[^>]+property="og:title"[^>]+content="([^"]*)"', r"<title[^>]*>(.*?)</title>"):
        m = re.search(pat, page, re.S | re.I)
        if m and m.group(1).strip():
            return re.sub(r"\s+", " ", htmllib.unescape(m.group(1))).strip()[:300]
    return ""


def enrich_saved(uid):
    r = api("GET", f"{BASE}/clo/{uid}/saved?pageSize=300") or {}
    for d in r.get("documents", []):
        f = unfields(d)
        if f.get("url") and not f.get("title") and not f.get("titleTried"):
            title = page_title(f["url"])
            set_doc(d["name"].split("/documents/", 1)[1], {"title": title, "titleTried": True}, ["title", "titleTried"])
            log("saved title", title[:60] or "(none)")


# ---------------------------------------------------------------- "Việc dở dang": open loops across recent sessions
# CLO's normal chat (app/web): the backend queues {kind: "chat", chatJson: {model, system, messages}} signed, and streams
# this doc's `reply` back as SSE. Runs on the Claude plan (claude -p, no tools, no session), not on Vertex.
CHAT_DIR = STATE / "chat"


def _chat_prompt(messages):
    """Earlier turns as a transcript + the last user message's blocks (text and images) for stream-json input."""
    def text_of_blocks(c):
        if isinstance(c, str):
            return c
        return "\n".join(b.get("text", "") if b.get("type") == "text" else "[image]" for b in c if isinstance(b, dict))
    *history, last = messages or [{"role": "user", "content": ""}]
    blocks = []
    if history:
        lines = [f"{'User' if m.get('role') == 'user' else 'CLO'}: {text_of_blocks(m.get('content'))}" for m in history]
        blocks.append({"type": "text", "text": "<cuoc_tro_chuyen_truoc>\n" + "\n\n".join(lines) + "\n</cuoc_tro_chuyen_truoc>\n\nThe user's new message:"})
    content = last.get("content")
    for b in ([{"type": "text", "text": content}] if isinstance(content, str) else content or []):
        if isinstance(b, dict) and b.get("type") in ("text", "image"):
            blocks.append({k: v for k, v in b.items() if k != "cache_control"})
    return {"type": "user", "message": {"role": "user", "content": blocks}}


def run_chat(uid, rid, req):
    path = f"clo/{uid}/terminalInbox/{rid}"
    reply, last_write = "", 0.0
    try:
        c = json.loads(req.get("chatJson") or "{}")
        model = c.get("model") if c.get("model") in ("claude-opus-5-5", "claude-sonnet-5-5") else "claude-sonnet-5-5"
        CHAT_DIR.mkdir(parents=True, exist_ok=True)  # empty cwd: no project CLAUDE.md
        cmd = [CONFIG["claude"], "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
               "--include-partial-messages", "--no-session-persistence", "--tools", "", "--model", model,
               "--system-prompt", c.get("system") or "You are CLO."]
        set_doc(path, {"status": "running", "editedAt": now()}, ["status", "editedAt"])
        proc = subprocess.Popen(cmd, cwd=str(CHAT_DIR), env=profile_env("claude"), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True)
        _running[rid] = proc
        proc.stdin.write(json.dumps(_chat_prompt(c.get("messages"))) + "\n")
        proc.stdin.close()
        result = None
        for line in proc.stdout:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            d = (e.get("event") or {}).get("delta") or {}
            if e.get("type") == "stream_event" and d.get("type") == "text_delta":
                reply += d.get("text", "")
                if time.time() - last_write > 0.4:
                    last_write = time.time()
                    set_doc(path, {"reply": reply, "editedAt": now()}, ["reply", "editedAt"])
            elif e.get("type") == "result":
                result = e
        proc.wait(timeout=30)
        if result and not result.get("is_error"):
            set_doc(path, {"status": "done", "reply": result.get("result") or reply, "finishedAt": now(), "editedAt": now()},
                    ["status", "reply", "finishedAt", "editedAt"])
        else:
            err = (result or {}).get("result") or ("cancelled" if proc.returncode and proc.returncode < 0 else f"claude exited with code {proc.returncode}")
            set_doc(path, {"status": "error", "error": str(err)[:800], "reply": reply, "editedAt": now()}, ["status", "error", "reply", "editedAt"])
        log("chat", rid[:8], model, len(reply), "chars")
    except Exception as e:
        log("chat failed", rid[:8], e)
        set_doc(path, {"status": "error", "error": str(e)[:800], "editedAt": now()}, ["status", "error", "editedAt"])
    finally:
        _running.pop(rid, None)


# App → inbox {kind: "scanOpenLoops", machine}. This Mac digests its ~20 newest sessions (tail + lines that defer work),
# asks a cheaper model for ranked open loops, and replaces its cards in clo/{uid}/openLoops (started ones are kept).
LOOP_HINT = re.compile(r"(?i)để sau|làm sau|sau này|chưa (làm|xong|test|kiểm|sửa|có)|còn lại|còn thiếu|todo|fixme|tạm thời|"
                       r"tạm bỏ|bỏ dở|chưa xong|next step|follow[- ]?up|later|not yet|pending|known issue|workaround|rủi ro|risk|"
                       r"dead code|deprecated|thiếu test|no tests?|open issue|open /")
LOOP_PROMPT = """You are reviewing the user's recent Claude Code sessions on the machine {label} to find OPEN LOOPS (unfinished work).
Look for: (1) work promised for "later"/next step that was not done; (2) side issues that were mentioned but not handled
(dead code, stale docs, missing tests, security risks, ignored warnings); (3) tasks abandoned midway or ended in an error.
Skip work finished in a later turn of the same session, meaningless test/trial sessions, and questions that were already answered.
Merge duplicates across sessions. At most 12 items, sorted by impact, highest first.

Return ONLY one ```json block containing an array, each element:
{{"title": "<= 80 characters, English, starting with a verb",
  "why": "1-2 sentences: the evidence in the session + why it is worth doing",
  "kind": "deferred" | "side-issue" | "abandoned" | "risk",
  "impact": 1-5,
  "session": "<source session id, exactly as in the data>",
  "prompt": "a self-contained prompt (English) to open a NEW Claude Code session in that session's folder and do this: give the context, relevant files/places if known, the done criteria, and remind it to check the current state first since it may have changed"}}
Use no tools. Do not invent: only report items backed by evidence in the data.

DATA ({n} sessions, newest first):
{digest}"""


def loop_digest(limit=20, per_session=5000):
    files = sorted(PROJECTS.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    out, meta, seen_titles = [], {}, set()
    for p in files:
        if len(meta) >= limit:
            break
        if p.stem in _fork_ids:
            continue
        try:
            s = parse_session(p)
        except Exception:
            continue
        if not s or len(s["turns"]) < 4 or s["cwd"].startswith(("/tmp", "/private/tmp", "/var/folders", "/private/var")):
            continue  # one-shot probes / tests
        same = s["title"][:40]
        if same in seen_titles:
            continue  # scheduled/automated runs repeat the same opening: newest one is enough
        seen_titles.add(same)
        hints = []
        for t in s["turns"][:-8]:
            for line in t["text"].splitlines():
                if LOOP_HINT.search(line) and not line.startswith("🔧") and len(hints) < 25:
                    hints.append(f"[{t['role']}] {line.strip()[:240]}")
        tail = []
        for t in s["turns"][-8:]:
            text = "\n".join(l for l in t["text"].splitlines() if not l.startswith(("🔧", "🖼")))
            tail.append(f"[{t['role']}] {text.strip()[:900]}")
        block = f"### session {p.stem} · {s['title'][:80]} · {s['cwd']} · {s.get('lastAt', '')[:16]}\n"
        if hints:
            block += "Earlier lines hinting at open work:\n" + "\n".join(hints) + "\n"
        block += "End of session:\n" + "\n".join(tail)
        out.append(redact(block[:per_session]))
        meta[p.stem] = {"title": s["title"][:120], "cwd": s["cwd"]}
    return "\n\n".join(out), meta


def scan_open_loops(uid, rid, req):
    path = f"clo/{uid}/terminalInbox/{rid}"
    set_doc(path, {"status": "running", "startedAt": now(), "reply": "Reading recent sessions…", "editedAt": now()},
            ["status", "startedAt", "reply", "editedAt"])
    digest, meta = loop_digest()
    if not meta:
        return set_doc(path, {"status": "done", "reply": "No sessions to scan.", "editedAt": now()}, ["status", "reply", "editedAt"])
    prompt = LOOP_PROMPT.format(label=CONFIG["label"] or CONFIG["machine"], n=len(meta), digest=digest)
    cmd = [CONFIG["claude"], "-p", "--output-format", "json", "--no-session-persistence", "--tools", "",
           "--model", CONFIG.get("loops_model") or CONFIG.get("subagent_model") or "sonnet"]
    log("open loops scan", len(meta), "sessions,", len(prompt) // 1000, "k chars")
    r = subprocess.run(cmd, cwd=str(HOME), env=profile_env("claude"), input=prompt, capture_output=True, text=True, timeout=900)
    try:
        res = json.loads(r.stdout)
        text, cost = res.get("result") or "", res.get("total_cost_usd") or 0
    except ValueError:
        raise RuntimeError((r.stderr or r.stdout or "no output")[-500:])
    m = re.search(r"```(?:json)?\s*(\[.*\])\s*```", text, re.S) or re.search(r"(\[\s*\{.*\}\s*\])", text, re.S)
    items = json.loads(m.group(1)) if m else []
    existing = api("GET", f"{BASE}/clo/{uid}/openLoops?pageSize=300") or {}
    for d in existing.get("documents", []):
        f = unfields(d)
        if f.get("machine") == CONFIG["machine"] and f.get("status") != "started":
            api("DELETE", f"https://firestore.googleapis.com/v1/{d['name']}")
    kept = 0
    for i, it in enumerate(items[:12]):
        sid = str(it.get("session") or "")
        src = meta.get(sid)
        if not src or not it.get("title") or not it.get("prompt"):
            continue  # must point at a real session (and its folder)
        set_doc(f"clo/{uid}/openLoops/{CONFIG['machine']}__{int(time.time())}_{i}", {
            "title": redact(str(it["title"]))[:120], "why": redact(str(it.get("why") or ""))[:600],
            "kind": str(it.get("kind") or "deferred")[:20], "impact": max(1, min(5, int(it.get("impact") or 3))),
            "prompt": redact(str(it["prompt"]))[:4000], "sessionId": sid, "sessionTitle": src["title"], "cwd": src["cwd"],
            "machine": CONFIG["machine"], "label": CONFIG["label"] or CONFIG["machine"], "status": "open", "createdAt": now()})
        kept += 1
    set_doc(path, {"status": "done", "reply": f"{CONFIG['label'] or CONFIG['machine']}: {kept} open loop(s) from {len(meta)} session(s)"
                   + (f" · ~${cost:.2f}" if cost else ""), "finishedAt": now(), "editedAt": now()},
            ["status", "reply", "finishedAt", "editedAt"])
    log("open loops", kept, f"${cost:.2f}" if cost else "")


BRIDGE_DIR = pathlib.Path(__file__).resolve().parent
CLI_TOOLS = ("clo2-artifact", "clo2-secret", "clo2-resume")


def _version():
    r = subprocess.run(["git", "-C", str(BRIDGE_DIR), "log", "-1", "--format=%h %cI"], capture_output=True, text=True)
    return r.stdout.strip()


def link_tools():
    """~/.local/bin/clo2-* → small wrappers running this checkout's tools with this bridge's Python and data folder."""
    bindir = HOME / ".local/bin"
    bindir.mkdir(parents=True, exist_ok=True)
    env = f'CLO2_DIR="{tunnel_mod.DIR}" CLO2_STORE="{CONFIG["store"]}"'
    tools = {"clo2-artifact": f'{env} exec "{sys.executable}" -I "{BRIDGE_DIR / "clo2_tools.py"}" artifact "$@"',
             "clo2-secret": f'{env} exec "{sys.executable}" -I "{BRIDGE_DIR / "clo2_tools.py"}" secret "$@"',
             "clo2-resume": f'exec "{sys.executable}" -I "{BRIDGE_DIR / "clo-resume"}" "$@"',
             "clo2": f'CLO2_PAGE="http://127.0.0.1:{CONFIG["port"]}" exec "{sys.executable}" -I "{BRIDGE_DIR / "clo2_cli.py"}" "$@"'}
    for name, line in tools.items():
        dst = bindir / name
        body = "#!/bin/sh\n# CLO 2 (written by the bridge)\n" + line + "\n"
        if dst.exists() and "# CLO 2 (written by the bridge)" not in dst.read_text(errors="ignore"):
            continue  # someone's own file: leave it
        if not dst.exists() or dst.read_text() != body:
            dst.write_text(body)
            dst.chmod(0o755)
            log("linked", dst)
    if not LINUX:
        mac_launcher()


LAUNCHER_REV = "2"


def mac_launcher():
    """~/Applications/CLO 2.app (Spotlight / Launchpad): opens Terminal with `clo2 pair` (QR to pair a phone) and brings
    the menu bar icon back if it was quit. Also restarts the menu bar agent now — quitting it left no place to approve."""
    uid = os.getuid()
    subprocess.run(["launchctl", "kickstart", f"gui/{uid}/com.clo2.menubar"], capture_output=True, timeout=10)
    app = HOME / "Applications/CLO 2.app"
    mark = app / "Contents/Resources/clo2-launcher"
    if mark.exists() and mark.read_text().strip() == LAUNCHER_REV:
        return
    script = (f'do shell script "launchctl kickstart gui/{uid}/com.clo2.menubar >/dev/null 2>&1; true"\n'
              f'tell application "Terminal" to do script "{HOME}/.local/bin/clo2 pair"\n'
              'tell application "Terminal" to activate')
    try:
        app.parent.mkdir(exist_ok=True)
        shutil.rmtree(app, ignore_errors=True)
        subprocess.run(["/usr/bin/osacompile", "-o", str(app), *[x for line in script.split("\n") for x in ("-e", line)]],
                       capture_output=True, timeout=30, check=True)
        mark.write_text(LAUNCHER_REV)
        log("launcher:", app)
    except Exception as e:
        log("launcher not created:", e)


def self_update():
    """Every few seconds: if this checkout is only behind its upstream and clean, fast-forward; restart (gracefully) when
    bridge/ changed. Dirty / ahead / diverged checkouts are left alone — someone is working in them."""
    git = lambda *a: subprocess.run(["git", "-C", str(BRIDGE_DIR), *a], capture_output=True, text=True, timeout=120)
    if git("fetch", "-q").returncode != 0:
        return
    counts = git("rev-list", "--left-right", "--count", "HEAD...@{u}").stdout.split()
    if len(counts) != 2 or counts[0] != "0" or counts[1] == "0" or git("status", "--porcelain").stdout.strip():
        return
    changed = git("diff", "--name-only", "HEAD", "@{u}").stdout.split()
    if git("pull", "--ff-only", "-q").returncode != 0:
        return
    log("self-update: pulled", counts[1], "commits")
    if any(f.startswith("bridge/") for f in changed):
        link_tools()
        if not RESTART.exists():  # touching an old flag would hold new turns again
            RESTART.touch()  # exits once no turn is running; launchd starts the new code


_slash = {"list": [], "at": 0.0}
_prune = {"at": 0.0}
_orphan = {"at": time.time()}  # first periodic pass a minute after start (startup already recovers)


def probe_slash_commands():
    """Slash commands this Mac's Claude Code offers, read from the init event (killed before any model call)."""
    probe_sid = None
    proc = subprocess.Popen([CONFIG["claude"], "-p", "--output-format", "stream-json", "--verbose"], cwd=str(HOME),
                            env=profile_env("claude"), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, start_new_session=True)
    try:
        proc.stdin.write("hi"); proc.stdin.close()
        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "system" and ev.get("subtype") == "init":
                remember_slash(ev)
                probe_sid = ev.get("session_id")
                break
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
    # The probe may leave its own (empty) session file behind; remove exactly that one.
    if probe_sid:
        _fork_ids.add(probe_sid)
        for f in PROJECTS.glob(f"*/{probe_sid}.jsonl"):
            f.unlink(missing_ok=True)


def remember_slash(init_event):
    cmds = sorted(set(init_event.get("slash_commands") or []))
    if cmds:
        _slash.update(list=cmds, at=time.time())


def recover_orphans(uid, only_untracked=False):
    """Requests left "running" with no live process here (bridge restart/crash, killed child) would stay stuck forever."""
    rows = api("POST", f"{BASE}/clo/{uid}:runQuery", {"structuredQuery": {"from": [{"collectionId": "terminalInbox"}],
               "where": {"fieldFilter": {"field": {"fieldPath": "status"}, "op": "IN",
                                         "value": {"arrayValue": {"values": [enc("running"), enc("injected")]}}}}}}) or []
    for row in rows:
        doc = row.get("document")
        if not doc:
            continue
        req = unfields(doc)
        target = req.get("machine") or ""
        rid = doc["name"].rsplit("/", 1)[-1]
        if only_untracked and (rid in _running or rid in _injected_ids):
            continue  # genuinely running in this process
        if req.get("status") == "injected" and (target == CONFIG["machine"] or (target == "" and CONFIG["accept_untargeted"])):
            set_doc(doc["name"].split("/documents/", 1)[1], {"status": "done", "reply": "", "editedAt": now()},
                    ["status", "reply", "editedAt"])  # claude had it; whatever it answered is in the session history
            continue
        if target == CONFIG["machine"] or (target == "" and CONFIG["accept_untargeted"]):
            set_doc(doc["name"].split("/documents/", 1)[1], {
                "status": "error", "editedAt": now(),
                "error": "Bridge restarted midway — the full result (if any) is in the session history."},
                ["status", "error", "editedAt"])
            log("recovered orphan", doc["name"].rsplit("/", 1)[-1][:8])


# ---------------------------------------------------------------- Claude plan usage (same numbers as Claude Code's /usage)
_usage = {"at": 0.0}


def claude_usage():
    """Plan + rate-limit windows from the cache Claude Code keeps in ~/.claude.json (refreshed by `claude -p /usage`)."""
    try:
        c = json.loads((HOME / ".claude.json").read_text())
    except (OSError, ValueError):
        return None
    acct, cache = c.get("oauthAccount") or {}, c.get("cachedUsageUtilization") or {}
    u = cache.get("utilization") or {}
    if not acct or cache.get("accountUuid") != acct.get("accountUuid") or not u:
        return None
    org = (acct.get("organizationType") or "").replace("claude_", "")
    tier = re.sub(r"^default_(claude_)?", "", acct.get("userRateLimitTier") or "")
    names = {"session": "5h", "weekly_all": "Week"}
    wins = []
    for l in u.get("limits") or []:
        label = names.get(l.get("kind")) or (l.get("model") if l.get("kind") == "weekly_scoped" else None)
        if label and isinstance(l.get("percent"), (int, float)):
            wins.append({"label": label, "left": max(0, round(100 - l["percent"])), "resetsAt": l.get("resets_at") or ""})
    return {"plan": " · ".join(x for x in (org, tier) if x), "windows": wins,
            "fetchedAt": dt.datetime.fromtimestamp(cache.get("fetchedAtMs", 0) / 1000, dt.timezone.utc).isoformat()}


def owner_license():
    """~/.clo2/license: a Pro licence signed with the release key for this Mac's public key (scripts/owner_license.py).
    The app checks the signature and that it names the Mac it is talking to, so it can't be copied to another Mac."""
    try:
        return (tunnel_mod.DIR / "license").read_text().strip()[:2000]
    except OSError:
        return ""


def ram_used_pct():
    """% of this Mac's memory in use, like Activity Monitor's "Memory Used" (app + wired + compressed), or None."""
    if LINUX:   # like `free`: total − available
        try:
            m = {k: int(v.split()[0]) for k, v in (l.split(":", 1) for l in open("/proc/meminfo") if ":" in l)}
            return max(0, min(100, round((m["MemTotal"] - m["MemAvailable"]) * 100 / m["MemTotal"])))
        except Exception:
            return None
    try:
        total = int(subprocess.run(["/usr/sbin/sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5).stdout)
        out = subprocess.run(["/usr/bin/vm_stat"], capture_output=True, text=True, timeout=5).stdout
        page = int(re.search(r"page size of (\d+)", out).group(1))
        n = {k.strip(): int(v.strip().rstrip(".")) for k, v in re.findall(r"^([^:\n]+):\s+(\d+)\.?$", out, re.M)}
        used = (n["Anonymous pages"] - n.get("Pages purgeable", 0) + n["Pages wired down"]
                + n.get("Pages occupied by compressor", 0)) * page
        return max(0, min(100, round(used * 100 / total)))
    except Exception:
        return None


def disk_usage():
    """(% full, GB free) of the volume holding the home folder — a full disk silently breaks builds and updates."""
    try:
        d = shutil.disk_usage(str(HOME))
        return max(0, min(100, round((d.total - d.free) * 100 / d.total))), round(d.free / 1e9, 1)
    except Exception:
        return None, None


_hid = {}


def _hid_fns():
    """IOKit/CoreFoundation calls for the Apple Silicon temperature sensors (the same HID services Stats/macmon read; no
    sudo). Loaded once."""
    if not _hid:
        import ctypes, ctypes.util
        from ctypes import c_void_p as P, c_int32, c_int64, c_long, c_double, c_char_p, c_uint32, c_bool
        io = ctypes.CDLL(ctypes.util.find_library("IOKit"))
        cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
        for f, res, args in [
                (io.IOHIDEventSystemClientCreate, P, [P]), (io.IOHIDEventSystemClientSetMatching, c_int32, [P, P]),
                (io.IOHIDEventSystemClientCopyServices, P, [P]), (io.IOHIDServiceClientCopyEvent, P, [P, c_int64, c_int32, c_int64]),
                (io.IOHIDServiceClientCopyProperty, P, [P, P]), (io.IOHIDEventGetFloatValue, c_double, [P, c_int32]),
                (cf.CFStringCreateWithCString, P, [P, c_char_p, c_uint32]), (cf.CFNumberCreate, P, [P, c_int32, P]),
                (cf.CFDictionaryCreate, P, [P, P, P, c_long, P, P]), (cf.CFArrayGetCount, c_long, [P]),
                (cf.CFArrayGetValueAtIndex, P, [P, c_long]), (cf.CFStringGetCString, c_bool, [P, c_char_p, c_long, c_uint32]),
                (cf.CFRelease, None, [P])]:
            f.restype, f.argtypes = res, args
        _hid.update(ctypes=ctypes, io=io, cf=cf)
    return _hid


def _mac_cpu_temp():
    h = _hid_fns()
    ctypes, io, cf = h["ctypes"], h["io"], h["cf"]
    utf8, owned = 0x08000100, []

    def cfstr(s):
        owned.append(cf.CFStringCreateWithCString(None, s.encode(), utf8))
        return owned[-1]

    def cfnum(v):
        x = ctypes.c_int32(v)
        owned.append(cf.CFNumberCreate(None, 3, ctypes.byref(x)))  # kCFNumberSInt32Type
        return owned[-1]

    keys = (ctypes.c_void_p * 2)(cfstr("PrimaryUsagePage"), cfstr("PrimaryUsage"))
    vals = (ctypes.c_void_p * 2)(cfnum(0xff00), cfnum(5))   # Apple vendor page, temperature sensors
    match = cf.CFDictionaryCreate(None, keys, vals, 2, ctypes.addressof(ctypes.c_void_p.in_dll(cf, "kCFTypeDictionaryKeyCallBacks")),
                                  ctypes.addressof(ctypes.c_void_p.in_dll(cf, "kCFTypeDictionaryValueCallBacks")))
    owned.append(match)
    client = io.IOHIDEventSystemClientCreate(None)
    if not client:
        return None
    owned.append(client)
    io.IOHIDEventSystemClientSetMatching(client, match)
    svcs = io.IOHIDEventSystemClientCopyServices(client)
    temps, product, buf = [], cfstr("Product"), ctypes.create_string_buffer(128)
    try:
        for i in range(cf.CFArrayGetCount(svcs) if svcs else 0):
            sv = cf.CFArrayGetValueAtIndex(svcs, i)
            p = io.IOHIDServiceClientCopyProperty(sv, product)
            name = buf.value.decode(errors="replace") if p and cf.CFStringGetCString(p, buf, 128, utf8) else ""
            if p:
                cf.CFRelease(p)
            if "tdie" not in name:   # "PMU tdie1…", "PMU2 tdie…" = CPU die sensors (others: NAND, battery, tcal…)
                continue
            ev = io.IOHIDServiceClientCopyEvent(sv, 15, 0, 0)   # kIOHIDEventTypeTemperature
            if ev:
                t = io.IOHIDEventGetFloatValue(ev, 15 << 16)
                cf.CFRelease(ev)
                if 0 < t < 150:
                    temps.append(t)
    finally:
        if svcs:
            cf.CFRelease(svcs)
        for o in owned:
            if o:
                cf.CFRelease(o)
    return round(max(temps)) if temps else None


def _linux_cpu_temp():
    temps = []
    for z in pathlib.Path("/sys/class/thermal").glob("thermal_zone*"):
        try:
            kind = (z / "type").read_text().strip().lower()
            if any(k in kind for k in ("x86_pkg", "cpu", "soc", "tdie", "k10temp")):
                temps.append(int((z / "temp").read_text()) / 1000)
        except Exception:
            pass
    if not temps:   # coretemp / k10temp hwmon (no matching thermal zone)
        for hw in pathlib.Path("/sys/class/hwmon").glob("hwmon*"):
            try:
                if (hw / "name").read_text().strip() in ("coretemp", "k10temp", "zenpower"):
                    temps += [int(f.read_text()) / 1000 for f in hw.glob("temp*_input")]
            except Exception:
                pass
    temps = [t for t in temps if 0 < t < 150]
    return round(max(temps)) if temps else None


def cpu_temp():
    """Hottest CPU die sensor in °C (Apple Silicon HID sensors / Linux thermal zones), or None (Intel Mac, VM, no sensor)."""
    try:
        return _linux_cpu_temp() if LINUX else _mac_cpu_temp()
    except Exception as e:
        if not _hid.get("warned"):
            _hid["warned"] = True
            log("cpu temperature unavailable:", e)
        return None


def refresh_usage():
    """`claude -p /usage` is a local command: no model call, no cost; it refreshes the cache read above."""
    try:
        subprocess.run([CONFIG["claude"], "-p", "/usage", "--output-format", "json", "--no-session-persistence"], input="",
                       cwd=str(HOME), env=profile_env("claude"), capture_output=True, timeout=90)
    except Exception as e:
        log("usage refresh failed", e)


def _secret_key():
    try:
        return box_public_key()
    except Exception as e:  # Keychain locked/unavailable: the app just can't seal secrets for this Mac yet
        log("secret key unavailable:", e)
        return ""


def _sign_key():
    try:
        return sign_public_key()
    except Exception as e:
        log("sign key unavailable:", e)
        return ""


# ---------------------------------------------------------------- claude processes on this Mac
# Heartbeat lists every live `claude` (CLO turns, Terminal sessions, background agents) so the app can show them per Mac
# and stop one: inbox {kind: "killProc", machine, text: '{"pid": …, "started": …}'} (signed like any request).
PS_START = "%a %b %d %H:%M:%S %Y"


def claude_procs():
    out = subprocess.run(["ps", "-axo", "pid=,ppid=,tty=,lstart=,pcpu=,command="], capture_output=True, text=True, timeout=10).stdout
    procs = []
    for line in out.splitlines():
        f = line.split(None, 9)
        if len(f) < 10:
            continue
        cmd = f[9]
        argv = cmd.split()
        if os.path.basename(argv[0]) != "claude" or "--no-session-persistence" in argv:
            continue  # usage probes / one-shot chat replies aren't sessions
        try:
            started = dt.datetime.strptime(" ".join(f[3:8]), PS_START).timestamp()
        except ValueError:
            continue
        sid = next((argv[i + 1] for i, a in enumerate(argv[:-1]) if a in ("--resume", "-r", "--session-id")), "")
        procs.append({"pid": int(f[0]), "tty": "" if f[2] in ("??", "?") else f[2], "started": int(started), "cpu": float(f[8]),
                      "sid": sid, "args": cmd[:300]})
    if not procs:
        return []
    cwd = {}
    for p in procs if LINUX else []:
        try:
            cwd[p["pid"]] = os.readlink(f"/proc/{p['pid']}/cwd")
        except OSError:
            pass
    lo = "" if LINUX else subprocess.run(["/usr/sbin/lsof", "-a", "-d", "cwd", "-Fpn", "-p", ",".join(str(p["pid"]) for p in procs)],
                        capture_output=True, text=True, timeout=10).stdout
    pid = None
    for l in lo.splitlines():
        if l[:1] == "p":
            pid = int(l[1:])
        elif l[:1] == "n" and pid:
            cwd[pid] = l[1:]
    clo = {p.pid: rid for rid, p in _running.items() if p}
    taken = set()
    for p in sorted(procs, key=lambda p: -p["started"]):
        p["cwd"] = cwd.get(p["pid"], "")
        p["source"] = "clo" if p["pid"] in clo else ("terminal" if p["tty"] else "background")
        p["request"] = clo.get(p["pid"], "")
        d = PROJECTS / re.sub(r"[^A-Za-z0-9]", "-", p["cwd"])
        files = sorted(d.glob("*.jsonl"), key=lambda x: -x.stat().st_mtime) if p["cwd"] and d.is_dir() else []
        if not p["sid"]:  # interactive session: newest transcript in its folder written since it started, not claimed yet
            hit = next((x for x in files if x.stem not in taken and x.stat().st_mtime >= p["started"] - 5), None)
            p["sid"] = hit.stem if hit else ""
        taken.add(p["sid"])
        t = next(PROJECTS.glob(f"*/{p['sid']}.jsonl"), None) if p["sid"] else None  # cwd may have moved since (cd)
        p["lastActive"] = int(t.stat().st_mtime) if t else p["started"]
    return sorted(procs, key=lambda p: -p["lastActive"])


def kill_proc(uid, rid, req):
    path = f"clo/{uid}/terminalInbox/{rid}"
    try:
        want = json.loads(req.get("text") or "{}")
        pid, started = int(want["pid"]), int(want["started"])
    except (ValueError, KeyError, TypeError):
        return set_doc(path, {"status": "error", "error": "missing pid", "editedAt": now()}, ["status", "error", "editedAt"])
    live = next((p for p in claude_procs() if p["pid"] == pid), None)
    if not live or abs(live["started"] - started) > 2:  # gone, or the pid now belongs to something else
        return set_doc(path, {"status": "done", "reply": "The process had already ended.", "editedAt": now()},
                       ["status", "reply", "editedAt"])
    own = live["request"]
    if own and _running.get(own):  # a CLO turn: stop it the way the app's stop button does (reports "Đã dừng")
        _stopped[own] = True
        seal(own)
        os.killpg(_running[own].pid, signal.SIGTERM)
    else:
        os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            time.sleep(0.1)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
        else:
            os.kill(pid, signal.SIGKILL)
    log("killProc", pid, live["cwd"], live["source"])
    set_doc(path, {"status": "done", "reply": f"Stopped claude (pid {pid}).", "editedAt": now()}, ["status", "reply", "editedAt"])


def _procs_safe():
    try:
        return claude_procs()
    except Exception as e:
        log("claude procs failed:", e)
        return []


def heartbeat(uid):
    disk_pct, disk_free = disk_usage()
    set_doc(f"clo/{uid}/bridge/{CONFIG['machine']}", {"machine": CONFIG["machine"], "label": CONFIG["label"] or CONFIG["machine"],
            "lastSeen": now(), "cwds": recent_cwds(), "projects": project_dirs(), "slashCommands": _slash["list"],
            "profiles": ["claude-yolo", "claude"], "home": str(HOME), "access": tunnel_mod.setting("access"),
            "version": _version(), "usage": claude_usage() or {}, "ramUsed": ram_used_pct(), "cpuTemp": cpu_temp(),
            "diskUsed": disk_pct, "diskFree": disk_free, "license": owner_license(), "secretKey": _secret_key(), "signKey": _sign_key(),
            # restart.sh waiting for running turns: new ones are held until then (the app says why they wait)
            "restarting": RESTART.exists(), "runningCount": len([r for r in _running if r not in _injected_ids]), "tools": [t for t in CLI_TOOLS if (HOME / ".local/bin" / t).exists()],
            "procs": _procs_safe(),
            "features": ["inject", "skills", "openLoops", "saved", "child", "suggest", "effort", "environments", "signed", "secrets", "chat", "procs", "changes", "secretFetch"]})  # app only sends kinds a bridge understands


def self_update_loop():
    """Own thread, ~10 s: the other Mac's pushes land here fast (two Macs coding one repo) without a slow fetch ever
    stalling the inbox."""
    while True:
        time.sleep(CONFIG.get("self_update_every", 10))
        try:
            self_update()
        except Exception as e:
            log("self-update failed:", e)


def git_checkout():
    return subprocess.run(["git", "-C", str(BRIDGE_DIR), "rev-parse", "--git-dir"], capture_output=True).returncode == 0


def release_update_loop():
    """One-line installs: a signed release with a higher seq → files swapped in place, then a graceful restart."""
    time.sleep(CONFIG.get("update_first", 120))
    while True:
        try:
            if updater.check(BRIDGE_DIR, log):
                link_tools()
                subprocess.run(["bash", str(BRIDGE_DIR / "install.sh"), "menubar"], capture_output=True, timeout=600)
                if not RESTART.exists():
                    RESTART.touch()   # exits once no turn is running; launchd starts the new code
        except Exception as e:
            log("update check failed:", e)
        time.sleep(CONFIG["update_every"])


def heartbeat_loop(uid):
    """Own thread: the app's online dot must not depend on session scanning or inbox polling succeeding."""
    started, beats = time.time(), 0
    while True:
        if time.time() - _usage["at"] > CONFIG.get("usage_every", 300):
            _usage["at"] = time.time()
            refresh_usage()
        try:
            heartbeat(uid)
            beats += 1
            if _TRIAL == "trial" and beats >= 3 and time.time() - started > 120:   # a new release ran healthy: keep it
                _trial.ok()
                log("release trial passed")
                globals()["_TRIAL"] = None
        except Exception as e:
            log("heartbeat failed:", e)
        time.sleep(CONFIG["scan_every"])


# ---------------------------------------------------------------- CLO 2 tunnel ops (things that aren't store documents)
UPLOAD_MAX = 100 * 1024 * 1024   # per file: at ~65 KB/s through the relay that's already ~25 min
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{6,64}$")


def upload_name(name):
    name = pathlib.Path(str(name or "")).name.lstrip(".").replace("\x00", "")[:120]
    return name or "file"


def op_upload(req, dev):
    """A file from the phone, in order: {rid, name, index, total, data(b64)} → ~/.clo2-uploads/<rid>/<name>."""
    rid, name = str(req.get("rid") or ""), upload_name(req.get("name"))
    idx, total = int(req.get("index", 0)), int(req.get("total", 1))
    if not SAFE_ID.match(rid) or not (0 <= idx < total <= 4096):
        return 400, {"error": "bad upload"}
    updir = HOME / ".clo2-uploads" / rid
    updir.mkdir(parents=True, exist_ok=True)
    part = updir / (name + ".part")
    chunk = base64.b64decode(req.get("data") or "")
    offset = req.get("offset")   # sent by clients that retry a chunk: makes a resend after a lost reply harmless
    if idx > 0 and isinstance(offset, int):
        have = part.stat().st_size if part.exists() else -1
        final = updir / name
        if have == offset + len(chunk) or (have < 0 and idx == total - 1 and final.exists() and final.stat().st_size == offset + len(chunk)):
            return 200, ({"ok": True, "path": str(final), "size": final.stat().st_size} if have < 0 else {"ok": True})   # already have it
        if have != offset:
            return 409, {"error": "upload out of order"}
    if idx == 0:
        part.unlink(missing_ok=True)
    elif not part.exists() or part.stat().st_size == 0:
        return 409, {"error": "upload out of order"}
    if (part.stat().st_size if part.exists() else 0) + len(chunk) > UPLOAD_MAX:
        part.unlink(missing_ok=True)
        return 413, {"error": "File too large (max 100 MB)"}
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "ab") as f:
        f.write(chunk)
    if idx == total - 1:
        os.replace(part, updir / name)
        return 200, {"ok": True, "path": str(updir / name), "size": (updir / name).stat().st_size}
    return 200, {"ok": True}


def op_artifact(req, dev):
    html = clo2_tools.artifact_html(str(req.get("aid") or ""))
    return (200, {"html": html}) if html is not None else (404, {"error": "This artifact isn't on this Mac"})


def op_secret_set(req, dev):
    try:
        clo2_tools.secret_set(str(req.get("name") or ""), req.get("value"), str(req.get("label") or ""))
    except ValueError as e:
        return 400, {"error": str(e)}
    refresh_local_secrets()
    log("secret saved", req.get("name"), "from", dev.get("name"))   # the name only, never the value
    return 200, {"ok": True}


def op_secret_delete(req, dev):
    clo2_tools.secret_delete(str(req.get("name") or ""))
    refresh_local_secrets()
    return 200, {"ok": True}


def op_secret_reveal(req, dev):
    v = clo2_tools.secret_get(str(req.get("name") or ""))
    return (200, {"value": v}) if v is not None else (404, {"error": "This secret doesn't exist"})


def refresh_local_secrets():
    """Values the transcript mirror scrubs (‹secret NAME›) even if a command printed one."""
    try:
        _secret_values["values"] = {n: v for n, v in clo2_tools.secret_values().items() if v and len(v) >= 6}
    except Exception as e:
        log("secrets: can't read the vault:", e)
    _secret_values["at"] = time.time()


SECRET_REF = re.compile(r"\{\{secret:([A-Za-z0-9_.-]{1,64})\}\}")
FETCH_MAX = 8 * 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):   # a redirect would carry the key's headers to wherever it points: hand the 3xx back instead
        return None


def _public_host(host):
    """Only real internet hosts: a page must not aim a key (or this Mac) at localhost / the LAN."""
    import ipaddress
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            return False
    return bool(infos)


def op_secret_fetch(req, dev):
    """An artifact's API call with a vault secret ({{secret:NAME}} in url/headers/body, or `secret` → Bearer). The phone
    asked the user first (per page, secret and host). The value is added here and never leaves the Mac: not in the page,
    on the phone or through the relay; it is also scrubbed from the answer."""
    url, method = str(req.get("url") or ""), str(req.get("method") or "GET").upper()
    headers = {str(k): str(v) for k, v in (req.get("headers") or {}).items()} if isinstance(req.get("headers"), dict) else {}
    body = base64.b64decode(req.get("body") or "") if req.get("body") else None
    u = urllib.parse.urlsplit(url)
    test_http = os.environ.get("CLO2_TEST_FETCH_HTTP") == "1" and u.scheme == "http" and u.hostname == "127.0.0.1"
    if not test_http and (u.scheme != "https" or not u.hostname or not _public_host(u.hostname)):
        return 400, {"error": "Only https:// addresses on the internet can be called with a secret"}
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"):
        return 400, {"error": "bad method"}
    names = set(SECRET_REF.findall(url)) | {n for v in headers.values() for n in SECRET_REF.findall(v)}
    if body:
        names |= set(SECRET_REF.findall(body.decode("utf-8", "replace")))
    bearer = str(req.get("secret") or "")
    if bearer:
        names.add(bearer)
    if not names:
        return 400, {"error": "No secret named in this request"}
    values = {}
    for n in names:
        v = clo2_tools.secret_get(n)
        if v is None:
            return 404, {"error": f"No secret {n} on this Mac — add it in Vibeclo › Secrets"}
        values[n] = v
    sub = lambda t: SECRET_REF.sub(lambda m: values[m.group(1)], t)
    used_ref = any(SECRET_REF.search(x) for x in [url, *headers.values()]) or (body and SECRET_REF.search(body.decode("utf-8", "replace")))
    url, headers = sub(url), {k: sub(v) for k, v in headers.items()}
    if body:
        body = sub(body.decode("utf-8", "surrogateescape")).encode("utf-8", "surrogateescape")
    if bearer and not used_ref and not any(k.lower() == "authorization" for k in headers):
        headers["Authorization"] = "Bearer " + values[bearer]
    headers.setdefault("User-Agent", "Vibeclo")
    r = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        resp = urllib.request.build_opener(_NoRedirect).open(r, timeout=30)
    except urllib.error.HTTPError as e:
        resp = e
    except Exception as e:
        log("secret fetch", ",".join(sorted(names)), "→", u.hostname, "failed:", type(e).__name__)
        return 502, {"error": f"Couldn't reach {u.hostname}: {e}".replace(url, u.hostname or "")}
    data = resp.read(FETCH_MAX + 1)
    if len(data) > FETCH_MAX:
        return 413, {"error": "The answer is larger than 8 MB"}
    out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in ("set-cookie", "content-encoding", "transfer-encoding", "content-length")}
    for v in values.values():   # an API that echoes the request back must not hand the key to the page
        data = data.replace(v.encode(), b"[secret]")
        out_headers = {k: h.replace(v, "[secret]") for k, h in out_headers.items()}
    log("secret fetch", ",".join(sorted(names)), method, u.hostname, "→", resp.status, "for", dev.get("name"))   # names only
    return 200, {"status": resp.status, "headers": out_headers, "body": base64.b64encode(data).decode()}


TUNNEL_OPS = {"upload": op_upload, "artifact": op_artifact, "secret.set": op_secret_set,
              "secret.delete": op_secret_delete, "secret.reveal": op_secret_reveal, "secret.fetch": op_secret_fetch}


def main():
    import faulthandler
    faulthandler.register(signal.SIGUSR1, all_threads=True)   # `kill -USR1 <pid>`: thread dump in the log when stuck
    once = "--once" in sys.argv
    uid = None
    last_scan = 0.0
    log("CLO 2 bridge on", CONFIG["machine"], "claude:", CONFIG["claude"])
    STATE.mkdir(parents=True, exist_ok=True)
    localstore.open_store(CONFIG["store"])
    if not once:
        def device_write(method, url, body=None):
            r = localstore.handle(method, url, body)
            if method != "GET" and "terminalInbox" in url:
                _wake.set()   # a new message / stop from a device: don't wait for the next poll
            return r
        tun = tunnel_mod.Tunnel(CONFIG["relay"], CONFIG.get("label") or CONFIG["machine"], device_write, log, ops=TUNNEL_OPS,
                                lan_port=CONFIG.get("lan_port", 7789), ts_web=CONFIG.get("ts_web", 7779),
                                web=CONFIG.get("web", "https://clo.sakura5.app"))
        tun.on_push_result = on_push_result
        localstore.listeners.append(tun.changed)   # devices hear about session/inbox writes instead of polling
        _tunnel["t"] = tun
        threading.Thread(target=tun.run, daemon=True).start()
        pairpage.serve(tun, CONFIG["port"], log, CONFIG.get("web", "https://clo.sakura5.app"))
    RESTART.unlink(missing_ok=True)  # this is the restarted process
    try:
        if CONFIG.get("link_tools", True):
            link_tools()
    except OSError as e:
        log("link tools failed", e)
    (STATE / "graceful").write_text(str(os.getpid()))  # restart.sh: this process honours the restart flag
    while True:
        try:
            if not uid:
                uid = os.environ.get("CLO_UID") or lookup_uid()
                log("uid", uid)
                if not once:
                    threading.Thread(target=heartbeat_loop, args=(uid,), daemon=True).start()
                    if git_checkout():
                        if CONFIG.get("self_update"):
                            threading.Thread(target=self_update_loop, daemon=True).start()
                    elif CONFIG.get("auto_update", True):
                        threading.Thread(target=release_update_loop, daemon=True).start()
                recover_orphans(uid)
            if time.time() - _slash["at"] > 3600:
                try:
                    probe_slash_commands()
                except Exception as e:
                    log("slash probe failed:", e)
                    _slash["at"] = time.time() - 3000  # retry in ~10 min
            if time.time() - _orphan["at"] > 60:
                _orphan["at"] = time.time()
                try:
                    recover_orphans(uid, only_untracked=True)
                except Exception as e:
                    log("orphan check failed:", e)
            if time.time() - _prune["at"] > 120:
                _prune["at"] = time.time()
                try:
                    prune_missing(uid)
                except Exception as e:
                    log("prune failed:", e)
            if time.time() - _skills["at"] > 60:
                _skills["at"] = time.time()
                try:
                    scan_skills(uid)   # list this Mac's ~/.claude/skills for the app (CLO 2: no cross-Mac sync)
                    if CONFIG.get("cloud_extras"):
                        sync_skills(uid)
                    if _skills["at"] == 0:
                        scan_skills(uid)  # mirror what sync just installed/updated
                        _skills["at"] = time.time()
                except Exception as e:
                    log("skill scan failed:", e)
            if time.time() - _secret_values["at"] > 120:
                refresh_local_secrets()
            if CONFIG.get("cloud_extras") and (time.time() - _secret_values["at"] > 300 or registry_changed(uid)):  # new device approved → re-seal within ~20 s
                try:
                    refresh_secret_values(uid)
                except Exception as e:
                    log("secrets refresh failed:", e)
            if CONFIG.get("cloud_extras") and time.time() - _saved["at"] > 60:
                _saved["at"] = time.time()
                try:
                    enrich_saved(uid)
                except Exception as e:
                    log("saved enrich failed:", e)
            if time.time() - last_scan > CONFIG["scan_every"]:
                scan_sessions(uid)
                if once:
                    heartbeat(uid)
                last_scan = time.time()
            else:
                scan_sessions(uid, limit=8)  # live activity of sessions being worked on
            poll_inbox(uid)
            if RESTART.exists() and not _running and not _injecting and not _busy_sessions:
                RESTART.unlink(missing_ok=True)
                log("restart requested — no turn running, exiting")
                os._exit(0)  # KeepAlive starts the updated bridge
        except Exception as e:
            log("error:", e)
            time.sleep(10)
        if once:
            for t in _workers:
                t.join()
            return
        # Each inbox query costs a Firestore read even when empty: 3 s while something happened lately, 6 s when idle.
        _wake.wait(CONFIG["poll_every"] if (_running or time.time() - _activity["at"] < 600) else CONFIG.get("idle_poll_every", 6))
        _wake.clear()


if __name__ == "__main__":
    main()
