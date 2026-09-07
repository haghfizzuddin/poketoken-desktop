"""Function-coverage audit (manual, needs network + a WSLg display).

Traces every function call while running: the unit tests, every CLI command, the PokéAPI
fallbacks, and a scripted walkthrough of the window (tabs, buttons, menu, toggles, error
paths). Then lists every `def` in the package that was never entered.

    python3 tests/audit_functions.py
"""
from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import random
import shutil
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "poketoken"
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DISPLAY", ":0")

called: set[tuple[str, str, int]] = set()


def tracer(frame, event, arg):
    if event == "call":
        co = frame.f_code
        if co.co_filename.startswith(str(PKG)):
            called.add((os.path.relpath(co.co_filename, PKG), co.co_name, co.co_firstlineno))
    return None


sys.settrace(tracer)
threading.settrace(tracer)

from poketoken import cli, companion as C, fmt, pokeapi, pricing, ui, usage as U  # noqa: E402

M = 1_000_000
problems: list[str] = []
scratch = Path(tempfile.mkdtemp(prefix="ptaudit-"))


def section(name):
    print(f"\n── {name}")


def quiet(fn, *a, **k):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            rc = fn(*a, **k)
        except SystemExit as e:
            rc = e.code
    return rc, buf.getvalue()


# ------------------------------------------------------------------ 1. unit tests
section("unit tests")
suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_*.py")
res = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
print(f"{res.testsRun} tests, failures={len(res.failures)} errors={len(res.errors)}")
if not res.wasSuccessful():
    problems.append("unit tests failing")

# ------------------------------------------------------------------ 2. pure helpers
section("fmt / pricing / usage helpers")
for v in (5, 12_345, 190_612_940, 1_240_000_000, -2_500):
    fmt.compact(v)
fmt.grouped(1234567); fmt.cost(3.14159); fmt.percent(50); fmt.percent(33.3); fmt.bar(0.5)
for usd in (9.5, 311, 12_345):
    fmt.cost_compact(usd)
for m in ("grok-4", "antigravity/claude-sonnet-4-6", "claude-opus-9", "claude-sonnet-5-x", "claude-sonnet-9",
          "claude-haiku-9", "claude-fable-6", "claude-mythos-5-1", "unknown"):
    pricing.rate(m)
os.environ["CLAUDE_CONFIG_DIR"] = f"{Path.home() / '.claude'},{scratch / 'nope'}"
os.environ["POKETOKEN_SCAN_ROOTS"] = str(scratch)
roots = U.claude_project_roots()
del os.environ["CLAUDE_CONFIG_DIR"], os.environ["POKETOKEN_SCAN_ROOTS"]
print("roots via CLAUDE_CONFIG_DIR:", [str(r) for r in roots])
snap = U.summarize([], None)
assert not snap.has_data and snap.today_by_provider() == {}
from poketoken import paths  # noqa: E402
print("default state dir:", paths.state_dir())

# ------------------------------------------------------------------ 3. PokéAPI fallbacks
section("pokeapi fallbacks (network)")
api = pokeapi.PokeAPI(scratch / "cache", scratch / "sprites")
try:
    sid = api.random_base_via_rest(random.Random(3))
    print("REST random base:", sid)
except pokeapi.PokeAPIError as e:
    problems.append(f"random_base_via_rest failed: {e}")
# base index: fresh GraphQL, then a broken endpoint with (a) stale disk cache, (b) nothing
idx = api.base_index(); print("graphql base index:", len(idx), "species")
saved_gql = pokeapi.GRAPHQL
pokeapi.GRAPHQL = "https://127.0.0.1:9/"
api2 = pokeapi.PokeAPI(scratch / "cache", scratch / "sprites")
disk = json.loads((scratch / "cache" / "base-index.json").read_text())
disk["fetchedAt"] = 0                      # expired → tries network → falls back to stale
(scratch / "cache" / "base-index.json").write_text(json.dumps(disk))
print("stale fallback:", len(api2.base_index()), "species")
api3 = pokeapi.PokeAPI(scratch / "cache-empty", scratch / "sprites")
try:
    api3.base_index()
    problems.append("base_index should raise with no cache and no network")
