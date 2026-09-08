"""Layout arithmetic for the window, kept pure so it is testable without a display:
the content frame for a viewport, how many card columns fit, masonry placement into the
shortest column, item grids, ranked bar lists, and short model names."""
from __future__ import annotations

import re

from .theme import GAP, MAX_CONTENT, breakpoint_for, is_compact

MARGIN = 16          # window edge to first card
MARGIN_COMPACT = 12
MIN_COL = 340        # narrowest useful card column
MAX_COLS = 3


def content_frame(width: int, max_width: int = MAX_CONTENT) -> tuple[float, float]:
    """(x, w) of the centred content area for a viewport of `width`: full width minus the
    margins, never wider than `max_width` (§1 — desktop centres, it does not stretch forever)."""
    margin = MARGIN_COMPACT if is_compact(width) else MARGIN
    usable = max(80, width - 2 * margin)
    w = min(usable, max_width)
    return (width - w) / 2, w


def grid(width: float, min_cell: float, max_cols: int = 8, gap: float = GAP) -> tuple[int, float]:
    """(columns, cell width) for a row of equal cells at least `min_cell` wide."""
    cols = max(1, min(max_cols, int((width + gap) // (min_cell + gap))))
    return cols, (width - gap * (cols - 1)) / cols


def cell_xy(i: int, cols: int, cell_w: float, cell_h: float, x0: float, y0: float,
            gap: float = GAP) -> tuple[float, float]:
    """Top-left of the i-th cell in a `cols`-wide grid."""
    return x0 + (i % cols) * (cell_w + gap), y0 + (i // cols) * (cell_h + gap)


def grid_height(count: int, cols: int, cell_h: float, gap: float = GAP) -> float:
    rows = max(0, -(-count // max(1, cols)))
    return rows * cell_h + max(0, rows - 1) * gap


def columns_for(width: int, max_cols: int = MAX_COLS) -> int:
    """Card columns a page of `width` supports (1 below 'md', up to `max_cols` beyond)."""
    bp = breakpoint_for(int(width))
    if bp in ("xs", "sm"):
        return 1
    if bp == "md":                                   # tablet / small desktop: two at most
        max_cols = min(max_cols, 2)
    usable = min(width, MAX_CONTENT) - 2 * MARGIN
    return max(1, min(max_cols, int((usable + GAP) // (MIN_COL + GAP))))


def column_geometry(width: int, cols: int) -> list[tuple[float, float]]:
    """[(x, w)] for each column across the centred content frame."""
    x0, usable = content_frame(int(width))
    w = (usable - GAP * (cols - 1)) / cols
    return [(x0 + i * (w + GAP), w) for i in range(cols)]


def masonry(heights: list[float], cols: int, pinned: dict[int, int] | None = None,
            gap: float = 10.0) -> list[tuple[int, float]]:
    """Place blocks in order into the currently shortest column (ties → leftmost).
    `pinned` forces block index → column. Returns [(column, y_offset)] per block."""
    tops = [0.0] * max(1, cols)
    out: list[tuple[int, float]] = []
    for i, h in enumerate(heights):
        col = pinned.get(i) if pinned and i in pinned else min(range(len(tops)), key=lambda c: (tops[c], c))
        col = min(col, len(tops) - 1)
        y = tops[col]
        out.append((col, y))
        tops[col] = y + h + gap
    return out


def evo_rows(pad: float, label_h: float, sprite: float, has_track: bool,
             gap: float = 8.0, lift: float = 6.0) -> dict:
    """Vertical geometry of the evolution row, relative to the card's top edge. Everything is
    derived from the measured label height so the highlight behind the current form can never
    start above the label's baseline box and paint over it."""
    label_top = pad - 4
    label_bottom = label_top + label_h
    row_top = label_bottom + gap                 # sprites start here
    highlight_top = row_top - lift               # the highlight is lifted, but never past the label
    name_top = row_top + sprite + 4              # the species name under the sprite
    row_bottom = name_top + 18
    track_top = row_bottom + 6 if has_track else None
    height = (row_bottom + (30 if has_track else 0)) + pad - 6
    return {"label_top": label_top, "label_bottom": label_bottom, "row_top": row_top,
            "highlight_top": highlight_top, "highlight_bottom": row_bottom - 2,
            "name_top": name_top, "row_bottom": row_bottom, "track_top": track_top, "height": height}


def top_n(items: dict[str, float], n: int, other_label: str = "other") -> list[tuple[str, float]]:
    """Largest n entries, the rest folded into one 'other' row."""
    ranked = sorted(items.items(), key=lambda kv: -kv[1])
    head, tail = ranked[:n], ranked[n:]
    rest = sum(v for _, v in tail)
    if rest > 0:
        head.append((other_label, rest))
    return head


_VERSION = re.compile(r"^(?P<family>[a-z]+)-(?P<major>\d+)(?:-(?P<minor>\d+))?(?:-\d{8})?$")


def short_model(model: str) -> str:
    """'claude-fable-5-1' → 'Fable 5.1', 'claude-haiku-4-5-20251001' → 'Haiku 4.5', unknown → as is."""
    name = model.removeprefix("claude-")
    m = _VERSION.match(name)
    if not m:
        return model
    version = m.group("major") + (f".{m.group('minor')}" if m.group("minor") else "")
    return f"{m.group('family').title()} {version}"
