"""Layout arithmetic for the window, kept pure so it is testable without a display:
how many card columns fit a width, where each column sits, masonry placement of cards
into the shortest column, ranked bar lists, and short model names."""
from __future__ import annotations

import re

MARGIN = 16          # window edge to first card
GAP = 12             # between columns
MIN_COL = 340        # narrowest useful card column
MAX_COLS = 3


def columns_for(width: int, max_cols: int = MAX_COLS) -> int:
    usable = width - 2 * MARGIN
    return max(1, min(max_cols, (usable + GAP) // (MIN_COL + GAP)))


def column_geometry(width: int, cols: int) -> list[tuple[float, float]]:
    """[(x, w)] for each column across the usable width."""
    usable = width - 2 * MARGIN
    w = (usable - GAP * (cols - 1)) / cols
    return [(MARGIN + i * (w + GAP), w) for i in range(cols)]


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
