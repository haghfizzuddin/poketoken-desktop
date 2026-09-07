"""Companion game state — port of `CompanionModel.swift` and the core loop of
`CompanionStore.swift` (ledger → egg incubation → hatch → evolve → graduate → Pokédex,
plus the Shop and Bag).

Not ported: Ditto disguise/reveal, Rare Candy grants from official limit windows
(needs the claude.ai limits API), save-transfer envelopes. The JSON save uses upstream's
field names but is not byte-compatible with the macOS app's Codable output.
"""
from __future__ import annotations

import json
import os
import random
import statistics
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from .pokeapi import EvoLine, EvoNode, PokeAPI, PokeAPIError, rarity_rank, rarity_includes

# ------------------------------------------------------------------ balance (PokemonBalance & friends)
EGG_HATCH_THRESHOLD = 5_000_000
GRADUATION_TOTAL = {
    "common": 750_000_000,
    "uncommon": 1_875_000_000,
    "rare": 3_000_000_000,
    "legendary": 6_000_000_000,
}
RARE_CANDY_XP = 100_000_000
RARE_CANDY_PRICE = 500_000_000
RARE_CANDY_WEEKLY_GRANT = 5
MINT_PRICE = 100_000_000
SHINY_CHARM_PRICE = 3_000_000_000
FRESH_EGG_PRICE = 1_000_000_000
SHINY_DENOMINATOR = 64
SHINY_CHARM_DENOMINATOR = 48
DITTO_DISGUISE_DENOMINATOR = 128       # common, multi-stage hatches only
DITTO_ID = 132
EGG_TIERS: list[str | None] = [None, "uncommon", "rare"]

# activity history (phase 2): streaks and the weekly goal are computed from this
HISTORY_DAYS = 120
STREAK_MIN_TOKENS = 1_000_000          # a day "counts" toward the streak at 1M+ tokens
WEEKLY_GOAL_MIN = 50_000_000           # the weekly goal never drops below this
WEEKLY_GOAL_LOOKBACK = 4               # median of up to this many previous complete weeks
WEEKLY_GOAL_MIN_WEEKS = 2              # ...but needs at least this many to unlock

# Rare Candy grants (replaces upstream's limit-window grants): consistency, not volume
STREAK_MILESTONES = [(3, 1), (7, 2), (14, 3), (30, 5)]   # (streak days, candies)
STREAK_REPEAT_DAYS = 30                                   # past 30: every further 30 days pays 5
STREAK_REPEAT_CANDY = 5


def streak_milestones_reached(days: int) -> list[tuple[int, int]]:
    out = [(m, c) for m, c in STREAK_MILESTONES if days >= m]
    k = 2
    while STREAK_REPEAT_DAYS * k <= days:
        out.append((STREAK_REPEAT_DAYS * k, STREAK_REPEAT_CANDY))
        k += 1
    return out


