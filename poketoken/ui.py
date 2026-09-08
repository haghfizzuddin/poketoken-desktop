"""SwiftUI-styled window for poketoken.

A normal, decorated Tk window (WSLg will not show borderless override-redirect windows,
which is why the first floating pet never appeared). Everything is drawn on one Canvas:
rounded cards, capsule progress bars, a segmented control, tinted pills and buttons, in
Apple's light/dark system palettes. Open and close it whenever you like — the game state
lives on disk and the window is only a live view of it.
"""
from __future__ import annotations

import json
import os
import queue
import signal
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageSequence, ImageTk

from . import battle as B, battle_ui as BU, companion as C, fmt, instance, layout as L, notify

LIGHT = dict(bg="#F2F2F7", card="#FFFFFF", sep="#E5E5EA", fill="#E9E9EB", fill2="#F4F4F6",
             label="#1C1C1E", secondary="#6E6E73", tertiary="#AEAEB2",
             blue="#007AFF", green="#34C759", orange="#FF9500", red="#FF3B30", yellow="#FFCC00",
             purple="#AF52DE", pink="#FF2D55", teal="#32ADE6", gray="#8E8E93",
             seg="#E3E3E8", segsel="#FFFFFF", onaccent="#FFFFFF")
DARK = dict(bg="#000000", card="#1C1C1E", sep="#2C2C2E", fill="#2C2C2E", fill2="#242426",
            label="#FFFFFF", secondary="#98989D", tertiary="#636366",
            blue="#0A84FF", green="#30D158", orange="#FF9F0A", red="#FF453A", yellow="#FFD60A",
            purple="#BF5AF2", pink="#FF375F", teal="#64D2FF", gray="#8E8E93",
            seg="#1C1C1E", segsel="#48484A", onaccent="#FFFFFF")

RARITY_COLOR = {"common": "gray", "uncommon": "green", "rare": "purple", "legendary": "orange"}
STATE_COLOR = {"egg": "yellow", "sleep": "gray", "idle": "blue", "working": "green",
               "focus": "orange", "tired": "red", "levelUp": "pink"}
STATE_LABEL = {"egg": "Incubating", "sleep": "Sleeping", "idle": "Idle", "working": "Working",
               "focus": "In the zone", "tired": "Tired", "levelUp": "Level up!"}
SPEED = {"egg": None, "sleep": 2.5, "idle": 1.6, "working": 1.0, "focus": 0.6, "tired": 1.8, "levelUp": 0.5}
FONT_PREFS = ["SF Pro Text", "Helvetica Neue", "Inter", "Segoe UI", "Ubuntu", "Liberation Sans", "DejaVu Sans"]
TABS = [("home", "Home"), ("dex", "Pokédex"), ("shop", "Shop"), ("bag", "Bag"), ("battle", "Battle")]
TYPE_COLORS = {"normal": "#A8A77A", "fire": "#EE8130", "water": "#6390F0", "electric": "#E4B90E", "grass": "#7AC74C",
               "ice": "#6FBFBC", "fighting": "#C22E28", "poison": "#A33EA1", "ground": "#D4A94A", "flying": "#A98FF3",
               "psychic": "#F95587", "bug": "#A6B91A", "rock": "#B6A136", "ghost": "#735797", "dragon": "#6F35FC",
               "dark": "#705746", "steel": "#8E8EB0", "fairy": "#D685AD"}
SPRITE_BOX = 96                     # native size class of the Gen-V sprites (used by previews)
SPRITE_BOXES = (256, 320, 384, 448)  # fixed container sizes offered in the menu
DEFAULT_SPRITE_BOX = 256            # Home shows companion + evolution line + Today without scrolling
EVO_CARD_H = 104
SPRITE_PAD = 12
MINI_BOX = 128                      # stats page header sprite
FULL_GEOMETRY = "392x700"
FULL_MARGIN = 56                    # window width needed beyond the sprite container in the full view
PREFS_VERSION = 2


