#!/usr/bin/env python3
"""Record docs/demo.gif: a short scripted tour of the desktop window.

Runs against the same synthetic log fixture as tools/screenshots.py, so the
recording can never pick up real project names or real spend.

    python3 tools/demo_gif.py                  # -> docs/demo.gif
    python3 tools/demo_gif.py --fps 8 --scale 0.8

Needs a running X server (WSLg or any desktop), python-xlib and Pillow.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

WIDTH, HEIGHT = 420, 780

# (view, seconds to hold it, scroll steps to take while holding)
TOUR = [
    ("home",    3.0, 0),   # the companion, breathing
    ("dex",     2.2, 0),
    ("species", 2.6, 0),   # tap a Pokédex entry
    ("shop",    2.6, 5),   # scroll past the balls to the eggs
    ("bag",     1.4, 0),
    ("battle",  1.8, 0),
    ("home",    1.2, 0),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(ROOT / "docs" / "demo.gif"))
    ap.add_argument("--fps", type=int, default=10)
    ap.add_argument("--scale", type=float, default=1.0)
    ap.add_argument("--colors", type=int, default=128)
    ap.add_argument("--display", default=os.environ.get("DISPLAY", ":0"))
    args = ap.parse_args()

    os.environ["DISPLAY"] = args.display
    import screenshots as fx
    os.environ["CLAUDE_CONFIG_DIR"] = str(fx.WORK / "claude")

    import shutil
    from Xlib import X, display
    from PIL import Image
    from poketoken import cli, ui

    warm = Path.home() / ".local" / "share" / "poketoken"
    shutil.rmtree(fx.WORK, ignore_errors=True)
    fx.STATE.mkdir(parents=True)
    for sub in ("sprites", "cache"):
        if (warm / sub).is_dir():
            shutil.copytree(warm / sub, fx.STATE / sub)
    fx.build_logs()
    fx.build_state(with_encounter=True)

    win = ui.PokeWindow(cli.App(fx.STATE), compact=False, dark=False, interval=600)
    t0 = time.time()
    while win.payload is None and time.time() - t0 < 120:
        win.root.update(); time.sleep(0.05)
    while (win.busy or win.refresh_again) and time.time() - t0 < 180:
        win.root.update(); time.sleep(0.05)
    win.root.geometry(f"{WIDTH}x{HEIGHT}")
    for _ in range(12):
        win.root.update(); time.sleep(0.05)

    d = display.Display(args.display)
    xwin = d.create_resource_object("window", win.root.winfo_id())
    period = 1.0 / args.fps
    frames: list[Image.Image] = []

    def shoot() -> None:
        g = xwin.get_geometry()
        raw = xwin.get_image(0, 0, g.width, g.height, X.ZPixmap, 0xffffffff)
        img = Image.frombytes("RGBX", (g.width, g.height), raw.data, "raw", "BGRX").convert("RGB")
        if args.scale != 1.0:
            img = img.resize((round(g.width * args.scale), round(g.height * args.scale)), Image.LANCZOS)
        frames.append(img)

    def hold(seconds: float, scroll: int = 0) -> None:
        """Let the window animate for `seconds`, grabbing a frame every 1/fps.
        `scroll` spreads that many wheel steps evenly across the second half of the hold."""
        end = time.time() + seconds
        n = max(1, round(seconds / period))
        at = {round(n * (0.45 + 0.5 * i / max(1, scroll))): 1 for i in range(scroll)} if scroll else {}
        i = 0
        while time.time() < end:
            frame_end = time.time() + period
            if i in at:
                win.c.yview_scroll(1, "units")
            shoot()
            i += 1
            while time.time() < frame_end:
                win.root.update(); time.sleep(0.01)

    for beat, seconds, scroll in TOUR:
        if beat == "species":
            win.open_detail(194)
        else:
            win.detail = None
            win.set_tab(beat)
        win.toast = None
        win.render()
        for _ in range(3):
            win.root.update(); time.sleep(0.02)
        win.c.yview_moveto(0)
        hold(seconds, scroll)

    win.quit()

    # one shared palette, so the GIF does not flicker between per-frame palettes
    ref = frames[0].copy()
    for f in frames[1::7]:
        ref.paste(f, (0, 0))                 # a cheap mix of every view for the quantiser
    pal = ref.quantize(colors=args.colors, method=Image.MEDIANCUT)
    conv = [f.quantize(palette=pal, dither=Image.FLOYDSTEINBERG) for f in frames]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    conv[0].save(out, save_all=True, append_images=conv[1:], duration=round(period * 1000),
                 loop=0, optimize=True, disposal=2)
    secs = len(frames) * period
    print(f"{out}  {len(frames)} frames  {secs:.1f}s  {out.stat().st_size / 1e6:.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
