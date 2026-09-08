#!/usr/bin/env python3
"""Regenerate docs/screenshots/*.png from a synthetic log fixture.

Screenshots must never be captured against a real account: the Home cards publish
project folder names and real spend. This script builds a throwaway Claude Code log
tree and save file, points the app at them, and captures every published shot.

    python3 tools/screenshots.py            # writes docs/screenshots/
    python3 tools/screenshots.py --out /tmp # writes somewhere else first

Needs a running X server (WSLg or any desktop), python-xlib and Pillow.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

WORK = Path("/tmp/poketoken-shots")
LOGS = WORK / "claude" / "projects"
STATE = WORK / "state"

# neutral, obviously-fake project folders: project_label() strips the leading dash
PROJECTS = {"-demo-web-app": 0.55, "-demo-api": 0.30, "-demo-notes": 0.15}
MODELS = [("claude-fable-5-1", 0.55), ("claude-opus-5", 0.30), ("claude-sonnet-5", 0.15)]
DAYS = 17                     # enough history for a streak and a weekly goal
SEED = 7

SHOTS = [("home", 420, 780), ("stats", 390, 760), ("home-wide", 1280, 820),
         ("dex", 1280, 820), ("shop", 1280, 820), ("home-dark", 420, 780),
         ("compact", 300, 460)]


def build_logs() -> None:
    rng = random.Random(SEED)
    now = datetime.now()
    for folder, share in PROJECTS.items():
        d = LOGS / folder
        d.mkdir(parents=True)
        lines = []
        for back in range(DAYS):
            day = now - timedelta(days=back)
            budget = int((26_000_000 if back else 41_000_000) * share)
            per = budget / 14
            for i in range(14):
                ts = day.replace(hour=9 + i % 9, minute=(i * 7) % 60, second=(i * 11) % 60)
                model = rng.choices([m for m, _ in MODELS], [w for _, w in MODELS])[0]
                write = int(per * 0.05)
                lines.append(json.dumps({
                    "type": "assistant", "timestamp": ts.isoformat() + "Z",
                    "requestId": f"req_{folder}_{back}_{i}",
                    "message": {"id": f"msg_{folder}_{back}_{i}", "model": model,
                                "usage": {"input_tokens": 40, "output_tokens": int(per * 0.006),
                                          "cache_creation_input_tokens": write,
                                          "cache_read_input_tokens": int(per * 0.93),
                                          "cache_creation": {"ephemeral_5m_input_tokens": write,
                                                             "ephemeral_1h_input_tokens": 0}}}}))
        (d / "session.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_state(with_encounter: bool) -> None:
    """A trainer mid-run: Wooper raising, two in the Pokédex, a rare Eevee waiting."""
    today = datetime.now().strftime("%Y-%m-%d")
    ivs = {"hp": 27, "attack": 31, "defense": 12, "special-attack": 22,
           "special-defense": 30, "speed": 31}
    state = {
        "installBaselineSet": True, "usedSinceInstall": 6_180_000_000, "spentTokens": 570_000_000,
        "eggUsage": 0, "eggTier": None, "pendingHatchID": None,
        "claimedTodayTokensByProvider": {"claude_code": 0}, "lastDate": "1970-01-01",
        "active": {"baseID": 194, "pathIDs": [194], "plannedPathIDs": [194, 195],
                   "stageIndex": 0, "usedAtStage": 133_000_000, "rarity": "common",
                   "totalForms": 2, "isShiny": False, "nature": "jolly", "ivs": ivs,
                   "luck": {"streak": 16, "cacheRatio": 0.93, "bonusRolls": 3,
                            "shinyDenominator": 48, "charm": False}},
        "dex": [
            {"id": "d1", "baseID": 16, "finalID": 18, "chainOrder": [16, 17, 18],
             "rarity": "common", "caughtAt": "2026-09-01T10:00:00+00:00", "isShiny": False,
             "nature": "brave", "source": "graduated", "level": 100,
             "ivs": {"hp": 20, "attack": 31, "defense": 15, "special-attack": 9,
                     "special-defense": 28, "speed": 31},
             "names": {"16": {"en": "Pidgey"}, "17": {"en": "Pidgeotto"}, "18": {"en": "Pidgeot"}}},
            {"id": "d2", "baseID": 25, "finalID": 25, "chainOrder": [25], "rarity": "common",
             "caughtAt": "2026-09-06T09:00:00+00:00", "isShiny": True, "nature": "timid",
             "source": "wild", "level": 21,
             "ivs": {"hp": 24, "attack": 23, "defense": 29, "special-attack": 24,
                     "special-defense": 25, "speed": 30},
             "names": {"25": {"en": "Pikachu"}}}],
        "collectedFinals": ["16:18", "25:25"],
        "inventory": {"rareCandy": 2, "mint": 1, "greatBall": 2},
        "encounters": [], "candyGrantTier": {},
        "encounterFeatureSeeded": True, "candyFeatureSeeded": True,
        "historyBackfilled": False, "language": "en"}
    if with_encounter:
        state["encounters"] = [{"id": "e1", "species": 133, "name": "Eevee",
                                "names": {"en": "Eevee"}, "captureRate": 45, "level": 38,
                                "rarity": "rare", "shiny": False, "nature": "jolly",
                                "trigger": "personal-best 5-hour block", "appeared": today,
                                "expires": "2099-12-31", "status": "wild", "throws": 0,
                                "luck": {}}]
    else:                       # today's triggers already spent: no wild card on the Home shot
        state["candyGrantTier"] = {f"enc:day:{today}": 1, f"enc:best:{today}": 1}
    (STATE / "state.json").write_text(json.dumps(state), encoding="utf-8")
    (STATE / "settings.json").write_text(json.dumps({"trainer": "Trainer"}), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(ROOT / "docs" / "screenshots"))
    ap.add_argument("--display", default=os.environ.get("DISPLAY", ":0"))
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    os.environ["DISPLAY"] = args.display
    os.environ["CLAUDE_CONFIG_DIR"] = str(WORK / "claude")

    from Xlib import X, display                      # noqa: E402  (needs DISPLAY first)
    from PIL import Image                            # noqa: E402
    from poketoken import cli, ui                    # noqa: E402

    warm = Path.home() / ".local" / "share" / "poketoken"
    shutil.rmtree(WORK, ignore_errors=True)
    STATE.mkdir(parents=True)
    for sub in ("sprites", "cache"):                 # reuse a warm cache when there is one
        if (warm / sub).is_dir():
            shutil.copytree(warm / sub, STATE / sub)
    build_logs()

    def settle(w) -> None:
        t0 = time.time()
        while w.payload is None and time.time() - t0 < 120:
            w.root.update(); time.sleep(0.05)
        while (w.busy or w.refresh_again) and time.time() - t0 < 180:
            w.root.update(); time.sleep(0.05)

    def grab(w, name: str) -> None:
        w.toast = None                               # no transient toast in a published shot
        w.render()
        for _ in range(14):
            w.root.update(); time.sleep(0.05)
        d = display.Display(args.display)
        x = d.create_resource_object("window", w.root.winfo_id())
        g = x.get_geometry()
        raw = x.get_image(0, 0, g.width, g.height, X.ZPixmap, 0xffffffff)
        img = Image.frombytes("RGBX", (g.width, g.height), raw.data, "raw", "BGRX")
        img.convert("RGB").save(out / f"{name}.png")
        print(f"  {name}.png  {g.width}x{g.height}")

    def size(w, px: int, py: int) -> None:
        w.root.geometry(f"{px}x{py}")
        for _ in range(10):
            w.root.update(); time.sleep(0.05)

    sizes = dict((n, (w, h)) for n, w, h in SHOTS)

    build_state(with_encounter=True)                 # the wide shots show the wild card
    win = ui.PokeWindow(cli.App(STATE), compact=False, dark=False, interval=600)
    settle(win)
    size(win, *sizes["stats"]); win.set_tab("home"); win.open_detail(194); settle(win)
    grab(win, "stats")
    win.detail = None
    size(win, *sizes["home-wide"]); win.set_tab("home"); settle(win); grab(win, "home-wide")
    win.set_tab("dex"); settle(win); grab(win, "dex")
    win.set_tab("shop"); settle(win); grab(win, "shop")
    win.quit()

    build_state(with_encounter=False)                # narrow Home: all four cards in one view
    win = ui.PokeWindow(cli.App(STATE), compact=False, dark=False, interval=600)
    settle(win)
    size(win, *sizes["home"]); win.set_tab("home"); grab(win, "home")
    win.quit()

    win = ui.PokeWindow(cli.App(STATE), compact=False, dark=True, interval=600)
    settle(win)
    size(win, *sizes["home-dark"]); win.set_tab("home"); grab(win, "home-dark")
    win.quit()

    win = ui.PokeWindow(cli.App(STATE), compact=True, dark=False, interval=600)
    settle(win)
    size(win, *sizes["compact"]); grab(win, "compact")
    win.quit()

    print(f"wrote {len(SHOTS)} screenshots to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
