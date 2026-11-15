"""themes.py: a vibecoded theme is checked before any client applies it (data only, nothing that runs or loads)."""
import json, pathlib, sys, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import themes
from harness import check, done

C = {"bg": "#FFFFFF", "surface": "#F0F0F0", "bubble": "#EEEEEE", "border": "#DDDDDD", "text": "#111111",
     "muted": "#555555", "accent": "#AA3322", "code": "#F5F5F5"}
good = {"name": "T", "light": C, "dark": C, "font": "serif",
        "background": {"gradient": ["#FFFFFF", "#EEEEEE"], "pattern": {"glyphs": ["✿"], "opacity": 9}},
        "particles": {"kind": "petals", "count": 999}}
t = themes.clean(good)
check("valid theme kept; numbers clamped", t["font"] == "serif" and t["background"]["pattern"]["opacity"] == 0.25
      and t["particles"]["count"] == 160 and t["background"]["gradientDark"], t)
bad = dict(good, css="body{background:url(https://evil/x)}", font="comic",
           background={"svg": '<svg onload="alert(1)"></svg>', "pattern": {"glyphs": ["<b>", "\"x"]}},
           particles={"kind": "fireworks"})
t = themes.clean(bad)
check("css with url(), svg with handlers, odd glyphs/kinds dropped",
      "css" not in t and "svg" not in t["background"] and "pattern" not in t["background"]
      and t["particles"]["kind"] == "none" and t["font"] == "sans", t)
t = themes.clean(dict(good, background={"svg": "<svg><script>x()</script></svg>"}))
check("svg with <script> dropped", "svg" not in t["background"], t)
try:
    themes.clean(dict(good, light={"bg": "red"}))
    check("bad colors refused", False)
except ValueError:
    check("bad colors refused", True)
check("JSON in a fence with // comments parses",
      themes.parse('Here:\n```json\n{"a": 1, // note\n "b": [1,2,],\n}\n```')["b"] == [1, 2])
st = pathlib.Path(tempfile.mkdtemp())
a = themes.save(st, good); b = themes.save(st, dict(good, name="B"), a["id"])
check("save with id replaces; list newest first", len(themes.load(st)) == 1 and themes.load(st)[0]["name"] == "B")
check("delete", themes.delete(st, a["id"]) and themes.load(st) == [])
seen = {}
def fake(prompt):
    seen["p"] = prompt
    return json.dumps(dict(good, name="Fake"))
t = themes.make("make it darker", dict(good, id="x"), fake)
check("make() sends the current theme for a change", "<current>" in seen["p"] and t["name"] == "Fake" and t["prompt"] == "make it darker")
done()
