"""Themes the user vibecodes ("ottoman + japanese vibes"): Claude Code on this Mac writes a theme, the bridge checks it and
keeps it in ~/.clo2/themes.json, and every paired phone / browser can pick it.

A theme is DATA, never code for the app: colors, a font family, a background (gradient + a glyph pattern), and falling
particles from a fixed set of effects. The web may also take an SVG background tile and a little extra CSS (no scripts,
no outside URLs). Everything is checked here, so a client can apply what it gets without trusting the model's output.
"""
import json
import re
import secrets
import subprocess
import threading
import time
from pathlib import Path

KEYS = ("bg", "surface", "bubble", "border", "text", "muted", "accent", "code")
FONTS = ("sans", "serif", "rounded", "mono")
PARTICLES = ("none", "snow", "petals", "leaves", "stars", "bubbles", "embers", "confetti", "glyphs")
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
MAX_THEMES = 40
_lock = threading.Lock()

PROMPT = """You design a color theme for Vibeclo, a chat app (iPhone app + web) for driving Claude Code. The user asked:

<request>
{ask}
</request>
{base}
Reply with ONLY one JSON object, no prose, no code fence:
{{
  "name": "2-4 word theme name",
  "icon": "one emoji",
  "light": {{"bg": "#RRGGBB", "surface": "#RRGGBB", "bubble": "#RRGGBB", "border": "#RRGGBB",
            "text": "#RRGGBB", "muted": "#RRGGBB", "accent": "#RRGGBB", "code": "#RRGGBB"}},
  "dark":  {{ same 8 keys for dark mode }},
  "font": "sans" | "serif" | "rounded" | "mono",
  "background": {{
    "gradient": ["#RRGGBB", "#RRGGBB"],   // optional, light mode: 2-3 soft colors near light.bg (low contrast)
    "gradientDark": ["#RRGGBB", "#RRGGBB"],   // with gradient: the same wash for dark mode, near dark.bg
    "angle": 0-360,
    "pattern": {{"glyphs": ["✿", "◆"], "size": 10-64, "opacity": 0.02-0.25, "gap": 24-160}},  // optional tiled motif
    "svg": "<svg ...>...</svg>"   // optional, web only: a small seamless tile (<= 6 KB, no <script>, no links)
  }},
  "particles": {{"kind": "none|snow|petals|leaves|stars|bubbles|embers|confetti|glyphs", "count": 0-160,
                 "speed": 0.2-3, "colors": ["#RRGGBB"], "glyphs": ["🌸"]}},
  "css": ""   // optional, web only: a few extra CSS rules using the variables --bg --surface --bubble --border --text --muted --accent --code; no url(), no @import
}}
Rules: body text must stay easy to read (contrast >= 7:1 for text on bg and on surface, >= 4.5:1 for muted). bubble is
the user's chat bubble, surface cards/inputs, code the code block background, accent buttons/links (white text on it
must read). Keep backgrounds calm: the motif is a hint, not wallpaper. Use the request's mood in colors, motif, particles
and name. Omit optional parts you don't need."""


def path(state: Path) -> Path:
    return state / "themes.json"


def load(state: Path) -> list:
    try:
        v = json.loads(path(state).read_text())
        return v if isinstance(v, list) else []
    except (OSError, ValueError):
        return []


def _write(state: Path, themes: list):
    p = path(state)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(themes, ensure_ascii=False))
    tmp.replace(p)


def _num(v, lo, hi, d):
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return d


def _hexes(v, n):
    return [c for c in (v if isinstance(v, list) else []) if isinstance(c, str) and HEX.match(c)][:n]


def _glyphs(v, n=4):
    out = []
    for g in v if isinstance(v, list) else []:
        g = str(g).strip()
        if g and len(g) <= 4 and not re.search(r"[<>\"'`\\]", g):
            out.append(g)
    return out[:n]


def _svg(s):
    s = str(s or "").strip()
    if not s.startswith("<svg") or len(s) > 6144:
        return ""
    low = s.lower()
    if any(x in low for x in ("<script", "javascript:", "<foreignobject", "href=\"http", "href='http", "@import", "<iframe")) \
            or re.search(r"\son[a-z]+\s*=", low):
        return ""
    return s


def _css(s):
    s = str(s or "")[:4000]
    low = s.lower()
    if any(x in low for x in ("url(", "@import", "expression(", "</style", "javascript:", "behavior:", "-moz-binding")):
        return ""
    return s