except pokeapi.PokeAPIError:
    print("no-cache + no-network raises PokeAPIError: ok")
pokeapi.GRAPHQL = saved_gql
os.environ["POKETOKEN_ALLOW_IPV6"] = "1"; pokeapi._build_opener(); del os.environ["POKETOKEN_ALLOW_IPV6"]
line = api.line(1)
pokeapi.EvoLine.from_dict(line.to_dict()); line.tree.find(3); line.tree.final_ids; line.tree.depth
try:
    api._get_json("https://pokeapi.co/api/v2/pokemon-species/999999/")
except pokeapi.PokeAPIError:
    print("HTTP error path: ok")
api.sprite(1, animated=False, shiny=True); api.sprite(9999, animated=True)   # static + fallback path
api.egg_sprite(); api.item_sprite("rare-candy")

# ------------------------------------------------------------------ 4. companion odds & ends
section("companion misc")
comp = C.Companion(api, scratch / "c.json", rng=random.Random(1), clock=time.time, log=print)
comp.update({U.PROVIDER_ID: 10 * M}, "2026-09-07"); comp.update({U.PROVIDER_ID: 16 * M}, "2026-09-07")
print("hatched:", comp.display_name(), comp.stage_text, comp.is_final_stage, comp.line_items(),
      "tokens_to_next", comp.tokens_to_next, "threshold", comp.threshold)
comp.state.used_since_install += 10_000 * M
for key in ("rareCandy", "mint", "shinyCharm", "egg:plain", "nope"):
    print(" buy", key, comp.buy(key))
print(" mint on egg:", comp.use_mint(), " candy on egg:", comp.use_rare_candy())
comp.update({U.PROVIDER_ID: 30 * M}, "2026-09-07")          # hatch the bought egg
print(" candy:", comp.use_rare_candy(), " mint:", comp.use_mint())
comp.compute_state("normal", True, True, 5)                  # tired
comp.compute_state("normal", False, False, 0)                # sleep
print(" dex names:", [e.name(e.final_id) for e in comp.state.dex])
assert C.MonState.from_dict({"pathIDs": []}) is None and C.DexEntry.from_dict({}) is None
C.CompanionState.from_dict({"active": {"bogus": 1}, "dex": [{"bad": 1}], "inventory": {"x": "y"}})

# ------------------------------------------------------------------ 5. CLI, every command
section("cli commands")
rich = scratch / "state-rich"
shutil.copytree(Path.home() / ".local/share/poketoken", rich, dirs_exist_ok=True)
for junk in ("app.pid", "app.cmd", "app.log"):              # never inherit the user's live window
    (rich / junk).unlink(missing_ok=True)
st = json.loads((rich / "state.json").read_text())
st.update({"usedSinceInstall": 6_200_000_000, "spentTokens": 600_000_000,
           "active": {"baseID": 1, "pathIDs": [1, 2], "plannedPathIDs": [1, 2, 3], "stageIndex": 1,
                      "usedAtStage": 640_000_000, "rarity": "rare", "totalForms": 3, "isShiny": True, "nature": "jolly"},
           "dex": [{"id": "x1", "baseID": 16, "finalID": 18, "chainOrder": [16, 17, 18], "rarity": "common",
                    "caughtAt": "2026-09-01T10:00:00+03:00", "isShiny": False, "nature": "brave",
                    "names": {"16": {"en": "Pidgey"}, "17": {"en": "Pidgeotto"}, "18": {"en": "Pidgeot"}}}],
           "collectedFinals": ["16:18"], "inventory": {"rareCandy": 2, "mint": 1}})
(rich / "state.json").write_text(json.dumps(st))
egg_dir = scratch / "state-egg"
shutil.copytree(Path.home() / ".local/share/poketoken", egg_dir, dirs_exist_ok=True)
for junk in ("app.pid", "app.cmd", "app.log"):
    (egg_dir / junk).unlink(missing_ok=True)
