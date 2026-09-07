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

from . import companion as C, fmt, instance

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
TABS = [("home", "Home"), ("dex", "Pokédex"), ("shop", "Shop"), ("bag", "Bag")]
SPRITE_BOX = 96
FULL_GEOMETRY = "392x700"
COMPACT_GEOMETRY = "272x352"


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
        out[("anim", a.current_id, a.is_shiny)] = api.sprite(a.current_id, animated=True, shiny=a.is_shiny)
        for sid in a.path_ids[: a.stage_index + 1]:
            out[("static", sid, a.is_shiny)] = api.sprite(sid, animated=False, shiny=a.is_shiny)
    wanted: set[tuple[int, bool]] = set()
    for e in s.dex[-80:]:
        for sid in e.chain_order:
            wanted.add((sid, e.is_shiny))
    for sid, shiny in wanted:
        out[("static", sid, shiny)] = api.sprite(sid, animated=False, shiny=shiny)
    for name in ("rare-candy", "shiny-charm"):
        out[("item", name)] = api.item_sprite(name)
    return out


class PokeWindow:
    def __init__(self, app, compact: bool = False, dark: bool | None = None, interval: int = 30):
        self.app = app
        self.interval = max(10, interval)
        self.prefs_file: Path = app.dir / "ui.json"
        prefs = self._load_prefs()
        self.compact = compact if compact else bool(prefs.get("compact", False)) and compact
        self.dark = bool(prefs.get("dark", False)) if dark is None else dark
        self.tab = "home"                                   # always open on Home
        self.detail: int | None = None                      # species shown in the Pokédex detail page
        self.sprite_scale = int(prefs["sprite_scale"]) if prefs.get("sprite_scale") in (2, 3, 4) else 3
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
        size_menu = tk.Menu(self.menu, tearoff=0)
        for n in (2, 3, 4):
            size_menu.add_command(label=f"{n}×  ({SPRITE_BOX * n} px)", command=lambda n=n: self.set_sprite_scale(n))
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
        size = SPRITE_BOX * self.sprite_scale
        return f"{max(272, size + 64)}x{size + 176}" if self.compact else FULL_GEOMETRY

    def _restore_geometry(self, prefs: dict) -> str:
        """Saved size for this view, unless it is too narrow for the current sprite scale."""
        geo = str(prefs.get("geometry_compact" if self.compact else "geometry_full", "")).split("+")[0]
        try:
            w = int(geo.split("x")[0])
        except ValueError:
            return self.default_geometry()
        if self.compact and w < SPRITE_BOX * self.sprite_scale + 40:
            return self.default_geometry()
        return geo

    def set_sprite_scale(self, n: int) -> None:
        self.sprite_scale = n
        self.images.clear()
        self.sprite_key = None
        if self.compact:
            self.root.geometry(self.default_geometry())
        self._save_prefs()
        self.render()

    def _save_prefs(self) -> None:
        p = self._load_prefs()
        p.update({"dark": self.dark, "tab": self.tab, "sprite_scale": self.sprite_scale})
        if self.settled_geometry:
            p["geometry_compact" if self.compact else "geometry_full"] = self.settled_geometry
        try:
            self.prefs_file.write_text(json.dumps(p))
        except OSError:
            pass

    def _pick_font(self) -> str:
        fams = set(tkfont.families(self.root))
        for f in FONT_PREFS:
            if f in fams:
                return f
        return "TkDefaultFont"

    # ------------------------------------------------------------ worker
    def refresh(self) -> None:
        if self.busy:
            return
        self.busy = True
        # The worker must not capture `self`: if it ended up holding the last reference to the
        # Tk root, Tk would be torn down from a non-main thread (Tcl_AsyncDelete abort).
        app, q, lock = self.app, self.q, self.lock
        extra = ((self.detail, self._species_shiny(self.detail)),) if self.detail else ()

        def work():
            try:
                with lock:
                    snap = app.tick()
                    events = app.companion.drain_events()
                paths = resolve_sprites(app, extra)
                q.put(("ok", {"snap": snap, "events": events, "paths": paths, "at": time.time()}))
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
                    for ev in payload["events"]:
                        self._toast(self._event_text(ev))
                    self.render()
                else:
                    self.app.log(f"ui refresh error: {payload}")
                    self._toast("Refresh failed — see events.log")
                    self.render()
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
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()

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
                ok, msg = (self.app.companion.use_rare_candy() if key == "rareCandy"
                           else self.app.companion.use_mint())
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
            if self.tab == "dex" and self.detail is not None:
                y = self.draw_detail(y, x0, cw)
            else:
                y = {"home": self.draw_home, "dex": self.draw_dex, "shop": self.draw_shop,
                     "bag": self.draw_bag}[self.tab](y, x0, cw)
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
        self.text(16, y, "PokeToken", "largeTitle", "label")
        # "more" button (three dots in a circle)
        cx, cy = w - 30, y + 16
        self.c.create_oval(cx - 14, cy - 14, cx + 14, cy + 14, fill=self.P["fill"], outline="", tags=("more",))
        for dx in (-6, 0, 6):
            self.c.create_oval(cx + dx - 1.6, cy - 1.6, cx + dx + 1.6, cy + 1.6, fill=self.P["secondary"],
                               outline="", tags=("more",))
        self.c.tag_bind("more", "<Button-1>", self._show_menu)
        self._hand("more")
        y += 44
        # segmented control
        x, segw, h = 16, w - 32, 32
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
        return y + h + 14

    # ------------------------------------------------------------------ home
    def draw_home(self, y, x0, cw) -> int:
        comp, s = self.app.companion, self.app.companion.state
        snap = self.payload["snap"] if self.payload else None
        state = comp.display_state
        accent = STATE_COLOR.get(state, "blue")

        # hero card
        card = self.card(x0, y, cw, 10)
        cy = y + 12
        size = SPRITE_BOX * self.sprite_scale
        self.sprite_subject = ("egg",) if s.active is None else ("mon", s.active.current_id, s.active.is_shiny)
        self.sprite_item = self.c.create_image(x0 + cw / 2, cy + size / 2, image="")
        cy += size + 4
        name = comp.display_name()
        name_font = "title" if self.measure(name, "title") < cw - 40 else "title2"
        if s.active and s.active.is_shiny:
            tw = self.measure(name, name_font) + 6 + self.measure("✦", "title2")
            self.text(x0 + cw / 2 - tw / 2, cy, name, name_font, "label", anchor="nw")
            self.text(x0 + cw / 2 + tw / 2, cy + 3, "✦", "title2", "yellow", anchor="ne")
        else:
            self.text(x0 + cw / 2, cy, name, name_font, "label", anchor="n")
        cy += 34
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
        cy += 34
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
        cy += 20
        self.capsule(x0 + 18, cy, cw - 36, 8, frac, accent)
        cy += 16
        used = s.active.used_at_stage if s.active else s.egg_usage
        self.text(x0 + 18, cy, f"{fmt.compact(used)} / {fmt.compact(comp.threshold)}  ·  {fmt.percent(frac * 100)}",
                  "caption", "tertiary")
        if not s.install_baseline_set:
            self.text(x0 + cw - 18, cy, "waiting for first usage", "caption", "tertiary", anchor="ne")
        cy += 24
        self.fit_card(card, x0, y, cw, cy - y)
        y = cy + 12

        # evolution line
        if s.active and comp.line:
            items = comp.line_items()
            card = self.card(x0, y, cw, 118)
            self.text(x0 + 18, y + 12, "EVOLUTION LINE", "captionB", "secondary")
            n = max(1, len(items))
            each = (cw - 24) / n
            for i, (sid, st) in enumerate(items):
                cx = x0 + 12 + each * i + each / 2
                ty = y + 34
                if st == "current":
                    self.rrect(cx - 34, ty - 4, cx + 34, ty + 72, 12,
                               fill=_blend(self.P[accent], self.P["card"], 0.86 if not self.dark else 0.75))
                if sid is None:
                    self.c.create_oval(cx - 24, ty + 2, cx + 24, ty + 50, fill=self.P["fill"], outline="")
                    self.text(cx, ty + 26, "?", "title2", "tertiary", anchor="center")
                    label = "???"
                else:
                    ph = self.img(self.payload["paths"].get(("static", sid, s.active.is_shiny)) if self.payload else None,
                                  52, "card")
                    if ph:
                        self.c.create_image(cx, ty + 26, image=ph)
                    else:
                        self.text(cx, ty + 26, f"#{sid}", "caption", "tertiary", anchor="center")
                    label = comp.line.name(sid, s.language)
                self.text(cx, ty + 56, self._ellipsize(label, "caption", each - 8), "caption",
                          "label" if st == "current" else "secondary", anchor="n")
                if i < n - 1:
                    self.text(x0 + 12 + each * (i + 1), ty + 26, "›", "title2", "tertiary", anchor="center")
            y += 118 + 12

        # today card
        card = self.card(x0, y, cw, 10)
        cy = y + 12
        self.text(x0 + 18, cy, "TODAY", "captionB", "secondary")
        if snap:
            self.text(x0 + cw - 18, cy, snap.today_date, "caption", "tertiary", anchor="ne")
        cy += 18
        t = snap.today if snap else None
        self.text(x0 + 18, cy, fmt.compact(t.total) if t else "—", "num", "label")
        self.text(x0 + 18 + self.measure(fmt.compact(t.total) if t else "—", "num") + 8, cy + 20, "tokens", "sub", "secondary")
        self.text(x0 + cw - 18, cy + 8, fmt.cost(t.cost) if t else "—", "title", "green", anchor="ne")
        cy += 48
        if t:
            grid = [("Input", t.input), ("Output", t.output), ("Cache write", t.cache_write), ("Cache read", t.cache_read)]
            colw = (cw - 36) / 2
            for i, (lbl, val) in enumerate(grid):
                gx = x0 + 18 + (i % 2) * colw
                gy = cy + (i // 2) * 34
                self.text(gx, gy, lbl, "caption", "secondary")
                self.text(gx, gy + 12, fmt.compact(val), "headline", "label")
            cy += 70
            if snap.models_today:
                line = "  ·  ".join(f"{m.replace('claude-', '')} {fmt.compact(n)}" for m, n in list(snap.models_today.items())[:3])
                self.text(x0 + 18, cy, self._ellipsize(line, "caption", cw - 36), "caption", "tertiary")
                cy += 16
        cy += 8
        self.fit_card(card, x0, y, cw, cy - y)
        y = cy + 12

        # activity card
        rows = []
        if snap:
            tpm = snap.tokens_per_minute
            rows.append(("Burn rate", f"{fmt.compact(int(tpm))}/min · {snap.burn_tier}" if tpm and tpm > 1000 else "quiet"))
            if snap.block:
                mins = (snap.now.timestamp() - (snap.block_start or snap.now.timestamp())) / 60
                rows.append(("5-hour block", f"{fmt.compact(snap.block.total)} · {fmt.cost(snap.block.cost)} · {mins:.0f} min"))
            rows.append(("This week", f"{fmt.compact(snap.week.total)} · {fmt.cost(snap.week.cost)}"))
            rows.append(("This month", f"{fmt.compact(snap.month.total)} · {fmt.cost(snap.month.cost)}"))
        if snap:
            n, _, counts = comp.streak(snap.today_date)
            nxt_days, nxt_candy = C.next_streak_milestone(n)
            rows.append(("Streak", f"{n} day{'s' if n != 1 else ''} · +{nxt_candy} candy at {nxt_days}" + ("" if counts else " · not yet today") if n else "none yet"))
            g = comp.weekly_goal(snap.today_date)
            rows.append(("Weekly goal", f"{fmt.compact(g['current'])} / {fmt.compact(g['target'])} · {fmt.percent(g['progress'] * 100)}"
                         if g["target"] else f"unlocks in {g['weeks_needed']} week(s)"))
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
        return y + h + 12

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
                d["shiny"] = d["shiny"] or a.is_shiny
                d["raising"] = True
        if not species:
            self.card(x0, y, cw, 140)
            self.text(x0 + cw / 2, y + 52, "No Pokémon yet", "title2", "label", anchor="center")
            self.text(x0 + cw / 2, y + 82, "Graduate your first companion to fill the Pokédex.", "caption", "secondary", anchor="center")
            return y + 152
        self.text(x0 + 2, y, f"{len(species)} species", "captionB", "secondary")
        y += 20
        cols = 3
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
        return bool(a and a.is_shiny and sid in a.path_ids[: a.stage_index + 1])

    def open_detail(self, sid: int) -> None:
        self.detail = sid
        self.c.yview_moveto(0)
        self.render()
        self.refresh()                       # fetch this species' animated sprite in the background

    def close_detail(self) -> None:
        self.detail = None
        self.c.yview_moveto(0)
        self.render()

    def draw_detail(self, y, x0, cw) -> int:
        comp, s = self.app.companion, self.app.companion.state
        sid = self.detail
        shiny = self._species_shiny(sid)
        entries = [e for e in s.dex if sid in e.chain_order]
        a = s.active
        raising = bool(a and sid in a.path_ids[: a.stage_index + 1])
        # name / chain from whatever record knows this species
        name, chain = f"#{sid}", [sid]
        if entries:
            name, chain = entries[0].name(sid, s.language), list(entries[0].chain_order)
        elif raising and comp.line:
            name, chain = comp.line.name(sid, s.language), list(a.path_ids[: a.stage_index + 1])
        rarity = entries[0].rarity if entries else (a.rarity if raising and a else "common")

        self.text(x0, y + 4, "‹ Pokédex", "headline", "blue", tags=("back",))
        self.c.tag_bind("back", "<Button-1>", lambda e: self.close_detail())
        self._hand("back")
        self.text(x0 + cw, y + 6, f"#{sid:03d}", "captionB", "tertiary", anchor="ne")
        y += 30

        card = self.card(x0, y, cw, 10)
        cy = y + 12
        size = SPRITE_BOX * self.sprite_scale
        self.sprite_subject = ("mon", sid, shiny)
        self.sprite_item = self.c.create_image(x0 + cw / 2, cy + size / 2, image="")
        cy += size + 4
        self.text(x0 + cw / 2, cy, name + ("  ✦" if shiny else ""), "title", "yellow" if shiny else "label", anchor="n")
        cy += 34
        pills = [(rarity.title(), RARITY_COLOR[rarity])]
        if shiny:
            pills.append(("Shiny owned", "yellow"))
        if raising:
            pills.append(("Raising now", "blue"))
        grads = sum(1 for e in entries if not e.is_released and e.final_id == sid)
        if grads:
            pills.append((f"Graduated ×{grads}", "green"))
        total = sum(self.measure(t, "captionB") + 16 for t, _ in pills) + 6 * (len(pills) - 1)
        px = x0 + cw / 2 - total / 2
        for t, colr in pills:
            px += self.pill(px, cy, t, colr) + 6
        cy += 36
        self.fit_card(card, x0, y, cw, cy - y)
        y = cy + 12

        # evolution line of the record
        self.card(x0, y, cw, 118)
        self.text(x0 + 18, y + 12, "EVOLUTION LINE", "captionB", "secondary")
        n = max(1, len(chain))
        each = (cw - 24) / n
        for i, cid in enumerate(chain):
            cx = x0 + 12 + each * i + each / 2
            ty = y + 34
            if cid == sid:
                self.rrect(cx - 34, ty - 4, cx + 34, ty + 72, 12,
                           fill=_blend(self.P["blue"], self.P["card"], 0.86 if not self.dark else 0.75))
            ph = self.img(self.payload["paths"].get(("static", cid, self._species_shiny(cid))) if self.payload else None, 52, "card")
            if ph:
                self.c.create_image(cx, ty + 26, image=ph)
            label = entries[0].name(cid, s.language) if entries else (comp.line.name(cid, s.language) if comp.line else f"#{cid}")
            self.text(cx, ty + 56, self._ellipsize(label, "caption", each - 8), "caption",
                      "label" if cid == sid else "secondary", anchor="n")
            if i < n - 1:
                self.text(x0 + 12 + each * (i + 1), ty + 26, "›", "title2", "tertiary", anchor="center")
            tag = f"dex:{cid}"
            if cid != sid:
                self.c.tag_bind(tag, "<Button-1>", lambda e, c=cid: self.open_detail(c))
        y += 118 + 12

        # records
        rows = []
        if raising and a:
            rows.append((f"Raising · stage {a.stage_index + 1} of {a.total_forms}", (a.nature or "").title(), "now", "blue"))
        for e in sorted(entries, key=lambda e: e.caught_at or "", reverse=True):
            rows.append((("released" if e.is_released else "graduated") + f" as {e.name(e.final_id, s.language)}",
                         (e.nature or "").title(), (e.caught_at or "")[:10], "gray" if e.is_released else "green"))
        h = 12 + 38 * max(1, len(rows)) + 4
        self.card(x0, y, cw, h)
        self.text(x0 + 18, y + 12, "RECORDS", "captionB", "secondary")
        ry = y + 30
        if not rows:
            self.text(x0 + 18, ry, "no records", "sub", "tertiary")
        for i, (what, nature, when, colr) in enumerate(rows):
            self.dot(x0 + 22, ry + 9, 3.5, colr)
            self.text(x0 + 32, ry, self._ellipsize(what, "body", cw - 150), "body", "label")
            self.text(x0 + cw - 18, ry + 1, f"{nature} · {when}".strip(" ·"), "caption", "secondary", anchor="ne")
            if i < len(rows) - 1:
                self.sep(x0 + 18, ry + 30, cw - 36)
            ry += 38
        return y + h + 12

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
        sprite = {"rareCandy": "rare-candy", "shinyCharm": "shiny-charm"}.get(key)
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
            if it["passive"]:
                self.pill(bx + bw - self.measure("Active", "captionB") - 16, by + 2, "Active", "green")
            elif comp.is_egg:
                self.button(bx, by, bw, 26, "Use", tag, "disabled")
            elif self.armed and self.armed[0] == tag:
                self.button(bx, by, bw, 26, "Use?", tag, "armed")
            else:
                self.button(bx, by, bw, 26, "Use", tag, "filled")
            if i < len(items) - 1:
                self.sep(x0 + 66, ry + 67, cw - 82)
            ry += 68
        return y + h + 12

    # -------------------------------------------------------------- compact
    def draw_compact(self, w) -> int:
        comp, s = self.app.companion, self.app.companion.state
        snap = self.payload["snap"] if self.payload else None
        state = comp.display_state
        accent = STATE_COLOR.get(state, "blue")
        size = SPRITE_BOX * self.sprite_scale
        y = 10
        self.sprite_subject = ("egg",) if s.active is None else ("mon", s.active.current_id, s.active.is_shiny)
        self.sprite_item = self.c.create_image(w / 2, y + size / 2, image="")
        y += size
        name = comp.display_name()
        self.text(w / 2, y, name + ("  ✦" if s.active and s.active.is_shiny else ""), "title2",
                  "yellow" if s.active and s.active.is_shiny else "label", anchor="n")
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
        at = datetime.fromtimestamp(self.payload["at"]).strftime("%H:%M:%S") if self.payload else "…"
        self.text(x0 + cw / 2, y + 4, f"Updated {at}  ·  refresh {self.interval}s  ·  Claude Code", "caption", "tertiary", anchor="n")
        self.text(x0 + cw / 2, y + 20, "Refresh now", "captionB", "blue", anchor="n", tags=("refresh",))
        self.c.tag_bind("refresh", "<Button-1>", lambda e: self.refresh())
        self._hand("refresh")
        self.c.bind("<Button-3>", self._show_menu)
        return y + 44

    # ------------------------------------------------------------- animation
    def _start_sprite_animation(self) -> None:
        if self.sprite_item is None:
            return
        comp = self.app.companion
        subject = self.sprite_subject
        key = subject + (self.dark, self.compact, self.sprite_scale)
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
        self.frames, self.durations, self.frame_idx = [], [], 0
        if not path or not Path(path).exists():
            return
        scale = self.sprite_scale
        bg = _rgb(self.P[bg_key])
        try:
            im = Image.open(path)
            for frame in ImageSequence.Iterator(im):
                fr = frame.convert("RGBA")
                if max(fr.size) > SPRITE_BOX:
                    fr.thumbnail((SPRITE_BOX, SPRITE_BOX), Image.NEAREST)
                box = Image.new("RGBA", (SPRITE_BOX, SPRITE_BOX), bg)
                box.alpha_composite(fr, ((SPRITE_BOX - fr.width) // 2, SPRITE_BOX - fr.height - 6))
                self.frames.append(ImageTk.PhotoImage(box.resize((SPRITE_BOX * scale, SPRITE_BOX * scale), Image.NEAREST)))
                self.durations.append(int(frame.info.get("duration", 100)) or 100)
                if static:
                    break
        except (OSError, ValueError) as e:
            self.app.log(f"sprite load failed {path}: {e}")
            self.frames = []

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
