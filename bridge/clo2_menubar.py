"""CLO 2 in the Mac's menu bar: online or not, approve a phone that asks to pair (type its 4-digit code), pair a new
device (QR in Terminal), where devices may pair from, permissions (Standard / Full access), see / revoke devices,
restart, check for updates.

A separate small process (LaunchAgent com.clo2.menubar) that only talks to the bridge's local control page
(http://127.0.0.1:7788, the same API the page uses) — the bridge keeps running if this quits, and vice versa.

  python3 -I clo2_menubar.py           # run (the installer starts it at login)
  python3 -I clo2_menubar.py --check   # print the menu for a few sample states (no UI; tests)
"""
import json
import os
import pathlib
import subprocess
import sys
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
PORT = int(os.environ.get("CLO2_PORT") or 7788)
API = f"http://127.0.0.1:{PORT}"
WEB = "https://clo2.sakura5.app"
LABEL = "com.clo2.bridge"


def api(path, body=None, timeout=1.5):
    req = urllib.request.Request(API + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"X-CLO": "1", "Content-Type": "application/json"} if body is not None else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def version():
    try:
        return (HERE / "VERSION").read_text().strip()
    except OSError:
        r = subprocess.run(["git", "-C", str(HERE), "log", "-1", "--format=%h %cs"], capture_output=True, text=True)
        return (r.stdout.strip() + " (git)") if r.returncode == 0 else "?"


def pair_link(state):
    d = (state.get("pairing") or "").split("d=", 1)[-1]
    return f"{state.get('web') or WEB}/#pair={d}" if d else None


VIA = [("tailnet", "Tailscale only"), ("wifi", "Same Wi-Fi only"), ("both", "Tailscale or same Wi-Fi"),
       ("any", "Anywhere (also links sent over the internet)")]
AUTO_OFF_WARNING = ("Vibeclo checks for a new signed version about once an hour and installs it when no task is running. "
                    "With automatic updates off, you won't get fixes — including security fixes — until you choose "
                    "\"Check for updates\" yourself.")
ACCESS = [("full", "Full access — claude --dangerously-skip-permissions"),
          ("standard", "Standard — claude --permission-mode acceptEdits")]
FULL_WARNING = ("Full access lets a paired phone or browser run Claude Code in “yolo” mode: it runs ANY command without "
                "asking — it can delete files, install software and read anything your user can. Use it only in projects "
                "under git with backups, and revoke lost devices at once.")


def plan(state):
    """The menu as data: [(title, action or None, children)] — action is a tuple the UI turns into a callback."""
    if state is None:
        return [("Vibeclo isn't running on this Mac", None, None), None,
                ("Start Vibeclo", ("start",), None), None, (f"Version {version()}", None, None),
                ("Quit menu bar icon", ("quit",), None)]
    items = [(("● " if state["online"] else "○ ") + (state.get("label") or "Mac")
              + (" · connected" if state["online"] else " · relay not reachable"), None, None), None]
    for p in state.get("pending") or []:
        # no code here: the user types the one the device shows, so it's the device in their hand
        items.append((f"{p['name']} wants to connect", None, [
            (f"Type the code shown on {p['name']}…", ("approve", p["tag"], p["check"], p["name"]), None),
            ("Deny", ("deny", p["tag"]), None)]))
    if state.get("pending"):
        items.append(None)
    if state.get("pairing"):
        items += [("Waiting to pair — show QR code", ("pair",), None), ("Cancel pairing", ("stop",), None)]
    else:
        items += [("Pair a phone…", ("pair",), None)]
    if os.environ.get("CLO2_SHOW_WEB"):   # the web app isn't announced yet (app first)
        items.append(("Open the web app on this Mac", ("web",), None))
    via, access = state.get("pairVia") or "both", state.get("access") or "full"
    items.append(("Allow pairing from", None, [(("✓ " if via == k else "    ") + t, ("via", k), None) for k, t in VIA]))
    items.append(("Permissions", None, [(("✓ " if access == k else "    ") + t, ("access", k), None) for k, t in ACCESS]))
    devs = state.get("devices") or []
    items.append((f"Devices ({len(devs)})", None,
                  [(d["name"], None, [(f"Revoke {d['name']}", ("revoke", d["id"], d["name"]), None)]) for d in devs]
                  or [("No devices yet", None, None)]))
    auto = (state.get("autoUpdate") or "on") == "on"
    items += [None, ("Restart Vibeclo", ("restart",), None), ("Check for updates", ("update",), None),
              (("✓ " if auto else "    ") + "Install updates automatically", ("autoupdate", "off" if auto else "on"), None),
              (f"Version {version()}", None, None), ("Quit menu bar icon", ("quit",), None)]
    return items