rich_ui = scratch / "state-rich-ui"                 # untouched copy for the window walkthrough
shutil.copytree(rich, rich_ui)
base = ["--state-dir", str(rich)]
for argv in (["status"], ["statusline"], ["refresh"], ["history", "-n", "10"], ["dex"], ["shop"], ["shop", "--buy", "mint"],
             ["shop", "--buy", "egg-rare"], ["bag"], ["bag", "--use", "candy"], ["bag", "--use", "mint"],
             ["bag", "--use", "bogus"], ["debug"], []):
    rc, out = quiet(cli.main, base + argv)
    flag = "" if rc in (0, 1, None) else "  <-- unexpected rc"
    print(f" {' '.join(argv) or '(default)':<22} rc={rc} {out.strip().splitlines()[0][:70] if out.strip() else ''}{flag}")
    if flag:
        problems.append(f"cli {' '.join(argv)} rc={rc}")
# empty-dex / empty-bag paths
for argv in (["dex"], ["bag"]):
    rc, out = quiet(cli.main, ["--state-dir", str(egg_dir)] + argv)
    print(f" egg-state {argv[0]:<12} rc={rc} {out.strip()[:60]}")
# watch: one iteration then Ctrl-C
saved_sleep, saved_system = cli.time.sleep, cli.os.system
cli.time.sleep = lambda s: (_ for _ in ()).throw(KeyboardInterrupt())
cli.os.system = lambda c: 0
rc, out = quiet(cli.main, base + ["watch", "-i", "1"])
cli.time.sleep, cli.os.system = saved_sleep, saved_system
print(f" watch (1 iteration)     rc={rc}")
# app / pet through the CLI with a non-blocking mainloop
saved_run = ui.PokeWindow.run
def fake_run(self):
    for _ in range(20):
        self.root.update(); time.sleep(0.05)
    self.quit()
ui.PokeWindow.run = fake_run
for argv in (["app", "--light", "-i", "60"], ["pet", "--dark"]):
    rc, out = quiet(cli.main, base + argv)
    print(f" {' '.join(argv):<22} rc={rc}")
ui.PokeWindow.run = saved_run
# background window: detach → second `app` raises it → toggle closes it
rc, out = quiet(cli.main, base + ["app", "--detach", "--light", "-i", "60"])
print(f" app --detach            rc={rc} {out.strip()[:60]}")
from poketoken import instance  # noqa: E402
if instance.running_pid(rich) is None:
    problems.append("detached window did not register a pid")
rc, out = quiet(cli.main, base + ["app"]); print(f" app (already open)      rc={rc} {out.strip()[:60]}")
time.sleep(0.5)
rc, out = quiet(cli.main, base + ["toggle"]); print(f" toggle (→ close)        rc={rc} {out.strip()[:60]}")
if instance.running_pid(rich) is not None:
    problems.append("toggle did not close the detached window")
rc, out = quiet(cli.main, base + ["close"]); print(f" close (not running)     rc={rc} {out.strip()[:60]}")
instance.write_pid(rich); instance.clear_pid(rich)
# run_window error path (no display)
saved_cls = ui.PokeWindow
class Boom:
    def __init__(self, *a, **k):
        raise tk.TclError("no display name")
ui.PokeWindow = Boom
rc, out = quiet(ui.run_window, cli.App(rich))
ui.PokeWindow = saved_cls
print(f" run_window TclError path rc={rc} ({out.strip().splitlines()[0][:50]})")

# ------------------------------------------------------------------ 6. window walkthrough
section("window walkthrough")
try:
    from Xlib import X, display
    from PIL import Image
    def grab(win, name):
        for _ in range(6):
            win.root.update(); time.sleep(0.05)
        d = display.Display(":0"); w = d.create_resource_object("window", win.root.winfo_id())
        g = w.get_geometry(); raw = w.get_image(0, 0, g.width, g.height, X.ZPixmap, 0xffffffff)
        Image.frombytes("RGBX", (g.width, g.height), raw.data, "raw", "BGRX").convert("RGB").save(scratch / f"{name}.png")
except Exception:  # noqa: BLE001
    grab = lambda win, name: None  # noqa: E731

class Ev:
    def __init__(self, **k): self.__dict__.update(k)