def union_bbox(frames) -> tuple[int, int, int, int] | None:
    """Bounding box of the visible pixels across every frame, so an animation keeps one scale."""
    boxes = [b for b in (f.getbbox() for f in frames) if b]
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def fit_scale(w: int, h: int, target: int) -> int:
    """Largest whole-number scale that keeps w×h inside target×target (0 = must downscale)."""
    if w <= 0 or h <= 0:
        return 1
    return min(target // w, target // h)
LINE_SPRITE = 52            # sprite size in the EVOLUTION LINE card
PREVIEW_BLOCK = 8           # mosaic cell size of the next form's preview at progress 0, for a LINE_SPRITE px sprite
PREVIEW_DARK = 0.55         # how far its colours sit toward the silhouette at progress 0 (1 = solid)
PREVIEW_LEVELS = 10         # progress buckets a preview is rendered (and cached) at


def _blend(a: str, b: str, t: float) -> str:
    """Mix hex colour a toward b by t (0..1)."""
    ar, ag, ab = int(a[1:3], 16), int(a[3:5], 16), int(a[5:7], 16)
    br, bg_, bb = int(b[1:3], 16), int(b[3:5], 16), int(b[5:7], 16)
    return "#%02x%02x%02x" % (round(ar + (br - ar) * t), round(ag + (bg_ - ag) * t), round(ab + (bb - ab) * t))


def _rgb(hexcol: str) -> tuple[int, int, int, int]:
    return int(hexcol[1:3], 16), int(hexcol[3:5], 16), int(hexcol[5:7], 16), 255


def _round_pts(x1, y1, x2, y2, r):
    r = max(1, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    return [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2,
            x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]


# ------------------------------------------------------------ evolution preview (pure, no display)
def block_size(progress: float, size: int = LINE_SPRITE) -> int:
    """Mosaic cell size of the next form's preview: PREVIEW_BLOCK at progress 0 (scaled to the
    sprite size), shrinking linearly to a single pixel at progress 1."""
    p = min(1.0, max(0.0, progress))
    return max(1, round(PREVIEW_BLOCK * (size / LINE_SPRITE) * (1.0 - p)))


def preview_level(progress: float) -> float:
    """Progress bucketed down to one of PREVIEW_LEVELS steps (0.0, 0.1, … 1.0), the resolution
    previews are rendered and cached at. Only a true 1.0 reaches the sharp bucket."""
    p = min(1.0, max(0.0, progress))
    return int(p * PREVIEW_LEVELS) / PREVIEW_LEVELS


def silhouette(im: Image.Image, colour: str) -> Image.Image:
    """The sprite's alpha mask filled with one solid colour: a full silhouette."""
    out = Image.new("RGBA", im.size, _rgb(colour))
    out.putalpha(im.getchannel("A"))
    return out


def mosaic(im: Image.Image, block: int, pad: tuple = (0, 0, 0, 0)) -> Image.Image:
    """Pixelate: average the image into exact block×block cells and scale back up with hard
    edges. The image is padded to whole cells with `pad` (use the background tint, alpha 0, so
    edge cells do not average toward black)."""
    if block <= 1:
        return im.copy()
    w, h = im.size
    cols, rows = -(-w // block), -(-h // block)
    padded = Image.new("RGBA", (cols * block, rows * block), pad)
    padded.paste(im, (0, 0))
    small = padded.resize((cols, rows), Image.BOX)
    return small.resize(padded.size, Image.NEAREST).crop((0, 0, w, h))


def preview_image(im: Image.Image, progress: float, colour: str) -> Image.Image:
    """The next form's preview: pixelated into coarse cells and tinted toward its silhouette at
    progress 0, sharp and in full colour at progress 1 (it snaps clear when it evolves anyway)."""
    p = min(1.0, max(0.0, progress))
    if p >= 1.0:
        return im.copy()
    tinted = Image.blend(im, silhouette(im, colour), PREVIEW_DARK * (1.0 - p))
    block = block_size(p, max(im.size))
    if block <= 1:
        return tinted
    # transparent pixels take the silhouette colour first, so edge cells average to that and not black
    flat = Image.new("RGBA", im.size, _rgb(colour))
    flat.paste(tinted, mask=tinted)
    flat.putalpha(tinted.getchannel("A"))
    out = mosaic(flat, block, pad=_rgb(colour)[:3] + (0,))
    alpha = out.getchannel("A").point(lambda a: 255 if a >= 96 else 0)   # blocky, hard-edged shape
    out.putalpha(alpha)
    return out


def resolve_sprites(app, extra: tuple = ()) -> dict:
    """Download (or hit the disk cache for) every sprite the window may need. Runs off-thread.
    `extra` = ((species_id, shiny), ...) for species shown outside the normal views (detail page)."""
    api, s = app.api, app.companion.state
    out: dict = {"egg": api.egg_sprite()}
    for sid, shiny in extra:
        out[("anim", sid, shiny)] = api.sprite(sid, animated=True, shiny=shiny)
        out[("static", sid, shiny)] = api.sprite(sid, animated=False, shiny=shiny)
    a = s.active
    if a is not None:
        out[("anim", a.current_id, a.shiny_visible)] = api.sprite(a.current_id, animated=True, shiny=a.shiny_visible)
        for sid in a.path_ids[: a.stage_index + 1] + a.planned_path_ids[a.stage_index + 1:]:   # reached + still-hidden forms
            out[("static", sid, a.shiny_visible)] = api.sprite(sid, animated=False, shiny=a.shiny_visible)
    wanted: set[tuple[int, bool]] = set()
    for e in s.dex[-80:]:
        for sid in e.chain_order:
            wanted.add((sid, e.is_shiny))
    for sid, shiny in wanted:
        out[("static", sid, shiny)] = api.sprite(sid, animated=False, shiny=shiny)
    enc = app.companion.current_encounter()
    if enc:
        out[("static", enc["species"], bool(enc.get("shiny")))] = api.sprite(enc["species"], animated=False, shiny=bool(enc.get("shiny")))
    for name in ("rare-candy", "shiny-charm", "poke-ball", "great-ball", "ultra-ball"):
        out[("item", name)] = api.item_sprite(name)
    return out


class PokeWindow:
    def __init__(self, app, compact: bool = False, dark: bool | None = None, interval: int = 30):
        self.app = app
        self.interval = max(10, interval)
        self.prefs_file: Path = app.dir / "ui.json"
        prefs = self._load_prefs()
        if prefs.get("version") != PREFS_VERSION:           # layout changed: drop old sizes, keep appearance
            prefs = {"dark": prefs.get("dark", False), "version": PREFS_VERSION}
            self._write_prefs(prefs)
        self.compact = compact
        self.dark = bool(prefs.get("dark", False)) if dark is None else dark
        self.tab = "home"                                   # always open on Home
        self.detail: int | None = None                      # species page currently shown (any tab)
        self.detail_origin = "dex"                          # which tab opened it: "home" or "dex"
        self.sprite_box = int(prefs["sprite_box"]) if prefs.get("sprite_box") in SPRITE_BOXES else DEFAULT_SPRITE_BOX
        self.sprite_draw_box = self.sprite_box              # box used by the sprite item on the current page
        self.sprite_subject: tuple = ("egg",)
        self.P = DARK if self.dark else LIGHT

        self.lock = threading.Lock()
        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self.payload: dict | None = None
        self.images: dict = {}
        self.frames: list[ImageTk.PhotoImage] = []
        self.durations: list[int] = []
        self.frame_idx = 0
        self.sprite_item = None
        self.sprite_key = None
        self.speed: float | None = 1.0
        self.anim_job = None
        self.toast: tuple[str, float] | None = None
        self.armed: tuple[str, float] | None = None
        self.last_width = 0
        self.resize_job = None
        self.settled_geometry: str | None = None   # set from <Configure>, i.e. after the WM applied it
        self.poll_job = self.periodic_job = None
        self.refresh_again = False
        self.sync_state = "busy"                   # busy | ok | error — the header indicator
        self.last_ok: float | None = None
        self.want_quit = False                     # set by SIGTERM / `poketoken close`

        r = self.root = tk.Tk()
        r.title("PokeToken")
        r.configure(bg=self.P["bg"])
        r.geometry(self._restore_geometry(prefs))
        r.minsize(240, 300)
        self.family = self._pick_font()
        self.F = {
            "largeTitle": tkfont.Font(family=self.family, size=24, weight="bold"),
            "title": tkfont.Font(family=self.family, size=18, weight="bold"),
            "title2": tkfont.Font(family=self.family, size=15, weight="bold"),
            "headline": tkfont.Font(family=self.family, size=12, weight="bold"),
            "body": tkfont.Font(family=self.family, size=12),
            "sub": tkfont.Font(family=self.family, size=11),
            "caption": tkfont.Font(family=self.family, size=9),
            "captionB": tkfont.Font(family=self.family, size=9, weight="bold"),
            "num": tkfont.Font(family=self.family, size=28, weight="bold"),
        }
        self.c = tk.Canvas(r, bg=self.P["bg"], highlightthickness=0, bd=0, yscrollincrement=24)
        self.c.pack(fill="both", expand=True)
        self.c.bind("<MouseWheel>", lambda e: self.c.yview_scroll(-1 if e.delta > 0 else 1, "units"))
        self.c.bind("<Button-4>", lambda e: self.c.yview_scroll(-1, "units"))
        self.c.bind("<Button-5>", lambda e: self.c.yview_scroll(1, "units"))
        self.c.bind("<Configure>", self._on_configure)
        self.c.bind("<Double-Button-1>", lambda e: self.toggle_compact() if self.compact else None)
        r.bind("<Escape>", lambda e: self.quit())
        r.bind("<Control-w>", lambda e: self.quit())
        r.bind("<Control-r>", lambda e: self.refresh())
        r.protocol("WM_DELETE_WINDOW", self.quit)

        self.menu = tk.Menu(r, tearoff=0)
        self.menu.add_command(label="Refresh now", command=self.refresh, accelerator="Ctrl+R")
        self.menu.add_command(label="Compact view", command=self.toggle_compact)
        self.menu.add_command(label="Dark appearance", command=self.toggle_dark)
        self.menu.add_command(label="Notifications: On", command=self.toggle_notify)
        size_menu = tk.Menu(self.menu, tearoff=0)
        for px in SPRITE_BOXES:
            size_menu.add_command(label=f"{px} px", command=lambda px=px: self.set_sprite_box(px))
        self.menu.add_cascade(label="Sprite size", menu=size_menu)
        self.menu.add_separator()
        self.menu.add_command(label="Quit", command=self.quit, accelerator="Esc")

        self.render()
        self.refresh()
        self.poll_job = r.after(150, self._poll)
        self.periodic_job = r.after(self.interval * 1000, self._periodic)

    # ------------------------------------------------------------- prefs
    def _load_prefs(self) -> dict:
        try:
            return json.loads(self.prefs_file.read_text())
        except (OSError, ValueError):
            return {}

    def default_geometry(self) -> str:
        box = self.sprite_box
        if self.compact:
            return f"{max(272, box + 64)}x{box + 176}"
        w, h = (int(v) for v in FULL_GEOMETRY.split("x"))
        return f"{max(w, box + FULL_MARGIN)}x{h}"

    def _restore_geometry(self, prefs: dict) -> str:
        """Saved size for this view, unless it is too narrow for the current sprite scale."""
        geo = str(prefs.get("geometry_compact" if self.compact else "geometry_full", "")).split("+")[0]
        try:
            w = int(geo.split("x")[0])
        except ValueError:
            return self.default_geometry()
        need = self.sprite_box + (40 if self.compact else FULL_MARGIN)
        if w < need:                                          # saved size too narrow for the container
            return f"{need}x{geo.split('x')[1]}" if "x" in geo else self.default_geometry()
        return geo

    def set_sprite_box(self, px: int) -> None:
        self.sprite_box = px
        self.images.clear()
        self.sprite_key = None
        if self.compact:
            self.root.geometry(self.default_geometry())
        elif self.root.winfo_width() < px + FULL_MARGIN:      # widen just enough for the container
            self.root.geometry(f"{px + FULL_MARGIN}x{self.root.winfo_height()}")
        self._save_prefs()
        self.render()

    def _write_prefs(self, p: dict) -> None:
        try:
            self.prefs_file.write_text(json.dumps(p))
        except OSError:
            pass

    def _save_prefs(self) -> None:
        p = self._load_prefs()
        p.update({"dark": self.dark, "sprite_box": self.sprite_box, "version": PREFS_VERSION})
        if self.settled_geometry:
            p["geometry_compact" if self.compact else "geometry_full"] = self.settled_geometry
        self._write_prefs(p)

    def _pick_font(self) -> str:
        fams = set(tkfont.families(self.root))
        for f in FONT_PREFS:
            if f in fams:
                return f
        return "TkDefaultFont"

    # ------------------------------------------------------------ worker
    def refresh(self) -> None:
        if self.busy:
            self.refresh_again = True          # e.g. a page opened mid-refresh: run once more when this one lands
            return
        self.busy = True
        self.refresh_again = False
        self.sync_state = "busy"
        self._draw_sync()
        # The worker must not capture `self`: if it ended up holding the last reference to the
        # Tk root, Tk would be torn down from a non-main thread (Tcl_AsyncDelete abort).
        app, q, lock = self.app, self.q, self.lock
        extra = ((self.detail, self._species_shiny(self.detail)),) if self.detail else ()
        extra += self._battle_extra()                                  # the challenger's sprite, if a card is loaded
        detail = self.detail

        def work():
            try:
                with lock:
                    snap = app.tick()
                    events = app.companion.drain_events()
                paths = resolve_sprites(app, extra)
                meta: dict[int, dict] = {}
                meta_failed: set[int] = set()
                a = app.companion.state.active
                for sid in {a.current_id if a else None, detail} - {None}:
                    try:
                        meta[sid] = app.api.pokemon(sid)
                    except Exception as e:  # noqa: BLE001 — stats are optional; the card says so
                        meta_failed.add(sid)
                        app.log(f"pokemon meta unavailable for {sid}: {e}")
                q.put(("ok", {"snap": snap, "events": events, "paths": paths, "meta": meta,
                              "meta_failed": meta_failed, "at": time.time()}))
            except Exception as e:  # noqa: BLE001
                q.put(("err", repr(e)))

        threading.Thread(target=work, daemon=True).start()

    def _periodic(self) -> None:
        self.refresh()
        self.periodic_job = self.root.after(self.interval * 1000, self._periodic)

    def _poll(self) -> None:
        cmd = instance.take_command(self.app.dir)
        if cmd == "quit" or self.want_quit:
            self.quit()
            return
        if cmd == "raise":
            self.bring_to_front()
        try:
            while True:
                kind, payload = self.q.get_nowait()
                self.busy = False
                if kind == "ok":
                    self.payload = payload
                    self.sync_state, self.last_ok = "ok", payload["at"]
                    for ev in payload["events"]:
                        self._toast(self._event_text(ev))
                    self.render()
                else:
                    self.app.log(f"ui refresh error: {payload}")
                    self.sync_state = "error"
                    self._toast("Refresh failed — see events.log")
                    self.render()
                if self.refresh_again:
                    self.refresh()
        except queue.Empty:
            pass
        if self.toast and time.time() > self.toast[1]:
            self.toast = None
            self.render()
        if self.armed and time.time() > self.armed[1]:
            self.armed = None
            self.render()
        self.poll_job = self.root.after(150, self._poll)

    @staticmethod
    def _event_text(ev: dict) -> str:
        k = ev["kind"]
        if k == "hatch":
            return f"{ev.get('name')} hatched!" + ("  ✦ Shiny!" if ev.get("shiny") else "")
        if k == "evolve":
            return f"Evolved into {ev.get('name')}!"
        if k == "graduate":
            return f"{ev.get('name')} joined the Pokédex"
        if k == "buy":
            return "Purchased " + C.ITEMS.get(ev.get("item", ""), {}).get("label", ev.get("item", ""))
        if k == "egg":
            return "A new egg arrived"
        if k == "mint":
            return f"Nature is now {str(ev.get('nature', '')).title()}"
        if k == "candy":
            return f"+{ev.get('count')} Rare Candy · {ev.get('reason')}"
        if k == "dittoReveal":
            return f"It was a Ditto all along! ({ev.get('disguise')})" + ("  ✦ Shiny!" if ev.get("shiny") else "")
        if k == "encounter":
            return f"A wild {ev.get('name')} appeared!" + ("  ✦ Shiny!" if ev.get("shiny") else "") + f"  ({ev.get('reason')})"
        if k == "caught":
            return f"Gotcha! {ev.get('name')} joined the Pokédex" + ("  ✦" if ev.get("shiny") else "")
        if k == "fled":
            return f"{ev.get('name')} fled…"
        return k

    def _toast(self, text: str, seconds: float = 4.0) -> None:
        self.toast = (text, time.time() + seconds)

    # ----------------------------------------------------------- actions
    def _on_configure(self, e) -> None:
        # Size only — WSLg reports bogus screen offsets, and restoring them would put the
        # window off-screen. The window manager places it.
        self.settled_geometry = self.root.winfo_geometry().split("+")[0]
        if e.width != self.last_width:
            self.last_width = e.width
            if self.resize_job:
                self.root.after_cancel(self.resize_job)
            self.resize_job = self.root.after(60, self.render)

    def set_tab(self, tab: str) -> None:
        self.tab = tab
        self.detail = None
        self.armed = None
        self.c.yview_moveto(0)
        self._save_prefs()
        self.render()

    def toggle_dark(self) -> None:
        self.dark = not self.dark
        self.P = DARK if self.dark else LIGHT
        self.images.clear()
        self.sprite_key = None
        self.root.configure(bg=self.P["bg"])
        self.c.configure(bg=self.P["bg"])
        self._save_prefs()
        self.render()

    def toggle_compact(self) -> None:
        self._save_prefs()
        self.compact = not self.compact
        self.settled_geometry = None          # stale until the next <Configure>
        p = self._load_prefs()
        self.root.geometry(self._restore_geometry(p))
        self.sprite_key = None
        self.render()

    def _show_menu(self, e) -> None:
        self.menu.entryconfig(1, label="Full view" if self.compact else "Compact view")
        self.menu.entryconfig(2, label="Light appearance" if self.dark else "Dark appearance")
        self.menu.entryconfig(3, label=f"Notifications: {'On' if notify.enabled(self.app.dir) else 'Off'}")
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()

    def toggle_notify(self) -> None:
        on = not notify.enabled(self.app.dir)
        notify.set_enabled(self.app.dir, on)
        self._toast(f"Notifications {'on' if on else 'off'}")
        self.render()

    def _act(self, tag: str) -> None:
        """Buttons arm on first click and fire on the second within 3 s (no accidental spending)."""
        if not (self.armed and self.armed[0] == tag):
            self.armed = (tag, time.time() + 3.0)
            self.render()
            return
        self.armed = None
        kind, key = tag.split(":", 1)
        with self.lock:
            if kind == "buy":
                ok, msg = self.app.companion.buy(key)
            elif kind == "use":
                ok, msg = self.app.companion.use_item(key)
            elif kind == "throw":
                ok, msg = self.app.companion.throw_ball(key)
            else:
                ok, msg = False, "unknown action"
            self.app.companion.drain_events()
        self._toast(msg if ok else "✕ " + msg)
        self.render()
        self.refresh()

    def bring_to_front(self) -> None:
        r = self.root
        r.deiconify()
        r.lift()
        try:
            r.attributes("-topmost", True)
            r.after(300, lambda: r.attributes("-topmost", False))
        except tk.TclError:
            pass
        r.focus_force()

    def quit(self) -> None:
        self._save_prefs()
        for job in (self.poll_job, self.periodic_job, self.anim_job, self.resize_job):
            if job:
                try:
                    self.root.after_cancel(job)
                except tk.TclError:
                    pass
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()

    # ---------------------------------------------------------- drawing helpers
    def col(self, key: str) -> str:
        return self.P.get(key, key)

    def text(self, x, y, s, font="body", fill="label", anchor="nw", **kw):
        return self.c.create_text(x, y, text=s, font=self.F[font], fill=self.col(fill), anchor=anchor, **kw)

    def measure(self, s: str, font: str) -> int:
        return self.F[font].measure(s)

    def rrect(self, x1, y1, x2, y2, r=14, fill="card", outline=None, **kw):
        return self.c.create_polygon(_round_pts(x1, y1, x2, y2, r), smooth=True, splinesteps=16,
                                     fill=self.col(fill), outline=self.col(outline) if outline else "", **kw)

    def card(self, x, y, w, h, r=16, tags=()):
        return self.rrect(x, y, x + w, y + h, r, fill="card", outline="sep", tags=tags)

    def fit_card(self, item, x, y, w, h, r=16):
        self.c.coords(item, *_round_pts(x, y, x + w, y + h, r))

    def sep(self, x, y, w):
        self.c.create_line(x, y, x + w, y, fill=self.P["sep"])

    def dot(self, x, y, r, color):
        self.c.create_oval(x - r, y - r, x + r, y + r, fill=self.col(color), outline="")

    def pill(self, x, y, s, color, font="captionB", h=20, pad=8) -> int:
        """Tinted capsule with coloured text; returns its width."""
        w = self.measure(s, font) + pad * 2
        tint = _blend(self.col(color), self.P["card"], 0.82 if not self.dark else 0.7)
        self.rrect(x, y, x + w, y + h, h / 2, fill=tint)
        self.text(x + w / 2, y + h / 2, s, font, color, anchor="center")
        return w

    def capsule(self, x, y, w, h, frac, color):
        self.rrect(x, y, x + w, y + h, h / 2, fill="fill")
        if frac > 0:
            self.rrect(x, y, x + max(h, w * min(1.0, frac)), y + h, h / 2, fill=color)

    def button(self, x, y, w, h, label, tag, style="filled", font="captionB"):
        if style == "disabled":
            self.rrect(x, y, x + w, y + h, h / 2, fill="fill")
            self.text(x + w / 2, y + h / 2, label, font, "tertiary", anchor="center")
            return
        if style == "armed":
            self.rrect(x, y, x + w, y + h, h / 2, fill="orange", tags=(tag,))
            self.text(x + w / 2, y + h / 2, label, font, "onaccent", anchor="center", tags=(tag,))
        elif style == "filled":
            self.rrect(x, y, x + w, y + h, h / 2, fill="blue", tags=(tag,))
            self.text(x + w / 2, y + h / 2, label, font, "onaccent", anchor="center", tags=(tag,))
        else:  # tinted
            tint = _blend(self.P["blue"], self.P["card"], 0.85 if not self.dark else 0.72)
            self.rrect(x, y, x + w, y + h, h / 2, fill=tint, tags=(tag,))
            self.text(x + w / 2, y + h / 2, label, font, "blue", anchor="center", tags=(tag,))
        self.c.tag_bind(tag, "<Button-1>", lambda e, t=tag: self._act(t))
        self._hand(tag)

    def _hand(self, tag):
        self.c.tag_bind(tag, "<Enter>", lambda e: self.c.config(cursor="hand2"))
        self.c.tag_bind(tag, "<Leave>", lambda e: self.c.config(cursor=""))

    def img(self, path, size: int, bg_key="card", crop: bool = False):
        """Static sprite → PhotoImage of `size` px, composited on the card colour.
        `crop` trims transparent margins first (egg/item PNGs are mostly padding)."""
        if not path:
            return None
        key = (str(path), size, bg_key, self.dark, crop)
        if key in self.images:
            return self.images[key]
        try:
            im = Image.open(path).convert("RGBA")
        except (OSError, ValueError):
            return None
        if crop:
            bbox = im.getbbox()
            if bbox:
                im = im.crop(bbox)
        box = Image.new("RGBA", (size, size), _rgb(self.P[bg_key]))
        if max(im.size) != size:
            method = Image.NEAREST if size % max(im.size) == 0 else Image.LANCZOS
            im = im.resize((max(1, im.width * size // max(im.size)), max(1, im.height * size // max(im.size))), method)
        box.alpha_composite(im, ((size - im.width) // 2, size - im.height))
        ph = ImageTk.PhotoImage(box)
        self.images[key] = ph
        return ph

    def preview_img(self, path, size: int, progress: float | None, bg_key="card"):
        """Still-hidden form → PhotoImage of `size` px on the card colour: a solid tertiary
        silhouette when `progress` is None, otherwise the sprite blurred and darkened toward that
        silhouette by how far the current stage still has to go (see preview_image). Cached per
        PREVIEW_LEVELS bucket of progress, so re-rendering does not re-blur."""
        if not path:
            return None
        level = None if progress is None else preview_level(progress)
        key = ("preview", str(path), size, bg_key, self.dark, level)
        if key in self.images:
            return self.images[key]
        try:
            im = Image.open(path).convert("RGBA")
        except (OSError, ValueError):
            return None
        if max(im.size) != size:                        # same fit as img()
            method = Image.NEAREST if size % max(im.size) == 0 else Image.LANCZOS
            im = im.resize((max(1, im.width * size // max(im.size)), max(1, im.height * size // max(im.size))), method)
        colour = self.P["tertiary"]
        im = silhouette(im, colour) if level is None else preview_image(im, level, colour)
        box = Image.new("RGBA", (size, size), _rgb(self.P[bg_key]))
        box.alpha_composite(im, ((size - im.width) // 2, size - im.height))
        ph = ImageTk.PhotoImage(box)
        self.images[key] = ph
        return ph

    def draw_future_form(self, cx, cy, i: int) -> None:
        """A still-hidden form of the active Pokémon, index `i` of its planned line, centred on
        (cx, cy): the next form as a blurred preview that clears as progress rises, later forms
        as a solid silhouette, and the grey '?' while its sprite is not on disk yet."""
        comp = self.app.companion
        a = comp.state.active
        ph = None
        if a and self.payload and a.stage_index < i < len(a.planned_path_ids):
            path = self.payload["paths"].get(("static", a.planned_path_ids[i], a.shiny_visible))
            ph = self.preview_img(path, LINE_SPRITE, comp.progress if i == a.stage_index + 1 else None)
        if ph:
            self.c.create_image(cx, cy, image=ph)
        else:
            self.c.create_oval(cx - 24, cy - 24, cx + 24, cy + 24, fill=self.P["fill"], outline="")
            self.text(cx, cy, "?", "title2", "tertiary", anchor="center")

    def _ellipsize(self, s: str, font: str, maxw: int) -> str:
        if self.measure(s, font) <= maxw:
            return s
        while s and self.measure(s + "…", font) > maxw:
            s = s[:-1]
        return s + "…"

    # ---------------------------------------------------------------- render
    def render(self) -> None:
        c = self.c
        if self.anim_job:
            self.root.after_cancel(self.anim_job)
            self.anim_job = None
        c.delete("all")
        self.sprite_item = None
        w = max(240, c.winfo_width() or 392)
        x0, cw = 16, w - 32
        if self.compact:
            y = self.draw_compact(w)
        else:
            y = self.draw_header(w)
            narrow = self.detail is not None or self.tab in ("shop", "bag", "battle")
            if narrow:                                   # reading pages stay a readable width, centred
                cw = min(cw, 560)
                x0 = (w - cw) / 2
            if self.detail is not None:
                y = self.draw_species(y, x0, cw)
            else:
                y = {"home": self.draw_home, "dex": self.draw_dex, "shop": self.draw_shop,
                     "bag": self.draw_bag, "battle": self.draw_battle}[self.tab](y, x0, cw)
            y = self.draw_footer(y, x0, cw)
        if self.toast:
            self.draw_toast(w)
        c.configure(scrollregion=(0, 0, w, max(y + 8, c.winfo_height())))
        self._start_sprite_animation()

    def draw_toast(self, w):
        s = self.toast[0]
        tw = self.measure(s, "captionB") + 28
        x = (w - tw) / 2
        y = self.c.canvasy(max(10, (self.c.winfo_height() or 700) - 46))   # bottom of the visible area
        self.rrect(x, y, x + tw, y + 30, 15, fill="label")
        self.text(w / 2, y + 15, s, "captionB", "card", anchor="center")

    def draw_header(self, w) -> int:
        y = 14
        self.text(16, y - 2, "PokeToken", "largeTitle", "label")
        # "more" button (three dots in a circle)
        cx, cy = w - 30, y + 16
        self.c.create_oval(cx - 14, cy - 14, cx + 14, cy + 14, fill=self.P["fill"], outline="", tags=("more",))
        for dx in (-6, 0, 6):
            self.c.create_oval(cx + dx - 1.6, cy - 1.6, cx + dx + 1.6, cy + 1.6, fill=self.P["secondary"],
                               outline="", tags=("more",))
        self.c.tag_bind("more", "<Button-1>", self._show_menu)
        self._hand("more")
        self._draw_sync(w)
        y += 40
        # segmented control (capped and centred when the window is wide)
        segw = min(w - 32, 560)
        x, h = (w - segw) / 2, 30
        self.rrect(x, y, x + segw, y + h, 9, fill="seg")
        n = len(TABS)
        each = (segw - 4) / n
        for i, (key, label) in enumerate(TABS):
            sx = x + 2 + i * each
            tag = f"seg:{key}"
            if key == self.tab:
                self.rrect(sx, y + 2, sx + each, y + h - 2, 7, fill="segsel", outline="sep", tags=(tag,))
                self.text(sx + each / 2, y + h / 2, label, "captionB", "label", anchor="center", tags=(tag,))
            else:
                self.rrect(sx, y + 2, sx + each, y + h - 2, 7, fill="seg", tags=(tag,))
                self.text(sx + each / 2, y + h / 2, label, "caption", "label", anchor="center", tags=(tag,))
            self.c.tag_bind(tag, "<Button-1>", lambda e, k=key: self.set_tab(k))
            self._hand(tag)
        return y + h + 10

    def _draw_sync(self, w: int | None = None) -> None:
        """Header status: green dot Live · time, amber Refreshing…, red Sync failed — plus Refresh."""
        c = self.c
        c.delete("sync")
        if self.compact:
            return
        w = w or max(240, c.winfo_width() or 392)
        colour, label = {"ok": ("green", "Live"), "busy": ("orange", "Refreshing…"),
                         "error": ("red", "Sync failed")}.get(self.sync_state, ("gray", "…"))
        if self.sync_state == "ok" and self.last_ok:
            label += " · " + datetime.fromtimestamp(self.last_ok).strftime("%H:%M")
        elif self.sync_state == "error" and self.last_ok:
            label += " · last " + datetime.fromtimestamp(self.last_ok).strftime("%H:%M")
        x = w - 30 - 14 - 12                                   # left of the ⋯ button
        cy = 14 + 16
        if self.sync_state != "busy":
            rw = self.measure("Refresh", "captionB")
            c.create_text(x, cy, text="Refresh", font=self.F["captionB"], fill=self.P["blue"], anchor="e", tags=("sync", "refresh"))
            c.tag_bind("refresh", "<Button-1>", lambda e: self.refresh())
            self._hand("refresh")
            x -= rw + 14
        c.create_text(x, cy, text=label, font=self.F["caption"], fill=self.P["secondary"], anchor="e", tags=("sync",))
        x -= self.measure(label, "caption") + 10
        c.create_oval(x - 4, cy - 4, x + 4, cy + 4, fill=self.P[colour], outline="", tags=("sync",))

    # ------------------------------------------------------------------ home
    def draw_home(self, y, x0, cw) -> int:
        """Home is a set of cards. Narrow window: one column in a fixed order. Wide window: the
        companion and its evolution line pin to the first column, the rest flow into the
        shortest column (masonry) so the page stays balanced when maximised."""
        w = cw + 2 * x0
        cols = L.columns_for(w)
        blocks = self._home_blocks()
        if cols == 1:
            for fn, _pin in blocks:
                y = fn(y, x0, cw) + 10
            return y - 10
        geo = L.column_geometry(w, cols)
        drawn = []                                             # (items, height) per block, drawn at y=0
        for fn, pin in blocks:
            col = pin if pin is not None else 0
            before = set(self.c.find_all())
            bottom = fn(0, geo[col][0], geo[col][1])
            items = [i for i in self.c.find_all() if i not in before]
            drawn.append((items, bottom, col if pin is not None else None))
        pinned = {i: c for i, (_, _, c) in enumerate(drawn) if c is not None}
        placed = L.masonry([h for _, h, _ in drawn], cols, pinned)
        bottom_y = y
        for (items, h, _), (col, off) in zip(drawn, placed):
            # unpinned blocks were drawn in column 0; shift them to their column
            dx = geo[col][0] - geo[0][0]
            for item in items:
                self.c.move(item, dx, y + off)
            bottom_y = max(bottom_y, y + off + h)
        return bottom_y

    def _home_blocks(self) -> list:
        """(draw function, pinned column or None) in narrow-window order."""
        comp = self.app.companion
        snap = self.payload["snap"] if self.payload else None
        blocks = []
        enc = comp.current_encounter()
        if enc:
            blocks.append((lambda y, x, w: self.draw_encounter_card(y, x, w, enc), None))
        blocks.append((self.draw_hero, 0))
        if comp.state.active and comp.line:
            s = comp.state
            accent = STATE_COLOR.get(comp.display_state, "blue")
            blocks.append((lambda y, x, w: self.draw_evo_line(
                y, x, w, [(sid, st == "current") for sid, st in comp.line_items()],
                lambda cid: comp.line.name(cid, s.language), accent, clickable=False), 0))
        blocks.append((self.draw_today, None))
        blocks.append((self.draw_rewards, None))
        if snap and snap.models_cost_today:
            blocks.append((self.draw_cost_by_model, None))
        if snap and snap.projects_today:
            blocks.append((self.draw_projects, None))
        blocks.append((self.draw_activity, None))
        return blocks

    def draw_hero(self, y, x0, cw) -> int:
        comp, s = self.app.companion, self.app.companion.state
        state = comp.display_state
        accent = STATE_COLOR.get(state, "blue")
        hero_tag = ("hero",) if s.active else ()
        card = self.card(x0, y, cw, 10, tags=hero_tag)
        cy = y + 10
        size = self.sprite_draw_box = self.sprite_box
        self.sprite_subject = ("egg",) if s.active is None else ("mon", s.active.current_id, s.active.shiny_visible)
        self.sprite_item = self.c.create_image(x0 + cw / 2, cy + size / 2, image="", tags=hero_tag)
        if s.active:
            self.c.tag_bind("hero", "<Button-1>", lambda e: self.open_stats())
            self._hand("hero")
            self.text(x0 + cw - 16, y + 10, "Stats ›", "captionB", "blue", anchor="ne", tags=hero_tag)
        cy += size + 2
        name = comp.display_name()
        name_font = "title" if self.measure(name, "title") < cw - 40 else "title2"
        if s.active and s.active.shiny_visible:
            tw = self.measure(name, name_font) + 6 + self.measure("✦", "title2")
            self.text(x0 + cw / 2 - tw / 2, cy, name, name_font, "label", anchor="nw")
            self.text(x0 + cw / 2 + tw / 2, cy + 3, "✦", "title2", "yellow", anchor="ne")
        else:
            self.text(x0 + cw / 2, cy, name, name_font, "label", anchor="n")
        cy += 30
        pills = []
        if s.active:
            a = s.active
            pills.append((a.rarity.title(), RARITY_COLOR[a.rarity]))
            if a.nature:
                pills.append((a.nature.title(), "teal"))
        pills.append((STATE_LABEL.get(state, state), accent))
        total = sum(self.measure(t, "captionB") + 16 for t, _ in pills) + 6 * (len(pills) - 1)
        px = x0 + cw / 2 - total / 2
        for t, colr in pills:
            px += self.pill(px, cy, t, colr) + 6
        cy += 28
        if s.active:
            left = f"Stage {s.active.stage_index + 1} of {s.active.total_forms}"
            right = f"{fmt.compact(comp.tokens_to_next)} to " + ("graduation" if comp.is_final_stage else "next form")
            frac = comp.progress
        else:
            left = "Egg"
            right = f"{fmt.compact(comp.egg_tokens_to_hatch)} to hatch"
            frac = comp.egg_progress
        self.text(x0 + 18, cy, left, "captionB", "secondary")
        self.text(x0 + cw - 18, cy, right, "caption", "secondary", anchor="ne")
        cy += 18
        self.capsule(x0 + 18, cy, cw - 36, 8, frac, accent)
        cy += 14
        used = s.active.used_at_stage if s.active else s.egg_usage
        self.text(x0 + 18, cy, f"{fmt.compact(used)} / {fmt.compact(comp.threshold)}  ·  {fmt.percent(frac * 100)}",
                  "caption", "tertiary")
        if not s.install_baseline_set:
            self.text(x0 + cw - 18, cy, "waiting for first usage", "caption", "tertiary", anchor="ne")
        cy += 20
        self.fit_card(card, x0, y, cw, cy - y)
        return cy

    def draw_today(self, y, x0, cw) -> int:
        snap = self.payload["snap"] if self.payload else None
        card = self.card(x0, y, cw, 10)
        cy = y + 10
        self.text(x0 + 18, cy, "TODAY", "captionB", "secondary")
        if snap:
            self.text(x0 + cw - 18, cy, snap.today_date, "caption", "tertiary", anchor="ne")
        cy += 16
        t = snap.today if snap else None
        self.text(x0 + 18, cy, fmt.compact(t.total) if t else "—", "num", "label")
        self.text(x0 + 18 + self.measure(fmt.compact(t.total) if t else "—", "num") + 8, cy + 20, "tokens", "sub", "secondary")
        self.text(x0 + cw - 18, cy + 8, fmt.cost(t.cost) if t else "—", "title", "green", anchor="ne")
        cy += 42
        if t:
            split = (f"in {fmt.compact(t.input)}  ·  out {fmt.compact(t.output)}  ·  "
                     f"cache write {fmt.compact(t.cache_write)}  ·  cache read {fmt.compact(t.cache_read)}")
            self.text(x0 + 18, cy, self._ellipsize(split, "caption", cw - 36), "caption", "secondary")
            cy += 16
        cy += 8
        self.fit_card(card, x0, y, cw, cy - y)
        return cy

    def draw_bar_list(self, y, x0, cw, title: str, rows: list, right_title: str = "", colour: str = "blue") -> int:
        """A card of ranked bars: rows = [(label, fraction, value text)]."""
        h = 12 + 18 + 24 * max(1, len(rows)) + 8
        self.card(x0, y, cw, h)
        self.text(x0 + 18, y + 12, title, "captionB", "secondary")
        if right_title:
            self.text(x0 + cw - 18, y + 12, right_title, "caption", "tertiary", anchor="ne")
        ry = y + 34
        if not rows:
            self.text(x0 + 18, ry, "nothing yet today", "caption", "tertiary")
        label_w = min(cw * 0.36, max([self.measure(r[0], "caption") for r in rows] + [40]) + 6)
        value_w = max([self.measure(r[2], "captionB") for r in rows] + [30])
        bar_x = x0 + 18 + label_w + 8
        bar_w = max(20, cw - 36 - label_w - 8 - value_w - 10)
        for label, frac, value in rows:
            self.text(x0 + 18, ry, self._ellipsize(label, "caption", label_w), "caption", "secondary")
            self.capsule(bar_x, ry + 4, bar_w, 6, frac, colour)
            self.text(x0 + cw - 18, ry - 1, value, "captionB", "label", anchor="ne")
            ry += 24
        return y + h

    def draw_cost_by_model(self, y, x0, cw) -> int:
        snap = self.payload["snap"]
        rows_src = L.top_n(snap.models_cost_today, 4)
        peak = max((v for _, v in rows_src), default=1) or 1
        rows = [(L.short_model(m), v / peak, fmt.cost(v)) for m, v in rows_src]
        return self.draw_bar_list(y, x0, cw, "COST BY MODEL", rows, f"today · {fmt.cost(snap.today.cost)}", "green")

    def draw_projects(self, y, x0, cw) -> int:
        snap = self.payload["snap"]
        rows_src = L.top_n(snap.projects_today, 5)
        peak = max((v for _, v in rows_src), default=1) or 1
        rows = [(name, v / peak, fmt.compact(int(v))) for name, v in rows_src]
        return self.draw_bar_list(y, x0, cw, "TOKENS BY PROJECT", rows, f"today · {fmt.compact(snap.today.total)}", "blue")

    def draw_rewards(self, y, x0, cw) -> int:
        """Streak progress toward the next candy, the weekly goal, and what is in the bag."""
        comp = self.app.companion
        snap = self.payload["snap"] if self.payload else None
        today = snap.today_date if snap else comp.today
        h = 12 + 18 + 2 * 44 + 24 + 8
        self.card(x0, y, cw, h)
        self.text(x0 + 18, y + 12, "REWARDS", "captionB", "secondary")
        n, _, counts = comp.streak(today)
        nxt_days, nxt_candy = C.next_streak_milestone(n)
        prev = max([m for m, _ in C.STREAK_MILESTONES if m <= n] + [0])
        if n >= C.STREAK_MILESTONES[-1][0]:
            prev = (n // C.STREAK_REPEAT_DAYS) * C.STREAK_REPEAT_DAYS
        frac = (n - prev) / max(1, nxt_days - prev)
        ry = y + 34
        self.text(x0 + 18, ry, f"Streak · {n} day{'s' if n != 1 else ''}" + ("" if counts or not n else " · not yet today"),
                  "body", "label")
        self.text(x0 + cw - 18, ry + 2, f"+{nxt_candy} candy at {nxt_days} days", "caption", "secondary", anchor="ne")
        self.capsule(x0 + 18, ry + 22, cw - 36, 8, frac, "orange")
        ry += 44
        g = comp.weekly_goal(today)
        self.text(x0 + 18, ry, "Weekly goal", "body", "label")
        if g["target"]:
            self.text(x0 + cw - 18, ry + 2, f"{fmt.compact(g['current'])} / {fmt.compact(g['target'])} · +5 candy",
                      "caption", "secondary", anchor="ne")
            self.capsule(x0 + 18, ry + 22, cw - 36, 8, g["progress"], "green" if g["progress"] >= 1 else "blue")
        else:
            self.text(x0 + cw - 18, ry + 2, f"unlocks after {g['weeks_needed']} more week(s)", "caption", "tertiary", anchor="ne")
            self.capsule(x0 + 18, ry + 22, cw - 36, 8, 0, "blue")
        ry += 44
        owned = [f"{C.ITEMS[k]['label']} ×{v}" for k, v in comp.state.inventory.items() if v > 0 and k in C.ITEMS]
        self.text(x0 + 18, ry, self._ellipsize("Bag: " + (" · ".join(owned) if owned else "empty"), "caption", cw - 36),
                  "caption", "tertiary")
        return y + h

    def draw_activity(self, y, x0, cw) -> int:
        comp, s = self.app.companion, self.app.companion.state
        snap = self.payload["snap"] if self.payload else None
        rows = []
        if snap:
            tpm = snap.tokens_per_minute
            rows.append(("Burn rate", f"{fmt.compact(int(tpm))}/min · {snap.burn_tier}" if tpm and tpm > 1000 else "quiet"))
            if snap.block:
                mins = (snap.now.timestamp() - (snap.block_start or snap.now.timestamp())) / 60
                rows.append(("5-hour block", f"{fmt.compact(snap.block.total)} · {fmt.cost(snap.block.cost)} · {mins:.0f} min"))
            rows.append(("This week", f"{fmt.compact(snap.week.total)} · {fmt.cost(snap.week.cost)}"))
            rows.append(("This month", f"{fmt.compact(snap.month.total)} · {fmt.cost(snap.month.cost)}"))
        rows.append(("Wallet", f"{fmt.compact(comp.wallet)} tokens"))
        grads = sum(1 for e in s.dex if not e.is_released)
        rows.append(("Pokédex", f"{grads} graduated · {len({sid for e in s.dex for sid in e.chain_order})} species"))
        h = 12 + 38 * len(rows) + 4
        self.card(x0, y, cw, h)
        ry = y + 12
        for i, (lbl, val) in enumerate(rows):
            self.text(x0 + 18, ry + 10, lbl, "body", "label")
            self.text(x0 + cw - 18, ry + 10, val, "sub", "secondary", anchor="ne")
            if i < len(rows) - 1:
                self.sep(x0 + 18, ry + 37, cw - 36)
            ry += 38
        return y + h

    def draw_encounter_card(self, y, x0, cw, enc: dict) -> int:
        """A wild Pokémon is waiting: who it is, how likely a catch is, and a throw button."""
        comp = self.app.companion
        h = 92
        self.card(x0, y, cw, h)
        self.text(x0 + 18, y + 10, "WILD ENCOUNTER", "captionB", "orange")
        self.text(x0 + cw - 18, y + 10, enc.get("trigger", ""), "caption", "tertiary", anchor="ne")
        shiny = bool(enc.get("shiny"))
        ph = self.img(self.payload["paths"].get(("static", enc["species"], shiny)) if self.payload else None, 56, "card")
        if ph:
            self.c.create_image(x0 + 18 + 28, y + 58, image=ph)
        tx = x0 + 18 + 64
        self.text(tx, y + 30, enc["name"] + ("  ✦" if shiny else ""), "title2", "yellow" if shiny else "label")
        px = tx
        px += self.pill(px, y + 56, enc["rarity"].title(), RARITY_COLOR.get(enc["rarity"], "gray")) + 6
        ball = comp.best_ball()
        leaves = "leaves tonight" if enc.get("expires") == comp.today else "leaves tomorrow"
        bw = 96
        bx, by = x0 + cw - 18 - bw, y + 40
        caption = (f"{C.catch_chance(enc['captureRate'], ball) * 100:.0f}% · {C.BALLS[ball]['label']}" if ball
                   else "no balls") + f" · {leaves}"
        self.text(px + 2, y + 59, self._ellipsize(caption, "caption", bx - px - 12), "caption", "secondary")
        if ball:
            tag = f"throw:{ball}"
            armed = self.armed and self.armed[0] == tag
            self.button(bx, by, bw, 28, "Throw?" if armed else "Throw", tag, "armed" if armed else "filled")
        else:
            self.rrect(bx, by, bx + bw, by + 28, 14, fill="fill", tags=("to-shop",))
            self.text(bx + bw / 2, by + 14, "Shop ›", "captionB", "blue", anchor="center", tags=("to-shop",))
            self.c.tag_bind("to-shop", "<Button-1>", lambda e: self.set_tab("shop"))
            self._hand("to-shop")
        return y + h

    # ------------------------------------------------------------------ dex
    def draw_dex(self, y, x0, cw) -> int:
        comp, s = self.app.companion, self.app.companion.state
        species: dict[int, dict] = {}
        for e in s.dex:
            for sid in e.chain_order:
                d = species.setdefault(sid, {"name": e.name(sid, s.language), "rarity": e.rarity, "shiny": False, "raising": False})
                d["shiny"] = d["shiny"] or e.is_shiny
        if s.active and comp.line:
            a = s.active
            for sid in a.path_ids[: a.stage_index + 1]:
                d = species.setdefault(sid, {"name": comp.line.name(sid, s.language), "rarity": a.rarity, "shiny": False, "raising": True})
                d["shiny"] = d["shiny"] or a.shiny_visible
                d["raising"] = True
        if not species:
            self.card(x0, y, cw, 140)
            self.text(x0 + cw / 2, y + 52, "No Pokémon yet", "title2", "label", anchor="center")
            self.text(x0 + cw / 2, y + 82, "Graduate your first companion to fill the Pokédex.", "caption", "secondary", anchor="center")
            return y + 152
        self.text(x0 + 2, y, f"{len(species)} species", "captionB", "secondary")
        y += 20
        cols = max(3, int(cw // 160))
        gap = 10
        cellw = (cw - gap * (cols - 1)) / cols
        cellh = cellw + 30
        for i, sid in enumerate(sorted(species)):
            d = species[sid]
            cx = x0 + (i % cols) * (cellw + gap)
            cy = y + (i // cols) * (cellh + gap)
            tag = f"dex:{sid}"
            self.card(cx, cy, cellw, cellh, 14, tags=(tag,))
            ph = self.img(self.payload["paths"].get(("static", sid, d["shiny"])) if self.payload else None,
                          int(cellw - 24), "card")
            if ph:
                self.c.create_image(cx + cellw / 2, cy + 8 + (cellw - 24) / 2, image=ph, tags=(tag,))
            self.text(cx + 8, cy + 8, f"#{sid}", "caption", "tertiary", tags=(tag,))
            if d["shiny"]:
                self.text(cx + cellw - 8, cy + 6, "✦", "captionB", "yellow", anchor="ne", tags=(tag,))
            self.dot(cx + 12, cy + cellh - 14, 3.5, RARITY_COLOR[d["rarity"]])
            self.text(cx + 20, cy + cellh - 22, self._ellipsize(d["name"], "captionB", cellw - 28), "captionB",
                      "label" if not d["raising"] else "blue", tags=(tag,))
            self.c.tag_bind(tag, "<Button-1>", lambda e, sid=sid: self.open_detail(sid))
            self._hand(tag)
        rows_n = (len(species) + cols - 1) // cols
        y += rows_n * (cellh + gap) + 8

        if s.dex:
            self.text(x0 + 2, y, "CATCH LOG", "captionB", "secondary")
            y += 18
            entries = sorted(s.dex, key=lambda e: e.caught_at or "", reverse=True)
            h = 8 + 52 * len(entries)
            self.card(x0, y, cw, h)
            ry = y + 8
            for i, e in enumerate(entries):
                ph = self.img(self.payload["paths"].get(("static", e.final_id, e.is_shiny)) if self.payload else None, 40, "card")
                if ph:
                    self.c.create_image(x0 + 34, ry + 26, image=ph)
                self.text(x0 + 62, ry + 9, e.name(e.final_id, s.language) + ("  ✦" if e.is_shiny else ""), "headline",
                          "label" if not e.is_shiny else "yellow")
                sub = f"{e.rarity} · {(e.nature or '').title()} · {(e.caught_at or '')[:10]}"
                self.text(x0 + 62, ry + 29, sub, "caption", "secondary")
                self.pill(x0 + cw - 18 - self.measure("released" if e.is_released else "graduated", "captionB") - 16,
                          ry + 16, "released" if e.is_released else "graduated", "gray" if e.is_released else "green")
                if i < len(entries) - 1:
                    self.sep(x0 + 62, ry + 51, cw - 78)
                ry += 52
            y += h + 12
        return y

    # --------------------------------------------------------------- detail
    def _species_shiny(self, sid: int | None) -> bool:
        s = self.app.companion.state
        if sid is None:
            return False
        if any(e.is_shiny and sid in e.chain_order for e in s.dex):
            return True
        a = s.active
        return bool(a and a.shiny_visible and sid in a.path_ids[: a.stage_index + 1])

    def open_species(self, sid: int, origin: str = "dex") -> None:
        """Show the species page. `origin` is the tab that stays highlighted and that '‹' returns to."""
        self.tab = origin
        self.detail_origin = origin
        self.detail = sid
        self.c.yview_moveto(0)
        self.render()
        self.refresh()                       # fetch this species' sprites and stats in the background

    def open_stats(self) -> None:
        """Home → the page of the Pokémon being raised."""
        a = self.app.companion.state.active
        if a is not None:
            self.open_species(a.current_id, "home")

    def open_detail(self, sid: int) -> None:
        self.open_species(sid, "dex")

    def open_board(self) -> None:
        self.set_tab("dex")

    def close_detail(self) -> None:
        self.detail = None
        self.c.yview_moveto(0)
        self.render()

    def close_stats(self) -> None:
        self.close_detail()

    def draw_evo_line(self, y, x0, cw, items, name_of, accent="blue", clickable=True) -> int:
        """One evolution-line card. items = [(species_id | None for a hidden future form, is_current)]."""
        self.card(x0, y, cw, EVO_CARD_H)
        self.text(x0 + 18, y + 10, "EVOLUTION LINE", "captionB", "secondary")
        n = max(1, len(items))
        each = (cw - 24) / n
        ty = y + 28
        for i, (cid, current) in enumerate(items):
            cx = x0 + 12 + each * i + each / 2
            if current:
                self.rrect(cx - 34, ty - 4, cx + 34, ty + 72, 12,
                           fill=_blend(self.P[accent], self.P["card"], 0.86 if not self.dark else 0.75))
            if cid is None:
                self.draw_future_form(cx, ty + 26, i)
                label = "???"
            else:
                tag = f"line:{cid}"
                ph = self.img(self.payload["paths"].get(("static", cid, self._species_shiny(cid))) if self.payload else None,
                              LINE_SPRITE, "card")
                if ph:
                    self.c.create_image(cx, ty + 26, image=ph, tags=(tag,))
                else:
                    self.text(cx, ty + 26, f"#{cid}", "caption", "tertiary", anchor="center", tags=(tag,))
                label = name_of(cid)
                if clickable and not current:
                    self.c.tag_bind(tag, "<Button-1>", lambda e, c=cid: self.open_species(c, self.detail_origin))
                    self._hand(tag)
            self.text(cx, ty + 52, self._ellipsize(label, "caption", each - 8), "caption",
                      "label" if current else "secondary", anchor="n")
            if i < n - 1:
                self.text(x0 + 12 + each * (i + 1), ty + 26, "›", "title2", "tertiary", anchor="center")
        return y + EVO_CARD_H

    def draw_species(self, y, x0, cw) -> int:
        """The species page: mini sprite header, stats, evolution line, records. Opened from Home
        (for the Pokémon being raised) or from any Pokédex cell."""
        comp, s = self.app.companion, self.app.companion.state
        sid = self.detail
        shiny = self._species_shiny(sid)
        entries = [e for e in s.dex if sid in e.chain_order]
        a = s.active
        raising = bool(a and sid in a.path_ids[: a.stage_index + 1])
        is_current = bool(a and sid == a.current_id)
        record = next((e for e in sorted(entries, key=lambda e: e.caught_at or "", reverse=True) if e.final_id == sid), None) \
            or (entries[0] if entries else None)
        name_of = (lambda cid: comp.line.name(cid, s.language)) if raising and comp.line else \
                  (lambda cid: entries[0].name(cid, s.language)) if entries else (lambda cid: f"#{cid}")
        name = name_of(sid)
        rarity = a.rarity if raising and a else (record.rarity if record else "common")
        nature = a.nature if is_current and a else (record.nature if record else None)
        meta = (self.payload or {}).get("meta", {}).get(sid)
        if is_current:
            view = comp.stats_view(meta) if meta else None
        elif meta:
            view = comp.stats_view_static(meta, record.ivs if record else None, nature)
        else:
            view = None

        # top bar
        back = "‹ Home" if self.detail_origin == "home" else "‹ Pokédex"
        self.text(x0, y + 4, back, "headline", "blue", tags=("back",))
        self.c.tag_bind("back", "<Button-1>", lambda e: self.close_detail())
        self._hand("back")
        if self.detail_origin == "home":
            self.text(x0 + cw, y + 4, "Pokédex ›", "headline", "blue", anchor="ne", tags=("to-board",))
            self.c.tag_bind("to-board", "<Button-1>", lambda e: self.open_board())
            self._hand("to-board")
        else:
            self.text(x0 + cw, y + 6, f"#{sid:03d}", "captionB", "tertiary", anchor="ne")
        y += 30

        # header card: mini sprite + identity
        h = MINI_BOX + 24
        self.card(x0, y, cw, h)
        self.sprite_draw_box = MINI_BOX
        self.sprite_subject = ("mon", sid, shiny)
        self.sprite_item = self.c.create_image(x0 + 12 + MINI_BOX / 2, y + 12 + MINI_BOX / 2, image="")
        tx, ty = x0 + 12 + MINI_BOX + 14, y + 14
        self.text(tx, ty, name + ("  ✦" if shiny else ""), "title2", "yellow" if shiny else "label")
        ty += 26
        if is_current and view:
            line2 = f"Lv {view['level']}  ·  {rarity.title()}"
        elif is_current:
            line2 = f"{rarity.title()}  ·  raising"
        elif record and not record.is_released and record.final_id == sid:
            line2 = f"Lv 100  ·  {rarity.title()}"
        else:
            line2 = rarity.title()
        self.text(tx, ty, self._ellipsize(line2, "headline", cw - MINI_BOX - 50), "headline", "label")
        ty += 24
        if view and view["types"]:
            px = tx
            for t in view["types"]:
                px += self.pill(px, ty, t.title(), TYPE_COLORS.get(t, "gray")) + 6
            ty += 26
        tail = []
        if is_current:
            tail.append(f"stage {a.stage_index + 1} of {a.total_forms}")
        elif raising:
            tail.append("raising")
        elif record:
            tail.append("released" if record.is_released else "caught in the wild" if record.is_wild else "graduated")
        if nature:
            tail.append(nature.title())
        if view:
            tail.append(f"{view['height_m']:.1f} m · {view['weight_kg']:.1f} kg")
        if shiny:
            tail.append("shiny")
        if tail:
            self.text(tx, ty, self._ellipsize(" · ".join(tail), "caption", cw - MINI_BOX - 50), "caption", "secondary")
        y += h + 10

        failed = sid in (self.payload or {}).get("meta_failed", set()) and not self.busy and not self.refresh_again
        y = self.draw_stats_card(y, x0, cw, view, is_current, failed) + 10

        # evolution line: the Pokémon being raised shows reached forms + blurred previews
        if raising and a and comp.line:
            items = [(cid, cid == sid) for cid in a.path_ids[: a.stage_index + 1]]
            items += [(None, False)] * max(0, a.total_forms - len(items))
        else:
            chain = list(record.chain_order) if record else [sid]
            items = [(cid, cid == sid) for cid in chain]
        y = self.draw_evo_line(y, x0, cw, items, name_of, "blue") + 10

        # records
        rows = []
        if raising and a:
            rows.append((f"Raising · stage {a.stage_index + 1} of {a.total_forms}", (a.nature or "").title(), "now", "blue"))
        for e in sorted(entries, key=lambda e: e.caught_at or "", reverse=True):
            verb = "released" if e.is_released else "caught" if e.is_wild else "graduated"
            rows.append((f"{verb} as {e.name(e.final_id, s.language)}", (e.nature or "").title(),
                         (e.caught_at or "")[:10], "gray" if e.is_released else "orange" if e.is_wild else "green"))
        h = 12 + 38 * max(1, len(rows)) + 4
        self.card(x0, y, cw, h)
        self.text(x0 + 18, y + 12, "RECORDS", "captionB", "secondary")
        ry = y + 30
        if not rows:
            self.text(x0 + 18, ry, "no records", "sub", "tertiary")
        for i, (what, nat, when, colr) in enumerate(rows):
            self.dot(x0 + 22, ry + 9, 3.5, colr)
            self.text(x0 + 32, ry, self._ellipsize(what, "body", cw - 150), "body", "label")
            self.text(x0 + cw - 18, ry + 1, f"{nat} · {when}".strip(" ·"), "caption", "secondary", anchor="ne")
            if i < len(rows) - 1:
                self.sep(x0 + 18, ry + 30, cw - 36)
            ry += 38
        return y + h + 12

    def draw_stats_card(self, y, x0, cw, view, live: bool, failed: bool = False) -> int:
        """Abilities and the six stats with IVs; `live` = the Pokémon being raised (shows its luck)."""
        card = self.card(x0, y, cw, 10)
        cy = y + 12
        self.text(x0 + 18, cy, "STATS" if live else "STATS AT LV 100", "captionB", "secondary")
        if view is None:
            self.text(x0 + cw - 18, cy, "unavailable offline" if failed else "loading…", "caption", "tertiary", anchor="ne")
            cy += 30
            self.fit_card(card, x0, y, cw, cy - y)
            return cy
        self.text(x0 + cw - 18, cy, f"IV total {view['iv_total']}/186" if view["iv_total"] is not None else "IVs unknown",
                  "caption", "tertiary", anchor="ne")
        cy += 22
        abil = ", ".join(x["name"].replace("-", " ").title() + (" (hidden)" if x["hidden"] else "") for x in view["abilities"])
        self.text(x0 + 18, cy, self._ellipsize("Abilities: " + abil, "caption", cw - 36), "caption", "tertiary")
        cy += 22
        peak = max(r["value"] for r in view["rows"]) or 1
        label_w = max(self.measure(r["label"], "caption") for r in view["rows"]) + 8
        bar_x = x0 + 18 + label_w
        bar_w = cw - 36 - label_w - 88
        for r in view["rows"]:
            colour = "green" if r["mod"] > 0 else "red" if r["mod"] < 0 else "blue"
            self.text(x0 + 18, cy, r["label"], "caption", "secondary")
            self.capsule(bar_x, cy + 4, bar_w, 6, r["value"] / peak, colour)
            self.text(bar_x + bar_w + 10, cy - 1, str(r["value"]), "headline", "label")
            iv = f"IV {r['iv']}" if r["iv"] is not None else "IV ?"
            self.text(x0 + cw - 18, cy + 1, iv, "caption", "tertiary", anchor="ne")
            cy += 22
        cy += 4
        lk = view.get("luck") or {}
        if live and view["iv_total"] is not None and lk:
            luck_txt = (f"Luck at hatch: {lk.get('bonusRolls', 0)} bonus roll{'s' if lk.get('bonusRolls', 0) != 1 else ''} "
                        f"· {lk.get('streak', 0)}-day streak · {lk.get('cacheRatio', 0) * 100:.0f}% cache reads · shiny 1/{lk.get('shinyDenominator', '?')}")
            self.text(x0 + 18, cy, self._ellipsize(luck_txt, "caption", cw - 36), "caption", "tertiary")
            cy += 20
        elif view["iv_total"] is None:
            self.text(x0 + 18, cy, "IVs unknown — hatched before stats existed" if live else "IVs were not recorded for this one",
                      "caption", "tertiary")
            cy += 20
        cy += 4
        self.fit_card(card, x0, y, cw, cy - y)
        return cy

    # ----------------------------------------------------------------- shop
    def draw_shop(self, y, x0, cw) -> int:
        comp = self.app.companion
        self.card(x0, y, cw, 88)
        self.text(x0 + 18, y + 12, "WALLET", "captionB", "secondary")
        self.text(x0 + 18, y + 30, fmt.compact(comp.wallet), "num", "label")
        self.text(x0 + 18 + self.measure(fmt.compact(comp.wallet), "num") + 8, y + 50, "tokens", "sub", "secondary")
        self.text(x0 + cw - 18, y + 62, "earned since install − spent", "caption", "tertiary", anchor="se")
        y += 100
        entries = comp.shop_entries()
        h = 8 + 68 * len(entries)
        self.card(x0, y, cw, h)
        ry = y + 8
        for i, r in enumerate(entries):
            self._draw_item_icon(x0 + 34, ry + 34, r["key"])
            tag = f"buy:{r['key']}"
            label = fmt.compact(r["price"])
            bw = max(64, self.measure("Buy?", "captionB") + 24, self.measure(label, "captionB") + 24)
            bx, by = x0 + cw - 18 - bw, ry + 22
            self.text(x0 + 66, ry + 12, r["label"], "headline", "label")
            self.text(x0 + 66, ry + 32, self._ellipsize(r["blurb"], "caption", bx - (x0 + 66) - 10), "caption", "secondary")
            if r["passive"] and r["owned"]:
                self.pill(bx + bw - self.measure("Owned", "captionB") - 16, by + 2, "Owned", "green")
            elif comp.wallet < r["price"]:
                self.button(bx, by, bw, 26, label, tag, "disabled")
            elif self.armed and self.armed[0] == tag:
                self.button(bx, by, bw, 26, "Buy?", tag, "armed")
            else:
                self.button(bx, by, bw, 26, label, tag, "tinted")
            if i < len(entries) - 1:
                self.sep(x0 + 66, ry + 67, cw - 82)
            ry += 68
        y += h + 12
        self.text(x0 + cw / 2, y, "Tap a price, then tap Buy? to confirm.", "caption", "tertiary", anchor="n")
        return y + 20

    def _draw_item_icon(self, cx, cy, key: str):
        paths = self.payload["paths"] if self.payload else {}
        if key.startswith("egg:"):
            tier = key.split(":")[1]
            ph = self.img(paths.get("egg"), 30, "card", crop=True)
            if ph:
                self.c.create_image(cx, cy, image=ph)
            if tier in ("uncommon", "rare"):
                self.dot(cx + 14, cy - 12, 5, RARITY_COLOR[tier])
            return
        sprite = {"rareCandy": "rare-candy", "shinyCharm": "shiny-charm", "pokeBall": "poke-ball",
                  "greatBall": "great-ball", "ultraBall": "ultra-ball"}.get(key)
        ph = self.img(paths.get(("item", sprite)), 30, "card", crop=True) if sprite else None
        if ph:
            self.c.create_image(cx, cy, image=ph)
        else:
            self.c.create_oval(cx - 17, cy - 17, cx + 17, cy + 17,
                               fill=_blend(self.P["green"], self.P["card"], 0.8), outline="")
            self.text(cx, cy, "M" if key == "mint" else "?", "headline", "green", anchor="center")

    # ------------------------------------------------------------------ bag
    def draw_bag(self, y, x0, cw) -> int:
        comp = self.app.companion
        items = [(k, n) for k, n in comp.state.inventory.items() if n > 0 and k in C.ITEMS]
        if not items:
            self.card(x0, y, cw, 140)
            self.text(x0 + cw / 2, y + 52, "Your bag is empty", "title2", "label", anchor="center")
            self.text(x0 + cw / 2, y + 82, "Buy Rare Candy or a Mint in the Shop.", "caption", "secondary", anchor="center")
            return y + 152
        h = 8 + 68 * len(items)
        self.card(x0, y, cw, h)
        ry = y + 8
        for i, (k, n) in enumerate(items):
            it = C.ITEMS[k]
            self._draw_item_icon(x0 + 34, ry + 34, k)
            bx, by, bw = x0 + cw - 18 - 64, ry + 22, 64
            self.text(x0 + 66, ry + 12, f"{it['label']}", "headline", "label")
            self.text(bx - 10, ry + 12, f"×{n}", "headline", "secondary", anchor="ne")
            self.text(x0 + 66, ry + 32, self._ellipsize(it["blurb"], "caption", bx - (x0 + 66) - 10), "caption", "secondary")
            tag = f"use:{k}"
            usable = comp.current_encounter() is not None if k in C.BALLS else not comp.is_egg
            if it["passive"]:
                self.pill(bx + bw - self.measure("Active", "captionB") - 16, by + 2, "Active", "green")
            elif not usable:
                self.button(bx, by, bw, 26, "Throw" if k in C.BALLS else "Use", tag, "disabled")
            elif self.armed and self.armed[0] == tag:
                self.button(bx, by, bw, 26, "Use?", tag, "armed")
            else:
                self.button(bx, by, bw, 26, "Throw" if k in C.BALLS else "Use", tag, "filled")
            if i < len(items) - 1:
                self.sep(x0 + 66, ry + 67, cw - 82)
            ry += 68
        return y + h + 12

    # --------------------------------------------------------------- battle
    def _battle(self) -> BU.BattleState:
        """The tab's state, created on first use so the tab stays self-contained."""
        st = self.__dict__.get("battle_state")
        if st is None:
            st = self.battle_state = BU.BattleState()
        return st

    def _battle_extra(self) -> tuple:
        """Extra (species, shiny) for refresh(): the challenger's sprite once a card is loaded."""
        key = BU.sprite_key(self._battle().challenger)
        return (key,) if key else ()

    def _battle_entry(self) -> tk.Entry:
        """The paste field: one tk.Entry, a child of the canvas, shown through a window item that
        draw_battle re-creates on every render. delete("all") drops the item and unmaps the widget,
        so it never lingers on other tabs; the widget (and its text) survives."""
        e = self.__dict__.get("battle_entry")
        if e is None:
            e = self.battle_entry = tk.Entry(self.c, bd=0, highlightthickness=0, relief="flat", font=self.F["sub"])
            e.bind("<Return>", lambda ev: self._battle_load())
        e.configure(bg=self.P["fill"], fg=self.P["label"], insertbackground=self.P["label"],
                    selectbackground=self.P["blue"], selectforeground=self.P["onaccent"])
        return e

    def _battle_my_card(self) -> dict | None:
        a = self.app.companion.state.active
        meta = (self.payload or {}).get("meta", {}).get(a.current_id) if a else None
        return BU.own_card(self.app.companion, meta, self.app.dir)

    def _battle_button(self, x, y, w, h, label, tag, handler, style="tinted") -> None:
        """A button() that runs `handler` on click instead of the Shop's arm-then-confirm _act
        (nothing here spends tokens)."""
        self.button(x, y, w, h, label, tag, style)
        if style != "disabled":
            self.c.tag_bind(tag, "<Button-1>", lambda e: handler())

    def _battle_static_img(self, path, box: int):
        """Challenger sprite for the arena, fill-scaled like the companion: crop to the visible
        pixels, largest whole-number scale that fits, centred on the card colour."""
        if not path:
            return None
        key = ("arena", str(path), box, self.dark)
        if key in self.images:
            return self.images[key]
        try:
            im = Image.open(path).convert("RGBA")
        except (OSError, ValueError):
            return None
        bbox = im.getbbox()
        if bbox:
            im = im.crop(bbox)
        target = box - 2 * SPRITE_PAD
        scale = fit_scale(im.width, im.height, target)
        if scale >= 1:
            im = im.resize((im.width * scale, im.height * scale), Image.NEAREST)
        else:
            im.thumbnail((target, target), Image.LANCZOS)
        canvas = Image.new("RGBA", (box, box), _rgb(self.P["card"]))
        canvas.alpha_composite(im, ((box - im.width) // 2, (box - im.height) // 2))
        ph = self.images[key] = ImageTk.PhotoImage(canvas)
        return ph

    def _battle_copy(self) -> None:
        card = self._battle_my_card()
        if card is None:
            self._toast("No card yet — hatch your egg first")
        else:
            self.root.clipboard_clear()
            self.root.clipboard_append(B.encode_card(card))
            self._toast("Card copied — send it to a colleague")
        self.render()

    def _battle_paste(self) -> None:
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            self._toast("Clipboard is empty")
            self.render()
            return
        self._battle_load(text)

    def _battle_reset(self) -> None:
        """Drop the current fight (new card, Clear): cancel its timer, forget the result."""
        st = self._battle()
        if st.job:
            self.root.after_cancel(st.job)
        st.job = None
        st.reset_fight()

    def _battle_load(self, text: str | None = None) -> None:
        """Decode the card in the paste field (`text` fills the field first); a bad card shows
        decode_card's message inline."""
        st = self._battle()
        e = self._battle_entry()
        if text is not None:
            e.delete(0, "end")
            e.insert(0, text.strip())
        raw = e.get().strip()
        self._battle_reset()
        st.challenger, st.error = None, ""
        if raw:
            try:
                st.challenger = B.decode_card(raw)
            except ValueError as ex:
                st.error = str(ex)
        self.render()
        if st.challenger:
            self.refresh()                     # fetches the challenger's sprite

    def _battle_clear(self) -> None:
        st = self._battle()
        self._battle_entry().delete(0, "end")
        self._battle_reset()
        st.challenger, st.error = None, ""
        self.render()

    def _battle_start(self) -> None:
        """Snapshot my card, fetch the type chart off-thread, then simulate and animate."""
        st = self._battle()
        if st.pending:
            return
        if st.challenger is None:
            self._battle_load()
            if st.challenger is None:
                return
        mine = self._battle_my_card()
        if mine is None:
            self._toast("You need a hatched Pokémon to battle")
            self.render()
            return
        self._battle_reset()
        st.mine, st.pending = mine, True
        api, q = self.app.api, st.q                # never `self` in the thread (see refresh)

        def work():
            try:
                q.put(("chart", api.type_chart()))
            except Exception as ex:  # noqa: BLE001
                q.put(("err", repr(ex)))

        threading.Thread(target=work, daemon=True).start()
        self.render()
        st.job = self.root.after(100, self._battle_poll)

    def _battle_poll(self) -> None:
        st = self._battle()
        st.job = None
        try:
            kind, payload = st.q.get_nowait()
        except queue.Empty:
            st.job = self.root.after(100, self._battle_poll)
            return
        st.pending = False
        if kind != "chart" or st.mine is None or st.challenger is None:
            self.app.log(f"battle: type chart unavailable: {payload}")
            st.error = "PokéAPI unreachable — the type chart could not be loaded, try again later"
            self.render()
            return
        st.result = B.simulate(st.mine, st.challenger, payload)
        st.schedule = BU.hp_schedule(st.result, st.mine, st.challenger)
        st.step = 0
        self.render()
        region = str(self.c.cget("scrollregion")).split()
        if self.tab == "battle" and len(region) == 4 and float(region[3]) > 0:     # bring the arena into view
            self.c.yview_moveto(max(0.0, (st.arena_y - 10) / float(region[3])))
        st.job = self.root.after(BU.tick_ms(len(st.schedule)), self._battle_tick)

    def _battle_tick(self) -> None:
        st = self._battle()
        st.job = None
        if st.result is None:
            return
        st.step += 1
        if st.finished or self.tab != "battle" or self.detail is not None or self.compact:
            self._battle_finish()              # the arena is not on screen: jump to the end
            return
        self.render()
        st.job = self.root.after(BU.tick_ms(len(st.schedule)), self._battle_tick)

    def _battle_finish(self) -> None:
        """Skip to the end of the fight (click on the arena, tab change) and record it once."""
        st = self._battle()
        if st.job:
            self.root.after_cancel(st.job)
        st.job = None
        if st.result is None:
            return
        st.step = len(st.schedule) - 1
        if not st.recorded:
            st.recorded = True
            st.history = BU.append_history(self.app.dir, BU.record_from_result(st.result, st.mine, st.challenger))
        self.render()

    def _battle_card_rows(self, x, w, cy, card, paths) -> int:
        """One card's summary: sprite, name, level and trainer, type pills, the six stats."""
        box = LINE_SPRITE
        key = BU.sprite_key(card)
        ph = self.img(paths.get(("static",) + key) if key else None, box, "card")
        if ph:
            self.c.create_image(x + box / 2, cy + box / 2, image=ph)
        else:
            self.c.create_oval(x + 4, cy + 4, x + box - 4, cy + box - 4, fill=self.P["fill"], outline="")
            self.text(x + box / 2, cy + box / 2, "?", "title2", "tertiary", anchor="center")
        tx, tw = x + box + 12, w - box - 12
        shiny = bool(card.get("shiny"))
        self.text(tx, cy, self._ellipsize(card["name"] + ("  ✦" if shiny else ""), "headline", tw), "headline",
                  "yellow" if shiny else "label")
        sub = f"Lv {card['level']} · {card.get('trainer', '?')}" + (f" · {str(card['nature']).title()}" if card.get("nature") else "")
        self.text(tx, cy + 20, self._ellipsize(sub, "caption", tw), "caption", "secondary")
        px = tx
        for t in card["types"]:
            px += self.pill(px, cy + 38, t.title(), TYPE_COLORS.get(t, "gray")) + 6
        cy += max(box, 60) + 8
        each = w / 6
        for i, (k, lbl) in enumerate(zip(C.STAT_KEYS, ("HP", "Atk", "Def", "SpA", "SpD", "Spe"))):
            cx = x + each * i + each / 2
            self.text(cx, cy, lbl, "caption", "tertiary", anchor="n")
            self.text(cx, cy + 13, str(card["stats"][k]), "headline", "label", anchor="n")
        return cy + 36

    def draw_battle(self, y, x0, cw) -> int:
        """Battle tab: my card (copy), a challenger card (paste), the arena with depleting HP
        bars, the fight log, the result banner and the record of the last fights."""
        st = self._battle()
        s = self.app.companion.state
        paths = self.payload["paths"] if self.payload else {}
        mine = self._battle_my_card()

        # ---- YOUR CARD
        card = self.card(x0, y, cw, 10)
        cy = y + 12
        self.text(x0 + 18, cy, "YOUR CARD", "captionB", "secondary")
        if mine:
            self.text(x0 + cw - 18, cy, f"power {B.power_score(mine)}", "caption", "tertiary", anchor="ne")
        cy += 22
        if s.active is None:
            self.text(x0 + 18, cy, "Still an egg", "headline", "label")
            cy += 22
            for ln in ("A battle card snapshots a hatched Pokémon: name, level, types and stats.",
                       "Spend tokens to hatch your egg, then copy your card here."):
                self.text(x0 + 18, cy, self._ellipsize(ln, "caption", cw - 36), "caption", "secondary")
                cy += 16
            cy += 6
        elif mine is None:
            failed = s.active.current_id in (self.payload or {}).get("meta_failed", set()) and not self.busy
            self.text(x0 + 18, cy, "Stats unavailable offline — the card needs PokéAPI" if failed else "Loading stats…",
                      "sub", "tertiary")
            cy += 28
        else:
            cy = self._battle_card_rows(x0 + 18, cw - 36, cy, mine, paths)
            bw = self.measure("Copy card", "captionB") + 28
            self._battle_button(x0 + 18, cy, bw, 26, "Copy card", "bt:copy", self._battle_copy, "tinted")
            self.text(x0 + 18 + bw + 10, cy + 13, self._ellipsize("puts your PT1. token on the clipboard", "caption", cw - 54 - bw),
                      "caption", "tertiary", anchor="w")
            cy += 38
        self.fit_card(card, x0, y, cw, cy - y)
        y = cy + 10

        # ---- CHALLENGER
        card = self.card(x0, y, cw, 10)
        cy = y + 12
        self.text(x0 + 18, cy, "CHALLENGER", "captionB", "secondary")
        if st.challenger:
            self.text(x0 + cw - 18, cy, f"power {B.power_score(st.challenger)}", "caption", "tertiary", anchor="ne")
        cy += 22
        pw = self.measure("Paste", "captionB") + 28
        fx, fw, fh = x0 + 18, cw - 36 - pw - 8, 30
        self.rrect(fx, cy, fx + fw, cy + fh, 9, fill="fill")
        self.c.create_window(fx + 10, cy + 4, window=self._battle_entry(), anchor="nw", width=max(20, fw - 20), height=fh - 8)
        self._battle_button(fx + fw + 8, cy + 2, pw, 26, "Paste", "bt:paste", self._battle_paste, "tinted")
        cy += fh + 10
        if st.error:
            self.text(x0 + 18, cy, self._ellipsize("✕ " + st.error, "caption", cw - 36), "caption", "red")
            cy += 20
        if st.challenger:
            cy = self._battle_card_rows(x0 + 18, cw - 36, cy, st.challenger, paths)
            bw = max(84, self.measure("Battle!", "captionB") + 28)
            if s.active is None:
                self._battle_button(x0 + 18, cy, bw, 26, "Battle!", "bt:go", self._battle_start, "disabled")
                hint = "hatch your egg first"
            elif mine is None:
                self._battle_button(x0 + 18, cy, bw, 26, "Battle!", "bt:go", self._battle_start, "disabled")
                hint = "waiting for your stats"
            elif st.pending:
                self._battle_button(x0 + 18, cy, bw, 26, "Loading…", "bt:go", self._battle_start, "disabled")
                hint = "fetching the type chart"
            else:
                self._battle_button(x0 + 18, cy, bw, 26, "Rematch" if st.result else "Battle!", "bt:go", self._battle_start, "filled")
                hint = ""
            cw_ = self.measure("Clear", "captionB") + 28
            self._battle_button(x0 + cw - 18 - cw_, cy, cw_, 26, "Clear", "bt:clear", self._battle_clear, "tinted")
            if hint:
                self.text(x0 + 18 + bw + 10, cy + 13, hint, "caption", "tertiary", anchor="w")
            cy += 38
        elif not st.error:
            self.text(x0 + 18, cy, self._ellipsize("Paste a colleague's PT1. card, then press Battle!", "caption", cw - 36),
                      "caption", "tertiary")
            cy += 22
        self.fit_card(card, x0, y, cw, cy - y)
        y = cy + 10

        # ---- ARENA, LOG, RESULT
        res = st.result
        if res and st.mine and st.challenger:
            hp_a, hp_b = st.schedule[min(st.step, len(st.schedule) - 1)]
            box = MINI_BOX
            st.arena_y = y
            card = self.card(x0, y, cw, 10, tags=("arena",))
            self.c.tag_bind("arena", "<Button-1>", lambda e: self._battle_finish())
            if not st.finished:
                self._hand("arena")
            cy = y + 12
            self.text(x0 + 18, cy, "ARENA", "captionB", "secondary", tags=("arena",))
            shown = min(st.step, len(res["log"]))
            turn = res["log"][shown - 1].split(":", 1)[0][1:] if shown else "0"
            self.text(x0 + cw - 18, cy, f"turn {turn} of {res['turns']}" + ("" if st.finished else "  ·  tap to skip"),
                      "caption", "tertiary", anchor="ne", tags=("arena",))
            cy += 22
            half = (cw - 36) / 2
            lx, rx = x0 + 18 + half / 2, x0 + 18 + half * 1.5
            self.sprite_draw_box = box
            self.sprite_subject = ("mon",) + (BU.sprite_key(st.mine) or (0, False))
            self.sprite_item = self.c.create_image(lx, cy + box / 2, image="", tags=("arena",))
            key = BU.sprite_key(st.challenger)
            ph = self._battle_static_img(paths.get(("static",) + key) if key else None, box)
            if ph:
                self.c.create_image(rx, cy + box / 2, image=ph, tags=("arena",))
            else:
                self.text(rx, cy + box / 2, "?", "title", "tertiary", anchor="center", tags=("arena",))
            self.text(x0 + 18 + half, cy + box / 2, "VS", "title2", "tertiary", anchor="center", tags=("arena",))
            cy += box + 4
            bw = half - 28
            for cx, c_, hp in ((lx, st.mine, hp_a), (rx, st.challenger, hp_b)):
                mx = int(c_["stats"]["hp"])
                self.text(cx, cy, self._ellipsize(c_["name"], "captionB", half - 12), "captionB", "label", anchor="n", tags=("arena",))
                self.capsule(cx - bw / 2, cy + 20, bw, 8, hp / mx if mx else 0, BU.hp_color(hp / mx if mx else 0))
                self.text(cx, cy + 32, "fainted" if hp <= 0 else f"{hp} / {mx} HP", "caption", "red" if hp <= 0 else "secondary",
                          anchor="n", tags=("arena",))
            cy += 54
            self.fit_card(card, x0, y, cw, cy - y)
            y = cy + 10

            if st.finished:
                bn = BU.banner(res, st.mine, st.challenger)
                colr = "green" if bn["won"] else "red"
                self.rrect(x0, y, x0 + cw, y + 76, 16, fill=_blend(self.P[colr], self.P["card"], 0.82 if not self.dark else 0.7))
                self.text(x0 + cw / 2, y + 10, bn["title"], "title2", colr, anchor="n")
                self.text(x0 + cw / 2, y + 36, self._ellipsize(bn["detail"], "caption", cw - 24), "caption", "label", anchor="n")
                self.text(x0 + cw / 2, y + 54, bn["power"], "caption", "secondary", anchor="n")
                y += 76 + 10

            lines = res["log"][max(0, shown - 6):shown]
            h = 12 + 20 + 17 * max(1, len(lines)) + 10
            self.card(x0, y, cw, h)
            self.text(x0 + 18, y + 12, "BATTLE LOG", "captionB", "secondary")
            self.text(x0 + cw - 18, y + 12, f"{shown} of {len(res['log'])} hits", "caption", "tertiary", anchor="ne")
            ly = y + 32
            if not lines:
                self.text(x0 + 18, ly, "The fight begins…", "caption", "tertiary")
            for ln in lines:
                left, right = BU.hit_row(ln)
                rw = self.measure(right, "caption") + 8 if right else 0
                if right:
                    self.text(x0 + cw - 18, ly, right, "caption", "tertiary", anchor="ne")
                self.text(x0 + 18, ly, self._ellipsize(left, "caption", cw - 36 - rw), "caption",
                          "label" if BU.is_own_hit(ln, st.mine["name"]) else "secondary")
                ly += 17
            y += h + 10

        # ---- RECORD
        if st.history is None:
            st.history = BU.load_history(self.app.dir)
        rows = list(reversed(st.history))
        wins, losses = BU.tally(st.history)
        h = 12 + 22 + (38 * len(rows) if rows else 22) + 6
        self.card(x0, y, cw, h)
        self.text(x0 + 18, y + 12, "RECORD", "captionB", "secondary")
        self.text(x0 + cw - 18, y + 12, f"{wins} W · {losses} L", "captionB", "label", anchor="ne")
        ry = y + 34
        if not rows:
            self.text(x0 + 18, ry, "No battles yet", "sub", "tertiary")
        for i, r in enumerate(rows):
            self.dot(x0 + 22, ry + 9, 3.5, "green" if r.get("won") else "red")
            right = f"{'won' if r.get('won') else 'lost'} · {r.get('turns', '?')} turns · {r.get('date', '')}"
            self.text(x0 + cw - 18, ry + 1, right, "caption", "secondary", anchor="ne")
            left = f"{r.get('opponent', '?')} · {r.get('trainer', '?')}"
            self.text(x0 + 32, ry, self._ellipsize(left, "body", cw - 60 - self.measure(right, "caption")), "body", "label")
            if i < len(rows) - 1:
                self.sep(x0 + 18, ry + 30, cw - 36)
            ry += 38
        return y + h + 12

    # -------------------------------------------------------------- compact
    def draw_compact(self, w) -> int:
        comp, s = self.app.companion, self.app.companion.state
        snap = self.payload["snap"] if self.payload else None
        state = comp.display_state
        accent = STATE_COLOR.get(state, "blue")
        size = self.sprite_draw_box = self.sprite_box
        y = 10
        self.sprite_subject = ("egg",) if s.active is None else ("mon", s.active.current_id, s.active.shiny_visible)
        self.sprite_item = self.c.create_image(w / 2, y + size / 2, image="")
        y += size
        name = comp.display_name()
        self.text(w / 2, y, name + ("  ✦" if s.active and s.active.shiny_visible else ""), "title2",
                  "yellow" if s.active and s.active.shiny_visible else "label", anchor="n")
        y += 26
        sub = f"Stage {s.active.stage_index + 1} of {s.active.total_forms} · " if s.active else ""
        sub += STATE_LABEL.get(state, state)
        self.dot(w / 2 - self.measure(sub, "caption") / 2 - 8, y + 7, 3.5, accent)
        self.text(w / 2 + 4, y, sub, "caption", "secondary", anchor="n")
        y += 20
        self.capsule(20, y, w - 40, 6, comp.progress, accent)
        y += 14
        if snap:
            line = f"today {fmt.compact(snap.today.total)} · {fmt.cost_compact(snap.today.cost)}"
            if snap.tokens_per_minute and snap.tokens_per_minute > 1000:
                line += f" · {fmt.compact(int(snap.tokens_per_minute))}/min"
            self.text(w / 2, y, line, "caption", "tertiary", anchor="n")
        y += 18
        self.text(w / 2, y, "double-click to expand", "caption", "tertiary", anchor="n", tags=("expand",))
        self.c.tag_bind("expand", "<Button-1>", lambda e: self.toggle_compact())
        self._hand("expand")
        self.c.bind("<Button-3>", self._show_menu)
        return y + 20

    def draw_footer(self, y, x0, cw) -> int:
        self.text(x0 + cw / 2, y + 6, f"auto-refresh every {self.interval} s  ·  Claude Code", "caption", "tertiary", anchor="n")
        self.c.bind("<Button-3>", self._show_menu)
        return y + 30

    # ------------------------------------------------------------- animation
    def _start_sprite_animation(self) -> None:
        if self.sprite_item is None:
            return
        comp = self.app.companion
        subject = self.sprite_subject
        key = subject + (self.dark, self.compact, self.sprite_draw_box)
        self.speed = SPEED.get(comp.display_state, 1.0) if subject[0] == "egg" or subject[1] == (comp.state.active.current_id if comp.state.active else None) else 1.0
        if key != self.sprite_key or not self.frames:
            self.sprite_key = key
            paths = self.payload["paths"] if self.payload else {}
            if subject[0] == "egg":
                path, static = paths.get("egg"), True
            else:
                path = paths.get(("anim", subject[1], subject[2])) or paths.get(("static", subject[1], subject[2]))
                static = bool(path) and str(path).endswith(".png")
            self._load_frames(path, static=static, bg_key="card" if not self.compact else "bg")
        if self.frames:
            self.c.itemconfig(self.sprite_item, image=self.frames[self.frame_idx % len(self.frames)])
            if len(self.frames) > 1:
                self._animate()
        else:
            self.c.itemconfig(self.sprite_item, image="")
            x, y = self.c.coords(self.sprite_item)
            self.text(x, y, "…" if self.payload is None else "?", "title", "tertiary", anchor="center")

    def _load_frames(self, path, static: bool, bg_key: str) -> None:
        """Fill the fixed container: crop to the visible pixels (union over all frames so the
        scale stays constant while animating), scale by the largest whole number that fits,
        centre. Small sprites therefore look as big as large ones. The egg sits at half size."""
        self.frames, self.durations, self.frame_idx = [], [], 0
        if not path or not Path(path).exists():
            return
        box = self.sprite_draw_box
        bg = _rgb(self.P[bg_key])
        try:
            im = Image.open(path)
            raw = []
            for frame in ImageSequence.Iterator(im):
                raw.append((frame.convert("RGBA"), int(frame.info.get("duration", 100)) or 100))
                if static:
                    break
        except (OSError, ValueError) as e:
            self.app.log(f"sprite load failed {path}: {e}")
            return
        bbox = union_bbox([f for f, _ in raw])
        if bbox is None:
            return
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        target = box // 2 if self.sprite_subject[0] == "egg" else box - 2 * SPRITE_PAD
        scale = fit_scale(w, h, target)
        for fr, dur in raw:
            crop = fr.crop(bbox)
            if scale >= 1:
                crop = crop.resize((w * scale, h * scale), Image.NEAREST)
            else:
                crop.thumbnail((target, target), Image.LANCZOS)
            canvas = Image.new("RGBA", (box, box), bg)
            canvas.alpha_composite(crop, ((box - crop.width) // 2, (box - crop.height) // 2))
            self.frames.append(ImageTk.PhotoImage(canvas))
            self.durations.append(dur)

    def _animate(self) -> None:
        if not self.frames or self.sprite_item is None:
            return
        self.frame_idx = (self.frame_idx + 1) % len(self.frames)
        try:
            self.c.itemconfig(self.sprite_item, image=self.frames[self.frame_idx])
        except tk.TclError:
            return
        delay = max(30, int(self.durations[self.frame_idx] * (self.speed if self.speed is not None else 1.0)))
        self.anim_job = self.root.after(delay, self._animate)


def run_window(app, compact: bool = False, dark: bool | None = None, interval: int = 30) -> int:
    if not os.environ.get("DISPLAY") and Path("/mnt/wslg").exists():
        os.environ["DISPLAY"] = ":0"
    try:
        win = PokeWindow(app, compact=compact, dark=dark, interval=interval)
    except tk.TclError as e:
        print(f"cannot open a window: {e}\n(is WSLg running? try: export DISPLAY=:0)", file=sys.stderr)
        return 1
    instance.write_pid(app.dir)
    if os.name != "nt":
        signal.signal(signal.SIGTERM, lambda *_: setattr(win, "want_quit", True))
    app.log(f"window opened compact={compact} pid={os.getpid()}")
    try:
        win.run()
    except KeyboardInterrupt:
        try:
            win.quit()
        except tk.TclError:
            pass
    finally:
        instance.clear_pid(app.dir)
        app.log("window closed")
    return 0
