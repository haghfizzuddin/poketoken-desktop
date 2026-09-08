"""Design tokens: the one place colours, spacing, radii, type sizes, sprite tiers and
breakpoints are defined. The drawing layer reads these instead of literals so a value is
changed once and applies everywhere.

The palettes are Apple's system colours, as before — the visual identity is unchanged. Only
the muted greys were lifted, because at their old values secondary text failed a contrast
check on both grounds.
"""
from __future__ import annotations

# ------------------------------------------------------------------ palettes
LIGHT = dict(bg="#F2F2F7", card="#FFFFFF", sep="#E5E5EA", fill="#E9E9EB", fill2="#F4F4F6",
             label="#1C1C1E", secondary="#5C5C61", tertiary="#8A8A8E",
             blue="#007AFF", green="#34C759", orange="#FF9500", red="#FF3B30", yellow="#B98900",
             purple="#AF52DE", pink="#FF2D55", teal="#32ADE6", gray="#8E8E93",
             seg="#E3E3E8", segsel="#FFFFFF", onaccent="#FFFFFF")
DARK = dict(bg="#000000", card="#1C1C1E", sep="#2C2C2E", fill="#2C2C2E", fill2="#242426",
            label="#FFFFFF", secondary="#A8A8AE", tertiary="#8E8E93",
            blue="#0A84FF", green="#30D158", orange="#FF9F0A", red="#FF453A", yellow="#FFD60A",
            purple="#BF5AF2", pink="#FF375F", teal="#64D2FF", gray="#8E8E93",
            seg="#1C1C1E", segsel="#48484A", onaccent="#FFFFFF")

# semantic roles (§14): blue = actions, green = live/money, orange = progression,
# red = defeat, grey = inactive. Named here so a page never picks a colour by feel.
ROLE = {"action": "blue", "live": "green", "money": "green", "progress": "orange",
        "danger": "red", "inactive": "gray", "locked": "tertiary"}

RARITY_COLOR = {"common": "gray", "uncommon": "green", "rare": "purple", "legendary": "orange"}
STATE_COLOR = {"egg": "yellow", "sleep": "gray", "idle": "blue", "working": "green",
               "focus": "orange", "tired": "red", "levelUp": "pink"}
STATE_LABEL = {"egg": "Incubating", "sleep": "Sleeping", "idle": "Idle", "working": "Working",
               "focus": "In the zone", "tired": "Tired", "levelUp": "Level up!"}
TYPE_COLORS = {"normal": "#A8A77A", "fire": "#EE8130", "water": "#6390F0", "electric": "#C79A00", "grass": "#5CA337",
               "ice": "#5AA9A6", "fighting": "#C22E28", "poison": "#A33EA1", "ground": "#B08A2E", "flying": "#8E77E0",
               "psychic": "#F95587", "bug": "#8A9A16", "rock": "#9A8A2E", "ghost": "#735797", "dragon": "#6F35FC",
               "dark": "#705746", "steel": "#77779A", "fairy": "#D685AD"}

# ------------------------------------------------------------------ metrics
SPACE = {"xs": 4, "sm": 8, "md": 12, "lg": 16, "xl": 24, "xxl": 32}
PAD_CARD = 16           # card inner padding (was a hand-typed 18 in most places)
PAD_CARD_TIGHT = 12     # …on narrow viewports
RADIUS = {"card": 16, "cell": 14, "control": 9, "pill": 999}
ROW_H = 44              # a list row / minimum touch target
ROW_H_TOUCH = 48        # …on touch-sized viewports
BTN_H = 30
BTN_H_TOUCH = 36
GAP = 12                # between cards and grid cells
MAX_CONTENT = 1240      # page content never grows past this, centred (§1)
MAX_READING = 620       # single-column reading pages (species) stay this narrow

# type scale: (size, weight). One ladder, so a page never invents a font size.
TYPE = {"largeTitle": (24, "bold"), "title": (18, "bold"), "title2": (15, "bold"),
        "headline": (12, "bold"), "body": (12, "normal"), "sub": (11, "normal"),
        "caption": (10, "normal"), "captionB": (10, "bold"), "num": (28, "bold"), "numSm": (22, "bold")}
TYPE_COMPACT = dict(TYPE, largeTitle=(19, "bold"), title=(16, "bold"), num=(24, "bold"), numSm=(19, "bold"),
                    caption=(10, "normal"), captionB=(10, "bold"))

# sprite tiers (§11) — pixel art, whole-number scaling only
SPRITE = {"hero": 256, "battle": 112, "dex": 88, "line": 52, "card": 40, "micro": 24}
SPRITE_BOXES = (192, 256, 320, 384)   # hero sizes offered in the menu
DEFAULT_SPRITE_BOX = 256
SPRITE_PAD = 12

# ------------------------------------------------------------------ breakpoints
# canvas width, not screen width: the window is the viewport (§1)
BREAKPOINTS = (("xs", 0), ("sm", 480), ("md", 768), ("lg", 1200))


def breakpoint_for(width: int) -> str:
    """'xs' (<480: compact, reduced density) · 'sm' (single column) · 'md' (two columns)
    · 'lg' (>=1200: full desktop)."""
    name = "xs"
    for key, floor in BREAKPOINTS:
        if width >= floor:
            name = key
    return name


def is_compact(width: int) -> bool:
    """Below 'sm': secondary information collapses rather than shrinking."""
    return breakpoint_for(width) == "xs"


def is_touch(width: int) -> bool:
    """Narrow enough to deserve touch-sized controls."""
    return breakpoint_for(width) in ("xs", "sm")


def card_pad(width: int) -> int:
    return PAD_CARD_TIGHT if is_compact(width) else PAD_CARD


def row_height(width: int) -> int:
    return ROW_H_TOUCH if is_touch(width) else ROW_H


def button_height(width: int) -> int:
    return BTN_H_TOUCH if is_touch(width) else BTN_H


def type_scale(width: int) -> dict:
    return TYPE_COMPACT if is_compact(width) else TYPE