for label, sdir in (("rich", rich_ui), ("egg", egg_dir)):
    app = cli.App(sdir)
    win = ui.PokeWindow(app, compact=False, dark=False, interval=60)
    t0 = time.time()
    while win.payload is None and time.time() - t0 < 30:
        win.root.update(); time.sleep(0.05)
    if win.payload is None:
        problems.append(f"window[{label}] never received a payload")
    for tab in ("home", "dex", "shop", "bag"):
        win.set_tab(tab); win.root.update()
        if not win.c.find_all():
            problems.append(f"window[{label}] tab {tab} drew nothing")
    grab(win, f"win-{label}-bag")
    if label == "rich":
        win.set_tab("shop"); win._act("buy:mint"); win.root.update()          # arms
        win._act("buy:mint"); win.root.update()                               # buys
        win._act("buy:egg:rare"); win.armed = None                            # arm then let it lapse
        win.set_tab("bag"); win._act("use:rareCandy"); win._act("use:rareCandy")
        win._act("use:mint"); win._act("use:mint")
        win._act("bogus:x"); win._act("bogus:x")
        for ev in ({"kind": "hatch", "name": "A", "shiny": True}, {"kind": "evolve", "name": "B"},
                   {"kind": "graduate", "name": "C"}, {"kind": "buy", "item": "mint"}, {"kind": "egg"},
                   {"kind": "mint", "nature": "bold"}, {"kind": "other"}):
            win._event_text(ev)
        win._toast("hello"); win.render(); grab(win, "win-rich-toast")
        win._show_menu(Ev(x_root=100, y_root=100)); win.root.update(); win.menu.unpost()
        win.bring_to_front(); win.root.update()
        win.set_tab("dex"); win.open_detail(18); win.root.update()
        t0 = time.time()
        while win.busy and time.time() - t0 < 20:          # detail refresh fetches the species sprite
            win.root.update(); time.sleep(0.05)
        win.render(); grab(win, "win-rich-detail")
        win.open_detail(2); win.root.update(); win.close_detail()
        win.set_sprite_scale(2); win.set_sprite_scale(3); win.root.update()
        win.toggle_dark(); win.set_tab("home"); grab(win, "win-rich-dark"); win.toggle_dark()
        win.toggle_compact(); win.root.update(); win._show_menu(Ev(x_root=50, y_root=50)); win.menu.unpost()
        grab(win, "win-rich-compact"); win.toggle_compact()
        win.c.yview_scroll(1, "units"); win.c.yview_scroll(-1, "units")
        win._periodic(); win._poll()
        win.q.put(("err", "boom")); win._poll()
        win._load_frames(scratch / "missing.gif", static=False, bg_key="card")
        win._ellipsize("a very very long label that must be cut", "caption", 40)
        win.set_tab("home")
    else:
        grab(win, "win-egg-home") if win.set_tab("home") is None else None
    t0 = time.time()
    while win.busy and time.time() - t0 < 20:      # let the refresh kicked by _act finish
        win.root.update(); time.sleep(0.05)
    if label == "rich":
        print(f" animated frames: {len(win.frames)}")
    win.root.after(600, win.quit)
    win.run()                                       # real mainloop until quit() destroys the window
    print(f" window[{label}] ok; prefs {json.loads((sdir / 'ui.json').read_text())}")

# ------------------------------------------------------------------ 7. diff against every def
section("functions never entered")
sys.settrace(None)
threading.settrace(None)
never: dict[str, list[str]] = {}
total = 0
for path in sorted(PKG.glob("*.py")):
    tree = ast.parse(path.read_text("utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            total += 1
            first = min([d.lineno for d in node.decorator_list] + [node.lineno])
            if (path.name, node.name, first) not in called:
                never.setdefault(path.name, []).append(f"{node.name} (line {node.lineno})")
missing = sum(len(v) for v in never.values())
print(f"{total - missing}/{total} functions entered")
for f, names in never.items():
    print(f" {f}: " + ", ".join(names))
if problems:
    print("\nPROBLEMS:"); print("\n".join(" - " + p for p in problems))
print("\nartifacts:", scratch)