def clean(t: dict) -> dict:
    """The model's (or a client's) theme → a safe, complete one. Raises ValueError when the colors are unusable."""
    if not isinstance(t, dict):
        raise ValueError("not a theme")
    out = {"v": 1, "name": str(t.get("name") or "My theme").strip()[:40],
           "icon": (_glyphs([t.get("icon")], 1) or ["🎨"])[0]}
    for mode in ("light", "dark"):
        src = t.get(mode) if isinstance(t.get(mode), dict) else {}
        cols = {k: src[k] for k in KEYS if isinstance(src.get(k), str) and HEX.match(src[k])}
        if len(cols) < 7:
            raise ValueError(f"the theme's {mode} colors are incomplete")
        cols.setdefault("code", cols.get("surface") or cols["bg"])
        for k in KEYS:
            cols.setdefault(k, cols["bg"])
        out[mode] = {k: cols[k].upper() for k in KEYS}
    out["font"] = t.get("font") if t.get("font") in FONTS else "sans"
    bg = t.get("background") if isinstance(t.get("background"), dict) else {}
    b = {}
    if len(_hexes(bg.get("gradient"), 3)) >= 2:
        b["gradient"] = [c.upper() for c in _hexes(bg.get("gradient"), 3)]
        b["angle"] = int(_num(bg.get("angle"), 0, 360, 160))
        dk = _hexes(bg.get("gradientDark"), 3)
        b["gradientDark"] = [c.upper() for c in dk] if len(dk) >= 2 else [out["dark"]["bg"], out["dark"]["surface"]]
    pat = bg.get("pattern") if isinstance(bg.get("pattern"), dict) else {}
    if _glyphs(pat.get("glyphs")):
        b["pattern"] = {"glyphs": _glyphs(pat.get("glyphs")), "size": round(_num(pat.get("size"), 10, 64, 22)),
                        "opacity": round(_num(pat.get("opacity"), 0.02, 0.25, 0.08), 3),
                        "gap": round(_num(pat.get("gap"), 24, 160, 64))}
    if _svg(bg.get("svg")):
        b["svg"] = _svg(bg.get("svg"))
    out["background"] = b
    p = t.get("particles") if isinstance(t.get("particles"), dict) else {}
    kind = p.get("kind") if p.get("kind") in PARTICLES else "none"
    glyphs = _glyphs(p.get("glyphs"))
    if kind == "glyphs" and not glyphs:
        kind = "none"
    out["particles"] = {"kind": kind, "count": int(_num(p.get("count"), 0, 160, 50)),
                        "speed": round(_num(p.get("speed"), 0.2, 3, 1), 2), "colors": [c.upper() for c in _hexes(p.get("colors"), 4)],
                        "glyphs": glyphs}
    if _css(t.get("css")):
        out["css"] = _css(t.get("css"))
    return out


def parse(text: str) -> dict:
    """The JSON object in Claude's reply (it may wrap it in a fence or add // comments)."""
    s = str(text or "")
    a, z = s.find("{"), s.rfind("}")
    if a < 0 or z <= a:
        raise ValueError("Claude didn't return a theme")
    body = s[a:z + 1]
    try:
        return json.loads(body)
    except ValueError:
        body = re.sub(r"(?m)^\s*//.*$|(?<=[,{\[\s])//[^\n\"]*$", "", body)
        body = re.sub(r",\s*([}\]])", r"\1", body)
        return json.loads(body)


def make(ask, base, run_claude):
    """Ask Claude for a theme (run_claude(prompt) → text). base = the theme being changed ("darker", "add petals")."""
    ask = str(ask or "").strip()[:800]
    if not ask:
        raise ValueError("Describe the theme you want")
    btxt = ""
    if isinstance(base, dict) and base.get("light"):
        b = {k: v for k, v in clean(base).items() if k != "v"}
        btxt = ("\nChange this current theme as asked; keep what the request doesn't mention:\n<current>\n"
                + json.dumps(b, ensure_ascii=False) + "\n</current>\n")
    t = clean(parse(run_claude(PROMPT.format(ask=ask, base=btxt))))
    t["prompt"] = ask
    return t


def save(state: Path, t: dict, tid: str = "") -> dict:
    t = {**clean(t), "prompt": str(t.get("prompt") or "")[:800]}
    with _lock:
        themes = load(state)
        old = next((x for x in themes if x.get("id") == tid), None) if tid else None
        t["id"] = tid if old else "t" + secrets.token_hex(5)
        t["createdAt"] = (old or {}).get("createdAt") or int(time.time())
        t["updatedAt"] = int(time.time())
        themes = [x for x in themes if x.get("id") != t["id"]]
        themes.insert(0, t)
        _write(state, themes[:MAX_THEMES])
    return t


def delete(state: Path, tid: str) -> bool:
    with _lock:
        themes = load(state)
        keep = [x for x in themes if x.get("id") != tid]
        if len(keep) == len(themes):
            return False
        _write(state, keep)
    return True


def claude_runner(claude: str, env: dict, cwd: str, model: str):
    """run_claude for make(): a bare `claude -p` (no tools, MCP, skills or settings) — a few k tokens per theme."""
    def run(prompt: str) -> str:
        r = subprocess.run([claude, "-p", "--output-format", "json", "--no-session-persistence", "--tools", "",
                            "--strict-mcp-config", "--disable-slash-commands", "--setting-sources", "",
                            "--system-prompt", "You are a UI theme designer. Reply with only the JSON object asked for.",
                            "--model", model], cwd=cwd, env=env, input=prompt, capture_output=True, text=True, timeout=240)
        try:
            out = json.loads(r.stdout)
        except ValueError:
            raise ValueError((r.stderr or r.stdout or "claude failed").strip()[-300:])
        if out.get("is_error"):
            raise ValueError(str(out.get("result") or "claude failed")[:300])
        return out.get("result") or ""
    return run