def next_streak_milestone(days: int) -> tuple[int, int]:
    for m, c in STREAK_MILESTONES:
        if days < m:
            return m, c
    nxt = (days // STREAK_REPEAT_DAYS + 1) * STREAK_REPEAT_DAYS
    return nxt, STREAK_REPEAT_CANDY

NATURES = ["hardy", "lonely", "brave", "adamant", "naughty",
           "bold", "docile", "relaxed", "impish", "lax",
           "timid", "hasty", "serious", "jolly", "naive",
           "modest", "mild", "quiet", "bashful", "rash",
           "calm", "gentle", "sassy", "careful", "quirky"]

# stats (phase 2): six IVs 0-31 rolled at hatch, level 5..100 from growth, games' formula
STAT_KEYS = ["hp", "attack", "defense", "special-attack", "special-defense", "speed"]
STAT_LABELS = {"hp": "HP", "attack": "Attack", "defense": "Defense", "special-attack": "Sp. Atk",
               "special-defense": "Sp. Def", "speed": "Speed"}
# nature -> (raised stat, lowered stat); neutral natures raise and lower nothing
NATURE_MODS: dict[str, tuple[str | None, str | None]] = {
    "hardy": (None, None), "lonely": ("attack", "defense"), "brave": ("attack", "speed"),
    "adamant": ("attack", "special-attack"), "naughty": ("attack", "special-defense"),
    "bold": ("defense", "attack"), "docile": (None, None), "relaxed": ("defense", "speed"),
    "impish": ("defense", "special-attack"), "lax": ("defense", "special-defense"),
    "timid": ("speed", "attack"), "hasty": ("speed", "defense"), "serious": (None, None),
    "jolly": ("speed", "special-attack"), "naive": ("speed", "special-defense"),
    "modest": ("special-attack", "attack"), "mild": ("special-attack", "defense"),
    "quiet": ("special-attack", "speed"), "bashful": (None, None), "rash": ("special-attack", "special-defense"),
    "calm": ("special-defense", "attack"), "gentle": ("special-defense", "defense"),
    "sassy": ("special-defense", "speed"), "careful": ("special-defense", "special-attack"), "quirky": (None, None),
}
IV_MAX = 31
LEVEL_MIN, LEVEL_MAX = 5, 100
# luck: consistency and efficiency during incubation, never raw volume
LUCK_STREAK_ROLL = 7            # streak days for one bonus IV roll (two at double)
LUCK_CACHE_RATIO_ROLL = 0.70    # 7-day mean cache-read ratio for one bonus roll (two at 0.90)
LUCK_SHINY_STREAK = 7           # streak days that cut the shiny denominator by a quarter


def ditto_disguise_hit(rarity: str, total_forms: int, roll: int) -> bool:
    """A Ditto hides only in common lines that could evolve; 1 in 128 of those hatches."""
    return rarity == "common" and total_forms >= 2 and roll % DITTO_DISGUISE_DENOMINATOR == 0


def stat_value(key: str, base: int, iv: int, level: int, nature: str | None) -> int:
    """Gen 3+ formula without EVs. HP has its own shape; nature is +10% / -10%."""
    core = (2 * base + iv) * level // 100
    if key == "hp":
        return core + level + 10
    up, down = NATURE_MODS.get(nature or "", (None, None))
    mod = 1.1 if key == up else 0.9 if key == down else 1.0
    return int((core + 5) * mod)


def nature_mod(key: str, nature: str | None) -> int:
    up, down = NATURE_MODS.get(nature or "", (None, None))
    return 1 if key == up else -1 if key == down else 0


ITEMS = {
    "rareCandy":  {"label": "Rare Candy",  "emoji": "🍬", "price": RARE_CANDY_PRICE,  "passive": False,
                   "blurb": f"+{RARE_CANDY_XP // 1_000_000}M growth for your Pokémon"},
    "mint":       {"label": "Mint",        "emoji": "🌿", "price": MINT_PRICE,        "passive": False,
                   "blurb": "re-roll your Pokémon's nature"},
    "shinyCharm": {"label": "Shiny Charm", "emoji": "✨", "price": SHINY_CHARM_PRICE, "passive": True,
                   "blurb": f"shiny odds 1/{SHINY_DENOMINATOR} → 1/{SHINY_CHARM_DENOMINATOR}, permanent"},
}
STATE_EMOJI = {"egg": "🥚", "sleep": "💤", "idle": "🐾", "working": "⚡", "focus": "🔥",
               "tired": "😮‍💨", "levelUp": "🎉"}


def phase_threshold(rarity: str, total_forms: int, stage_index: int) -> int:
    """Tokens needed at `stage_index` (0-based) before the next form / graduation.
    Form i of k costs T·i / (k(k+1)/2), so a line always sums to T regardless of length."""
    k = max(1, total_forms)
    i = stage_index + 1
    return int(round(GRADUATION_TOTAL[rarity] * i / (k * (k + 1) / 2)))


def egg_price(tier: str | None) -> int:
    if tier is None:
        return FRESH_EGG_PRICE
    return int(round(FRESH_EGG_PRICE * GRADUATION_TOTAL[tier] / GRADUATION_TOTAL["common"]))


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ------------------------------------------------------------------ persisted types
@dataclass
class MonState:
    base_id: int
    path_ids: list[int]
    planned_path_ids: list[int]
    stage_index: int = 0
    used_at_stage: int = 0
    rarity: str = "common"
    total_forms: int = 1
    is_shiny: bool = False
    nature: str | None = None
    ivs: dict[str, int] | None = None          # rolled at hatch (older saves: None → shown as unknown)
    luck: dict | None = None                    # what influenced the roll, for display
    ditto_disguise: int | None = None          # species this Ditto is posing as (None = not a Ditto)
    ditto_revealed: bool = False

    @property
    def is_disguised(self) -> bool:
        return self.ditto_disguise is not None and not self.ditto_revealed

    @property
    def shiny_visible(self) -> bool:
        """A disguised Ditto hides its shininess until the reveal."""
        return self.is_shiny and not self.is_disguised

    @property
    def current_id(self) -> int:
        if not self.path_ids:
            return self.base_id
        return self.path_ids[min(self.stage_index, len(self.path_ids) - 1)]

    def to_dict(self) -> dict:
        return {"baseID": self.base_id, "pathIDs": self.path_ids, "plannedPathIDs": self.planned_path_ids,
                "stageIndex": self.stage_index, "usedAtStage": self.used_at_stage, "rarity": self.rarity,
                "totalForms": self.total_forms, "isShiny": self.is_shiny, "nature": self.nature,
                "ivs": self.ivs, "luck": self.luck,
                "dittoDisguise": self.ditto_disguise, "dittoRevealed": self.ditto_revealed}

    @staticmethod
    def from_dict(d) -> "MonState | None":
        try:
            path = [int(x) for x in d["pathIDs"]]
            if not path:
                return None
            planned = [int(x) for x in d.get("plannedPathIDs") or []] or list(path)
            stage = min(max(0, int(d["stageIndex"])), len(path) - 1)
            rarity = d["rarity"] if d.get("rarity") in GRADUATION_TOTAL else "common"
            ivs = d.get("ivs")
            ivs = ({k: int(v) for k, v in ivs.items() if k in STAT_KEYS}
                   if isinstance(ivs, dict) and all(k in ivs for k in STAT_KEYS) else None)
            luck = d.get("luck") if isinstance(d.get("luck"), dict) else None
            dd = d.get("dittoDisguise")
            return MonState(int(d["baseID"]), path, planned, stage, int(d.get("usedAtStage", 0)), rarity,
                            int(d.get("totalForms") or len(planned)), bool(d.get("isShiny", False)),
                            d.get("nature") if d.get("nature") in NATURES else None, ivs, luck,
                            int(dd) if isinstance(dd, int) and not isinstance(dd, bool) else None,
                            bool(d.get("dittoRevealed", False)))
        except (KeyError, TypeError, ValueError, AttributeError):
            return None


@dataclass
class DexEntry:
    id: str
    base_id: int
    final_id: int
    chain_order: list[int]
    rarity: str
    caught_at: str | None
    is_shiny: bool = False
    nature: str | None = None
    names: dict[int, dict[str, str]] | None = None
    released_at: str | None = None
    ivs: dict[str, int] | None = None

    @property
    def is_released(self) -> bool:
        return self.released_at is not None

    def name(self, sid: int, lang: str = "en") -> str:
        by = (self.names or {}).get(sid, {})
        return by.get(lang) or by.get("en") or f"#{sid}"

    def to_dict(self) -> dict:
        return {"id": self.id, "baseID": self.base_id, "finalID": self.final_id, "chainOrder": self.chain_order,
                "rarity": self.rarity, "caughtAt": self.caught_at, "isShiny": self.is_shiny, "nature": self.nature,
                "names": {str(k): v for k, v in self.names.items()} if self.names else None,
                "releasedAt": self.released_at, "ivs": self.ivs}

    @staticmethod
    def from_dict(d) -> "DexEntry | None":
        try:
            names = d.get("names")
            parsed = {int(k): dict(v) for k, v in names.items()} if isinstance(names, dict) else None
            ivs = d.get("ivs")
            ivs = ({k: int(v) for k, v in ivs.items() if k in STAT_KEYS}
                   if isinstance(ivs, dict) and all(k in ivs for k in STAT_KEYS) else None)
            return DexEntry(str(d.get("id") or uuid.uuid4()), int(d["baseID"]), int(d["finalID"]),
                            [int(x) for x in d["chainOrder"]], d["rarity"] if d.get("rarity") in GRADUATION_TOTAL else "common",
                            d.get("caughtAt"), bool(d.get("isShiny", False)),
                            d.get("nature") if d.get("nature") in NATURES else None, parsed, d.get("releasedAt"), ivs)
        except (KeyError, TypeError, ValueError, AttributeError):
            return None


@dataclass
class CompanionState:
    install_baseline_set: bool = False
    used_since_install: int = 0
    spent_tokens: int = 0
    egg_usage: int = 0
    egg_tier: str | None = None
    pending_hatch_id: int | None = None
    claimed_today_tokens_by_provider: dict[str, int] | None = None
    last_date: str = ""
    active: MonState | None = None
    dex: list[DexEntry] = field(default_factory=list)
    collected_finals: set[str] = field(default_factory=set)
    language: str = "en"
    inventory: dict[str, int] = field(default_factory=dict)
    candy_grant_tier: dict[str, int] = field(default_factory=dict)
    candy_feature_seeded: bool = False
    history: dict[str, dict] = field(default_factory=dict)      # "yyyy-MM-dd" -> usage.day_stats row
    history_backfilled: bool = False

    def to_dict(self) -> dict:
        return {
            "installBaselineSet": self.install_baseline_set, "usedSinceInstall": self.used_since_install,
            "spentTokens": self.spent_tokens, "eggUsage": self.egg_usage, "eggTier": self.egg_tier,
            "pendingHatchID": self.pending_hatch_id,
            "claimedTodayTokensByProvider": self.claimed_today_tokens_by_provider, "lastDate": self.last_date,
            "active": self.active.to_dict() if self.active else None, "dex": [d.to_dict() for d in self.dex],
            "collectedFinals": sorted(self.collected_finals), "language": self.language,
            "inventory": self.inventory, "candyGrantTier": self.candy_grant_tier,
            "candyFeatureSeeded": self.candy_feature_seeded,
            "history": self.history, "historyBackfilled": self.history_backfilled,
        }

    @staticmethod
    def from_dict(d: dict) -> "CompanionState":
        """Lenient: a damaged field falls back to its default instead of discarding the save."""
        def get(key, typ, default):
            v = d.get(key, default)
            return v if isinstance(v, typ) and not (typ is int and isinstance(v, bool)) else default

        s = CompanionState()
        s.install_baseline_set = get("installBaselineSet", bool, False)
        s.used_since_install = get("usedSinceInstall", int, 0)
        s.spent_tokens = get("spentTokens", int, 0)
        s.egg_usage = get("eggUsage", int, 0)
        s.egg_tier = d.get("eggTier") if d.get("eggTier") in GRADUATION_TOTAL else None
        s.pending_hatch_id = get("pendingHatchID", int, None) if d.get("pendingHatchID") is not None else None
        if "claimedTodayTokensByProvider" in d:
            raw = d.get("claimedTodayTokensByProvider")
            s.claimed_today_tokens_by_provider = ({str(k): int(v) for k, v in raw.items() if isinstance(v, int)}
                                                 if isinstance(raw, dict) else {})
        s.last_date = get("lastDate", str, "")
        s.active = MonState.from_dict(d["active"]) if isinstance(d.get("active"), dict) else None
        s.dex = [e for e in (DexEntry.from_dict(x) for x in get("dex", list, []) if isinstance(x, dict)) if e]
        s.collected_finals = {str(x) for x in get("collectedFinals", list, [])}
        s.language = get("language", str, "en")
        s.inventory = {str(k): int(v) for k, v in get("inventory", dict, {}).items() if isinstance(v, int)}
        s.candy_grant_tier = {str(k): int(v) for k, v in get("candyGrantTier", dict, {}).items() if isinstance(v, int)}
        s.candy_feature_seeded = get("candyFeatureSeeded", bool, False)
        s.history = {str(k): dict(v) for k, v in get("history", dict, {}).items()
                     if isinstance(v, dict) and isinstance(v.get("tokens"), int)}
        s.history_backfilled = get("historyBackfilled", bool, False)
        return s


# ------------------------------------------------------------------ the store
class Companion:
    def __init__(self, api: PokeAPI, state_path: Path, rng: random.Random | None = None,
                 clock: Callable[[], float] = time.time, log: Callable[[str], None] | None = None):
        self.api = api
        self.path = Path(state_path)
        self.rng = rng or random.SystemRandom()
        self.clock = clock
        self.log = log or (lambda msg: None)
        self.state = CompanionState()
        self.line: EvoLine | None = None
        self.display_state = "egg"
        self.just_evolved_to: str | None = None
        self.just_graduated: str | None = None
        self.event_until: float | None = None
        self.events: list[dict] = []
        self.today: str = datetime.now().strftime("%Y-%m-%d")
        self.load()

    # ----------------------------------------------------------- persistence
    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("top-level is not an object")
            self.state = CompanionState.from_dict(data)
        except (OSError, ValueError) as e:
            backup = self.path.with_name(f"{self.path.name}.corrupt-{int(self.clock())}")
            try:
                self.path.replace(backup)
            except OSError:
                pass
            self.log(f"state file unreadable ({e}); backed up to {backup.name}, starting fresh")
            self.state = CompanionState()

    def save(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.state.to_dict(), ensure_ascii=False, indent=1), "utf-8")
        os.replace(tmp, self.path)

    def _emit(self, kind: str, **kw) -> None:
        ev = {"kind": kind, "at": self.clock(), **kw}
        self.events.append(ev)
        self.log(f"{kind}: " + " ".join(f"{k}={v}" for k, v in kw.items()))

    def drain_events(self) -> list[dict]:
        ev, self.events = self.events, []
        return ev

    # -------------------------------------------------------------- derived
    @property
    def is_egg(self) -> bool:
        return self.state.active is None

    @property
    def egg_progress(self) -> float:
        return min(1.0, max(0.0, self.state.egg_usage / EGG_HATCH_THRESHOLD))

    @property
    def egg_tokens_to_hatch(self) -> int:
        return max(0, EGG_HATCH_THRESHOLD - self.state.egg_usage)

    @property
    def wallet(self) -> int:
        return max(0, self.state.used_since_install - self.state.spent_tokens)

    def item_count(self, kind: str) -> int:
        return self.state.inventory.get(kind, 0)

    @property
    def owns_shiny_charm(self) -> bool:
        return self.item_count("shinyCharm") > 0

    def ensure_line(self) -> EvoLine | None:
        a = self.state.active
        if a is None:
            self.line = None
            return None
        if self.line is not None and self.line.base_id == a.base_id:
            return self.line
        try:
            self.line = self.api.line(a.base_id)
        except PokeAPIError as e:
            self.log(f"line fetch failed for base {a.base_id}: {e}")
            self.line = None
        return self.line

    def display_name(self, lang: str | None = None) -> str:
        a = self.state.active
        if a is None:
            return "Token Egg"
        line = self.ensure_line()
        return line.name(a.current_id, lang or self.state.language) if line else f"#{a.current_id}"

    @property
    def threshold(self) -> int:
        a = self.state.active
        return phase_threshold(a.rarity, a.total_forms, a.stage_index) if a else EGG_HATCH_THRESHOLD

    @property
    def progress(self) -> float:
        a = self.state.active
        if a is None:
            return self.egg_progress
        return min(1.0, a.used_at_stage / self.threshold) if self.threshold else 1.0

    @property
    def tokens_to_next(self) -> int:
        a = self.state.active
        if a is None:
            return self.egg_tokens_to_hatch
        return max(0, self.threshold - a.used_at_stage)

    @property
    def is_final_stage(self) -> bool:
        a = self.state.active
        return a is not None and a.stage_index >= a.total_forms - 1

    @property
    def stage_text(self) -> str:
        a = self.state.active
        return f"{a.stage_index + 1}/{a.total_forms}" if a else ""

    def line_items(self) -> list[tuple[int | None, str]]:
        """(species_id | None for a still-hidden future form, 'done'|'current'|'future')."""
        a = self.state.active
        if a is None:
            return []
        items: list[tuple[int | None, str]] = []
        for i, sid in enumerate(a.path_ids[: a.stage_index + 1]):
            items.append((sid, "current" if i == a.stage_index else "done"))
        for _ in range(max(0, a.total_forms - len(items))):
            items.append((None, "future"))
        return items

    # ------------------------------------------------------------ main loop
    def update(self, today_tokens_by_provider: dict[str, int], today_date: str, burn_tier: str = "idle",
               limit_warning: bool = False, has_usage_data: bool = True) -> str:
        s = self.state
        self.today = today_date
        today_tokens = sum(today_tokens_by_provider.values())
        has_current = has_usage_data and bool(today_tokens_by_provider)

        if not s.install_baseline_set:
            if not has_current:
                self.display_state = "egg" if s.active is None else "idle"
                self.ensure_line()
                return self.display_state
            # Install baseline: usage before the first real observation never counts.
            s.install_baseline_set = True
            s.claimed_today_tokens_by_provider = dict(today_tokens_by_provider)
            s.last_date = today_date
            self.save()
        elif has_current:
            if s.claimed_today_tokens_by_provider is None:
                s.claimed_today_tokens_by_provider = dict(today_tokens_by_provider)
                s.last_date = today_date
            elif today_date != s.last_date:
                # New local day: yesterday's ledger is not comparable, credit today's whole total.
                s.last_date = today_date
                ledger = {k: 0 for k in s.claimed_today_tokens_by_provider}
                ledger.update(today_tokens_by_provider)
                s.claimed_today_tokens_by_provider = ledger
                self._credit(today_tokens)
            else:
                ledger = dict(s.claimed_today_tokens_by_provider)
                delta = 0
                for pid, cur in today_tokens_by_provider.items():
                    prev = ledger.get(pid)
                    if prev is None:                    # new provider: seed, never back-credit
                        ledger[pid] = cur
                        continue
                    if cur < prev:                      # log rotation/regression: rebase that line
                        self.log(f"usage regression provider={pid} prev={prev} cur={cur}; rebased")
                        ledger[pid] = cur
                        continue
                    delta += cur - prev
                    ledger[pid] = cur
                s.claimed_today_tokens_by_provider = ledger
                self._credit(delta)

        if s.install_baseline_set:
            self.evaluate_candy_grants(today_date)
        if self.event_until is not None and self.clock() > self.event_until:
            self.just_graduated = self.just_evolved_to = None
            self.event_until = None
        if s.active is None and s.install_baseline_set:
            self.ensure_egg_prefetch()
        if s.active is None and s.egg_usage >= EGG_HATCH_THRESHOLD:
            self.hatch_if_needed()
        if s.active is not None and self.line is None and self.ensure_line() is not None:
            self._process_thresholds(self.line)      # usage banked while offline may already evolve
        if s.active is not None and s.active.is_disguised and self.line is not None:
            self._process_thresholds(self.line)      # a reveal that failed offline gets another go
        self.display_state = self.compute_state(burn_tier, limit_warning, has_usage_data, today_tokens)
        self.save()
        return self.display_state

    def _credit(self, delta: int) -> None:
        if delta <= 0:
            return
        s = self.state
        s.used_since_install += delta
        if s.active is None:
            s.egg_usage += delta
        else:
            self.apply_usage(delta)

    def apply_usage(self, delta: int) -> None:
        """Add growth to the current Pokémon; evolve/graduate at thresholds (overflow carries)."""
        a = self.state.active
        if a is None:
            return
        a.used_at_stage += delta
        line = self.ensure_line()
        if line is None:
            self.save()
            return
        self._process_thresholds(line)
        self.save()

    def _process_thresholds(self, line: EvoLine) -> None:
        for _ in range(50):
            a = self.state.active
            if a is None:
                return
            thr = phase_threshold(a.rarity, a.total_forms, a.stage_index)
            if a.used_at_stage < thr:
                return
            node = line.tree.find(a.current_id)
            if node is None:
                return
            if a.is_disguised:
                if not self.reveal_ditto():
                    return                       # line fetch failed: keep the usage, retry next tick
                line = self.line
                continue
            if not node.children:
                self.graduate()
                return
            nxt_i = a.stage_index + 1
            planned = None
            if nxt_i < len(a.planned_path_ids):
                planned = next((c for c in node.children if c.species_id == a.planned_path_ids[nxt_i]), None)
            if planned is None:
                planned = self._pick_planned_child(node, a.base_id)
                a.planned_path_ids = a.path_ids[: a.stage_index + 1] + self._make_plan(planned, a.base_id)
                a.total_forms = len(a.planned_path_ids)
                self.log(f"evolve: repaired planned path for base {a.base_id}")
            a.path_ids = a.path_ids[: a.stage_index + 1] + [planned.species_id]
            a.stage_index += 1
            a.used_at_stage -= thr
            name = line.name(planned.species_id, self.state.language)
            self.just_evolved_to = name
            self.event_until = self.clock() + 4
            self._emit("evolve", species=planned.species_id, name=name, stage=f"{a.stage_index + 1}/{a.total_forms}")

    def _pick_planned_child(self, node: EvoNode, base_id: int) -> EvoNode:
        fresh = [c for c in node.children
                 if any(f"{base_id}:{f}" not in self.state.collected_finals for f in c.final_ids)]
        return self.rng.choice(fresh or node.children)

    def _make_plan(self, root: EvoNode, base_id: int) -> list[int]:
        plan, node = [root.species_id], root
        while node.children:
            node = self._pick_planned_child(node, base_id)
            plan.append(node.species_id)
        return plan

    def reveal_ditto(self) -> bool:
        """The disguise drops at the first evolution threshold: the Pokémon becomes Ditto (its own
        rarity, one form), keeps shiny/nature/IVs, and the threshold overflow carries over."""
        a = self.state.active
        if a is None or not a.is_disguised:
            return False
        try:
            ditto = self.api.line(DITTO_ID)
        except PokeAPIError as e:
            self.log(f"ditto reveal: line fetch failed: {e}")
            return False
        thr = phase_threshold(a.rarity, a.total_forms, a.stage_index)
        disguise_name = self.display_name()
        a.used_at_stage = max(0, a.used_at_stage - thr)
        a.base_id = ditto.base_id
        a.path_ids = [ditto.base_id]
        a.planned_path_ids = self._make_plan(ditto.tree, ditto.base_id)
        a.stage_index = 0
        a.rarity = ditto.rarity
        a.total_forms = len(a.planned_path_ids)
        a.ditto_revealed = True
        self.line = ditto
        self.just_evolved_to = ditto.name(ditto.base_id, self.state.language)
        self.event_until = self.clock() + 5
        self._emit("dittoReveal", disguise=disguise_name, shiny=a.is_shiny)
        return True

    def graduate(self) -> None:
        a = self.state.active
        if a is None:
            return
        final_id = a.current_id
        line = self.line
        self.state.collected_finals.add(f"{a.base_id}:{final_id}")
        self.state.dex.append(DexEntry(
            id=str(uuid.uuid4()), base_id=a.base_id, final_id=final_id, chain_order=list(a.path_ids),
            rarity=a.rarity, caught_at=_now_iso(), is_shiny=a.is_shiny, nature=a.nature,
            names={sid: dict(line.names[sid]) for sid in a.path_ids if sid in line.names} if line else None,
            ivs=a.ivs))
        name = line.name(final_id, self.state.language) if line else f"#{final_id}"
        self.just_graduated = name
        self.event_until = self.clock() + 6
        self._emit("graduate", species=final_id, name=name, rarity=a.rarity, shiny=a.is_shiny)
        self.state.active = None
        self.line = None
        self.state.egg_usage = 0
        self.ensure_egg_prefetch()

    # -------------------------------------------------------------- hatching
    def choose_base(self) -> int | None:
        tier = self.state.egg_tier
        try:
            index = self.api.base_index()
        except PokeAPIError as e:
            self.log(f"base index unavailable ({e}); REST fallback")
            index = None
        if index:
            pool = [(i, c) for i, c in index if tier is None or rarity_includes(tier, c)]
            if not pool:
                self.log(f"no candidates for guaranteed {tier}; egg kept")
                return None
            weights = [max(1, c // 2) if any(f.startswith(f"{i}:") for f in self.state.collected_finals) else max(1, c)
                       for i, c in pool]
            r = self.rng.randrange(sum(weights))
            for (sid, _), w in zip(pool, weights):
                r -= w
                if r < 0:
                    return sid
            return pool[-1][0]
        try:
            return self.api.random_base_via_rest(self.rng, tier)
        except PokeAPIError as e:
            self.log(f"REST fallback failed: {e}")
            return None

    def ensure_egg_prefetch(self) -> None:
        s = self.state
        if s.active is not None:
            return
        if s.pending_hatch_id is None:
            base = self.choose_base()
            if base is None:
                return
            s.pending_hatch_id = base
            self.save()
        try:
            line = self.api.line(s.pending_hatch_id)
            self.api.sprite(line.base_id, animated=True, shiny=False)
        except PokeAPIError:
            pass

    def hatch_if_needed(self) -> bool:
        s = self.state
        if s.active is not None or s.egg_usage < EGG_HATCH_THRESHOLD:
            return False
        base = s.pending_hatch_id if s.pending_hatch_id is not None else self.choose_base()
        if base is None:
            return False
        try:
            line = self.api.line(base)
        except PokeAPIError as e:
            self.log(f"hatch: line fetch failed for {base}: {e}; egg kept")
            return False
        if s.egg_tier is not None and rarity_rank(line.rarity) < rarity_rank(s.egg_tier):
            self.log(f"hatch: rolled {line.rarity} below guaranteed {s.egg_tier}; re-rolling next tick")
            s.pending_hatch_id = None
            self.save()
            return False
        overflow = max(0, s.egg_usage - EGG_HATCH_THRESHOLD)
        s.egg_usage = 0
        s.egg_tier = None
        s.pending_hatch_id = None
        luck = self.luck_signals(self.today)
        is_shiny = self.rng.getrandbits(64) % luck["shinyDenominator"] == 0
        nature = self.rng.choice(NATURES)
        ivs = self.roll_ivs(luck["bonusRolls"])
        plan = self._make_plan(line.tree, line.base_id)
        disguise = line.base_id if ditto_disguise_hit(line.rarity, len(plan), self.rng.getrandbits(64)) else None
        s.active = MonState(base_id=line.base_id, path_ids=[line.base_id], planned_path_ids=plan,
                            stage_index=0, used_at_stage=0, rarity=line.rarity, total_forms=len(plan),
                            is_shiny=is_shiny, nature=nature, ivs=ivs, luck=luck, ditto_disguise=disguise)
        self.line = line
        self.just_evolved_to = None
        self.event_until = self.clock() + 4
        self._emit("hatch", species=line.base_id, name=line.name(line.base_id, s.language),
                   rarity=line.rarity, shiny=is_shiny and disguise is None, nature=nature, forms=len(plan),
                   ivs=sum(ivs.values()), bonus_rolls=luck["bonusRolls"])
        if overflow > 0:
            self.apply_usage(overflow)
        self.save()
        return True

    def compute_state(self, burn_tier: str, limit_warning: bool, has_usage_data: bool, today: int) -> str:
        if self.state.active is None:
            return "egg"
        if self.just_graduated is not None or (self.event_until is not None and self.clock() < self.event_until):
            return "levelUp"
        if limit_warning:
            return "tired"
        if not has_usage_data or today == 0:
            return "sleep"
        return {"idle": "idle", "normal": "working"}.get(burn_tier, "focus")

    # --------------------------------------------------------- luck & stats
    def luck_signals(self, today: str) -> dict:
        """Consistency and efficiency at hatch time → bonus IV rolls and better shiny odds."""
        streak, _, _ = self.streak(today)
        d = date.fromisoformat(today)
        ratios = [float(self.state.history.get((d - timedelta(days=i)).isoformat(), {}).get("cacheRatio", 0.0))
                  for i in range(7) if self.day_tokens((d - timedelta(days=i)).isoformat()) > 0]
        cache_ratio = round(sum(ratios) / len(ratios), 3) if ratios else 0.0
        bonus = (1 if streak >= LUCK_STREAK_ROLL else 0) + (1 if streak >= 2 * LUCK_STREAK_ROLL else 0)
        bonus += (1 if cache_ratio >= LUCK_CACHE_RATIO_ROLL else 0) + (1 if cache_ratio >= 0.90 else 0)
        denom = SHINY_CHARM_DENOMINATOR if self.owns_shiny_charm else SHINY_DENOMINATOR
        if streak >= LUCK_SHINY_STREAK:
            denom = max(1, denom * 3 // 4)
        return {"streak": streak, "cacheRatio": cache_ratio, "bonusRolls": bonus,
                "shinyDenominator": denom, "charm": self.owns_shiny_charm}

    def roll_ivs(self, bonus_rolls: int) -> dict[str, int]:
        """Each IV is the best of 1 + bonus_rolls uniform rolls (a floor-raiser, never a cap)."""
        return {k: max(self.rng.randrange(IV_MAX + 1) for _ in range(1 + max(0, bonus_rolls))) for k in STAT_KEYS}

    def total_progress(self) -> float:
        a = self.state.active
        if a is None:
            return 0.0
        done = sum(phase_threshold(a.rarity, a.total_forms, i) for i in range(a.stage_index))
        return min(1.0, (done + a.used_at_stage) / GRADUATION_TOTAL[a.rarity])

    def level(self) -> int:
        return LEVEL_MIN + int(round((LEVEL_MAX - LEVEL_MIN) * self.total_progress()))

    @staticmethod
    def stats_rows(meta: dict, ivs: dict[str, int] | None, level: int, nature: str | None) -> list[dict]:
        rows = []
        for k in STAT_KEYS:
            base = int(meta.get("stats", {}).get(k, 0))
            iv = None if ivs is None else int(ivs.get(k, 0))
            rows.append({"key": k, "label": STAT_LABELS[k], "base": base, "iv": iv,
                         "value": stat_value(k, base, iv if iv is not None else 0, level, nature),
                         "mod": nature_mod(k, nature)})
        return rows

    def stats_view(self, meta: dict) -> dict | None:
        """Stats card data for the active Pokémon (meta = PokeAPI.pokemon(current_id))."""
        a = self.state.active
        if a is None or not meta or int(meta.get("id", -1)) != a.current_id:
            return None
        lvl = self.level()
        return {"level": lvl, "rows": self.stats_rows(meta, a.ivs, lvl, a.nature), "types": list(meta.get("types", [])),
                "abilities": list(meta.get("abilities", [])), "height_m": meta.get("height", 0) / 10,
                "weight_kg": meta.get("weight", 0) / 10, "nature": a.nature, "ivs": a.ivs, "luck": a.luck,
                "iv_total": sum(a.ivs.values()) if a.ivs else None}

    # ------------------------------------------------------------- history
    def record_history(self, day_rows: dict[str, dict], today: str, backfill: bool = False) -> None:
        """Merge per-day usage rows into the save (rows given win), prune to HISTORY_DAYS."""
        self.state.history.update({d: dict(r) for d, r in day_rows.items()})
        cutoff = (date.fromisoformat(today) - timedelta(days=HISTORY_DAYS)).isoformat()
        self.state.history = {d: r for d, r in self.state.history.items() if d >= cutoff}
        if backfill:
            self.state.history_backfilled = True
        self.save()

    def day_tokens(self, day: str) -> int:
        return int(self.state.history.get(day, {}).get("tokens", 0))

    def streak(self, today: str) -> tuple[int, str | None, bool]:
        """(length, first day, today already counts). A streak survives the current day until
        midnight even if today is still below STREAK_MIN_TOKENS."""
        d = date.fromisoformat(today)
        today_counts = self.day_tokens(today) >= STREAK_MIN_TOKENS
        if not today_counts:
            d -= timedelta(days=1)
        length, start = 0, None
        while self.day_tokens(d.isoformat()) >= STREAK_MIN_TOKENS:
            length += 1
            start = d.isoformat()
            d -= timedelta(days=1)
        return length, start, today_counts

    @staticmethod
    def week_key(day: str) -> str:
        y, w, _ = date.fromisoformat(day).isocalendar()
        return f"{y}-W{w:02d}"

    def week_totals(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for d, r in self.state.history.items():
            k = self.week_key(d)
            out[k] = out.get(k, 0) + int(r.get("tokens", 0))
        return out

    def weekly_goal(self, today: str) -> dict:
        """Target = median of the previous complete weeks (up to WEEKLY_GOAL_LOOKBACK), floor
        WEEKLY_GOAL_MIN; unlocks after WEEKLY_GOAL_MIN_WEEKS of history."""
        totals = self.week_totals()
        this_week = self.week_key(today)
        current = totals.get(this_week, 0)
        previous = sorted((k for k in totals if k < this_week), reverse=True)[:WEEKLY_GOAL_LOOKBACK]
        if len(previous) < WEEKLY_GOAL_MIN_WEEKS:
            return {"week": this_week, "current": current, "target": None, "progress": 0.0,
                    "weeks_needed": WEEKLY_GOAL_MIN_WEEKS - len(previous)}
        target = max(WEEKLY_GOAL_MIN, int(statistics.median(totals[k] for k in previous)))
        return {"week": this_week, "current": current, "target": target,
                "progress": min(1.0, current / target), "weeks_needed": 0}

    def evaluate_candy_grants(self, today: str) -> list[dict]:
        """Pay satisfied windows once. Streak milestones track the highest one paid in the current
        run (`streak_paid`), so a run whose start shifts earlier (late log entries) does not
        re-pay; a run that starts later is a new run and pays from 3 days again. Weekly goals
        are keyed by ISO week. The first evaluation only seeds: windows already satisfied at
        install never pay retroactively (upstream's rule)."""
        s = self.state
        t = s.candy_grant_tier
        grants: list[dict] = []
        streak_due: list[tuple[int, int]] = []
        n, start, _ = self.streak(today)
        if start:
            start_i = int(start.replace("-", ""))
            if t.get("streak_start", 0) == 0 or start_i > t.get("streak_start", 0):
                t["streak_start"], t["streak_paid"] = start_i, 0        # a new run
            elif start_i < t["streak_start"]:
                t["streak_start"] = start_i                            # same run, extended backward
            paid = t.get("streak_paid", 0)
            streak_due = [(m, c) for m, c in streak_milestones_reached(n) if m > paid]
        g = self.weekly_goal(today)
        week_key = f"week:{g['week']}"
        week_due = bool(g["target"]) and g["progress"] >= 1 and week_key not in t
        if not s.candy_feature_seeded:
            if streak_due:
                t["streak_paid"] = max(m for m, _ in streak_due)
            if week_due:
                t[week_key] = 1
            s.candy_feature_seeded = True
            return []
        for m, c in streak_due:
            t["streak_paid"] = m
            s.inventory["rareCandy"] = s.inventory.get("rareCandy", 0) + c
            self._emit("candy", count=c, reason=f"{m}-day streak")
            grants.append({"key": f"streak:{m}", "count": c, "reason": f"{m}-day streak"})
        if week_due:
            t[week_key] = 1
            s.inventory["rareCandy"] = s.inventory.get("rareCandy", 0) + RARE_CANDY_WEEKLY_GRANT
            self._emit("candy", count=RARE_CANDY_WEEKLY_GRANT, reason="weekly goal reached")
            grants.append({"key": week_key, "count": RARE_CANDY_WEEKLY_GRANT, "reason": "weekly goal reached"})
        return grants

    # ---------------------------------------------------------------- shop
    def shop_entries(self) -> list[dict]:
        rows = [{"key": k, "label": v["label"], "emoji": v["emoji"], "price": v["price"], "blurb": v["blurb"],
                 "owned": self.item_count(k), "passive": v["passive"]} for k, v in ITEMS.items()]
        for tier in EGG_TIERS:
            label = {None: "Pokémon Egg", "uncommon": "Uncommon Egg", "rare": "Rare Egg"}[tier]
            blurb = {None: "send off your companion, start over",
                     "uncommon": "guaranteed Uncommon or better",
                     "rare": "guaranteed Rare or better"}[tier]
            rows.append({"key": f"egg:{tier or 'plain'}", "label": label, "emoji": "🥚", "price": egg_price(tier),
                         "blurb": blurb, "owned": 0, "passive": False})
        return sorted(rows, key=lambda r: r["price"])

    def buy(self, key: str) -> tuple[bool, str]:
        if key.startswith("egg:"):
            tier = key.split(":", 1)[1]
            return self.buy_egg(None if tier in ("plain", "none", "") else tier)
        item = ITEMS.get(key)
        if item is None:
            return False, f"unknown item {key!r}"
        if item["passive"] and self.item_count(key) > 0:
            return False, f"{item['label']} is already owned (one-time purchase)"
        if self.wallet < item["price"]:
            return False, f"not enough tokens: need {item['price']:,}, have {self.wallet:,}"
        self.state.spent_tokens += item["price"]
        self.state.inventory[key] = self.item_count(key) + 1
        self._emit("buy", item=key)
        self.save()
        return True, f"bought {item['label']}"

    def buy_egg(self, tier: str | None) -> tuple[bool, str]:
        if tier is not None and tier not in ("uncommon", "rare"):
            return False, "egg tiers: plain, uncommon, rare"
        price = egg_price(tier)
        if self.wallet < price:
            return False, f"not enough tokens: need {price:,}, have {self.wallet:,}"
        s = self.state
        self.state.spent_tokens += price
        released = None
        if s.active is not None:
            a = s.active
            reached = a.path_ids[: max(1, a.stage_index + 1)] or [a.base_id]
            released = self.display_name()
            s.dex.append(DexEntry(id=str(uuid.uuid4()), base_id=a.base_id, final_id=reached[-1],
                                  chain_order=list(reached), rarity=a.rarity, caught_at=_now_iso(),
                                  is_shiny=a.is_shiny, nature=a.nature,
                                  names={sid: dict(self.line.names[sid]) for sid in reached if sid in self.line.names}
                                  if self.line else None, released_at=_now_iso()))
            s.active = None
            self.line = None
        s.egg_usage = 0
        s.egg_tier = tier
        s.pending_hatch_id = None
        self._emit("egg", tier=tier or "plain", released=released)
        self.save()
        self.ensure_egg_prefetch()
        return True, f"new {tier or 'plain'} egg" + (f" — {released} was released" if released else "")

    # ----------------------------------------------------------------- bag
    def use_rare_candy(self) -> tuple[bool, str]:
        if self.state.active is None:
            return False, "no Pokémon to feed (egg)"
        if self.item_count("rareCandy") <= 0:
            return False, "no Rare Candy in the bag"
        if self.ensure_line() is None:
            return False, "evolution line unavailable (offline?), try again later"
        before = (self.state.active.current_id, self.state.active.stage_index)
        self.state.inventory["rareCandy"] -= 1
        self.apply_usage(RARE_CANDY_XP)       # growth only; never counts as real usage
        a = self.state.active
        if a is None:
            return True, "graduated!"
        if (a.current_id, a.stage_index) != before:
            return True, f"evolved into {self.display_name()}"
        return True, f"+{RARE_CANDY_XP // 1_000_000}M growth ({self.tokens_to_next:,} to next)"

    def use_mint(self) -> tuple[bool, str]:
        a = self.state.active
        if a is None:
            return False, "no Pokémon (egg)"
        if self.item_count("mint") <= 0:
            return False, "no Mint in the bag"
        self.state.inventory["mint"] -= 1
        choices = [n for n in NATURES if n != a.nature]
        a.nature = self.rng.choice(choices)
        self._emit("mint", nature=a.nature)
        self.save()
        return True, f"nature is now {a.nature.title()}"