def unique(items):
    """rumps keys menu items by title: same-named devices ("iPhone" twice) get invisible suffixes to stay separate."""
    seen = {}
    out = []
    for it in items:
        if it is not None:
            n = seen.get(it[0], 0)
            seen[it[0]] = n + 1
            it = (it[0] + "\u200b" * n, it[1], unique(it[2]) if it[2] else None)
        out.append(it)
    return out


def badge(state):
    return "" if state is None else (f" {len(state['pending'])}" if state.get("pending") else "")


def check():
    sample = {"online": True, "label": "MacBook", "pairing": "clo2://pair?d=abc", "web": WEB,
              "pending": [{"tag": "p:x", "name": "iPhone", "check": "1234"}],
              "devices": [{"id": "h1", "name": "iPad", "addedAt": 0, "lastSeen": 0}]}
    for s in (sample, {**sample, "online": False, "pairing": None, "pending": [], "devices": []}, None):
        def show(items, depth=0):
            for it in items:
                print("  " * depth + ("—" if it is None else it[0] + (f"  → {it[1][0]}" if it[1] else "")))
                if it and it[2]:
                    show(it[2], depth + 1)
        print(f"[badge {badge(s)!r}] link={pair_link(s or {})}")
        show(plan(s))
        print()


def main():
    import rumps
    from AppKit import NSApplication, NSImage

    class App(rumps.App):
        def __init__(self):
            super().__init__("Vibeclo", title="Vibeclo", quit_button=None)
            self.state, self.key = None, "init"
            self.timer = rumps.Timer(self.refresh, 2)
            self.timer.start()

        def symbol(self):
            try:
                btn = self._nsapp.nsstatusitem.button()
                img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                    "bubble.left.and.text.bubble.right" if self.state and self.state["online"] else "bubble.left.and.exclamationmark.bubble.right", "CLO 2")
                img.setTemplate_(True)
                btn.setImage_(img)
            except Exception:   # no status item yet / old macOS: plain text instead
                return False
            return True

        def refresh(self, _=None):
            try:
                s = api("/api/state")
            except Exception:
                s = None
            key = json.dumps(s, sort_keys=True)
            if key == self.key:
                return
            new_pending = s and len(s.get("pending") or []) > len((self.state or {}).get("pending") or [])
            self.state, self.key = s, key
            self.title = badge(s) if self.symbol() else "CLO" + badge(s)
            self.menu.clear()
            self.menu = [self.build(it) for it in unique(plan(s))]
            if new_pending:   # a phone is waiting: make sure the user sees it
                subprocess.run(["afplay", "/System/Library/Sounds/Glass.aiff"], capture_output=True)

        def build(self, it):
            if it is None:
                return rumps.separator
            title, action, children = it
            item = rumps.MenuItem(title, callback=(lambda _s, a=action: self.do(a)) if action else None)
            for c in children or []:
                item.add(self.build(c))
            return item

        def do(self, a):
            kind = a[0]
            try:
                if kind == "approve":   # same rule as the dialog: type the code the device shows
                    code = os.environ.get("CLO2_SELFTEST_CODE")
                    if code is None:
                        w = rumps.Window(f"Type the 4-digit code shown on {a[3]}.", "Vibeclo", ok="Pair", cancel="Cancel",
                                         dimensions=(120, 24))
                        r = w.run()
                        code = r.text if r.clicked else ""
                    if "".join(c for c in code if c.isdigit()) == a[2]:
                        api("/api/approve", {"tag": a[1]})
                    elif code and not os.environ.get("CLO2_SELFTEST_CODE"):
                        rumps.alert("Vibeclo", "Wrong code — not paired.")
                elif kind == "deny":
                    api("/api/deny", {"tag": a[1]})
                elif kind == "pair":   # the QR in Terminal (`clo2 pair`)
                    clo2 = pathlib.Path.home() / ".local/bin/clo2"
                    subprocess.run(["osascript", "-e", f'tell application "Terminal" to do script "{clo2} pair"',
                                    "-e", 'tell application "Terminal" to activate'], capture_output=True)
                elif kind == "via":
                    api("/api/settings", {"key": "pairVia", "value": a[1]})
                elif kind == "access":
                    if a[1] != "full" or os.environ.get("CLO2_SELFTEST_CLICK") or rumps.alert(
                            "Allow Full access?", FULL_WARNING, ok="Allow Full access", cancel="Keep Standard") == 1:
                        api("/api/settings", {"key": "access", "value": a[1]})
                elif kind == "via":
                    api("/api/pairvia", {"mode": a[1]})
                elif kind == "stop":
                    api("/api/pair/stop", {})
                elif kind == "web":
                    s = self.state or {}
                    if not s.get("pairing"):
                        api("/api/pair/start", {})
                        s = api("/api/state")
                    if pair_link(s):
                        subprocess.run(["open", pair_link(s)])
                elif kind == "revoke":
                    if rumps.alert("Revoke this device?", f"{a[2]} loses access to this Mac and has to pair again.",
                                   ok="Revoke", cancel="Cancel") == 1:
                        api("/api/revoke", {"id": a[1]})
                elif kind == "restart":
                    subprocess.Popen(["bash", str(HERE / "restart.sh")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    rumps.alert("Vibeclo", "Restarts when Claude finishes the running turn.")
                elif kind == "start":
                    subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
                elif kind == "update":
                    self.update()
                elif kind == "autoupdate":
                    if a[1] == "on" or os.environ.get("CLO2_SELFTEST_CLICK") or rumps.alert(
                            "Turn off automatic updates?", AUTO_OFF_WARNING, ok="Turn off", cancel="Keep on") == 1:
                        api("/api/settings", {"key": "autoUpdate", "value": a[1]})
                elif kind == "quit":
                    rumps.quit_application()
            except Exception as e:
                rumps.alert("Vibeclo", f"Couldn't do that: {e}")
            self.key = "dirty"
            self.refresh()

        def update(self):
            if not (HERE / "MANIFEST.json").exists():
                return rumps.alert("Vibeclo", "This copy runs from git — update with git pull.")
            sys.path.insert(0, str(HERE))
            import updater
            m = updater.check(HERE, log=lambda *a: None)
            if not m:
                return rumps.alert("Vibeclo", f"Up to date ({version()}).")
            (pathlib.Path(os.environ.get("CLO2_DIR") or pathlib.Path.home() / ".clo2") / "restart").touch()
            rumps.alert("Vibeclo", f"Downloaded {m.get('version')}. Vibeclo restarts when Claude finishes the running turn.")

    NSApplication.sharedApplication().setActivationPolicy_(1)   # accessory: menu bar only, no Dock icon
    app = App()
    if "--selftest" in sys.argv:   # tests: after the first ticks, read back what AppKit actually shows, then quit
        def dump(_):
            item = app._nsapp.nsstatusitem

            def titles(menu, depth=0):
                for mi in menu.itemArray():
                    print("  " * depth + ("—" if mi.isSeparatorItem() else str(mi.title())) + ("" if mi.isEnabled() or mi.isSeparatorItem() else "  (disabled)"))
                    if mi.hasSubmenu():
                        titles(mi.submenu(), depth + 1)
            print("status item:", item is not None, "· symbol image:", item.button().image() is not None, "· title:", repr(str(item.button().title())))
            titles(item.menu())
            click = os.environ.get("CLO2_SELFTEST_CLICK")   # tests: press a menu item the way a click does

            def press(menu):
                for i, mi in enumerate(menu.itemArray()):
                    if mi.hasSubmenu() and press(mi.submenu()):
                        return True
                    if not mi.isSeparatorItem() and str(mi.title()).startswith(click):
                        menu.performActionForItemAtIndex_(i)
                        print("clicked:", mi.title())
                        return True
            if click:
                press(item.menu())
            sys.stdout.flush()   # quitting ends the process without flushing a pipe
            rumps.quit_application()
        rumps.Timer(dump, 5).start()
    app.run()   # the 2 s timer builds the menu on its first tick


if __name__ == "__main__":
    check() if "--check" in sys.argv else main()
