"""Helpers behind the window's Battle tab: the battle record (`<state_dir>/battles.json`), the
hit-by-hit HP schedule the arena animates, and small pure formatting pieces.

Nothing here touches Tk; the drawing lives in `ui.PokeWindow.draw_battle`. The record is kept
outside the companion save on purpose: it is window bookkeeping, not game state.
"""
from __future__ import annotations

import json
import queue
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import battle as B, settings

HISTORY_FILE = "battles.json"
HISTORY_KEEP = 10
TICK_MS = 350            # one hit of the fight, at a relaxed pace
MAX_FIGHT_MS = 14_000    # long fights tick faster so the whole thing stays watchable
MIN_TICK_MS = 100
MAX_SPECIES = 1025

# "T3: Pikachu used a Electric move for 41 — super effective!  (Squirtle 12 HP)"
_HIT = re.compile(r"^T(\d+): (.*) used a \S+ move for \d+.*\((.*) (\d+) HP\)$")


# ---------------------------------------------------------------- record
def history_path(state_dir) -> Path:
    return Path(state_dir) / HISTORY_FILE


def prune_history(records: list, keep: int = HISTORY_KEEP) -> list[dict]:
    """The newest `keep` well-formed entries, oldest first."""
    good = [r for r in records if isinstance(r, dict)]
    return good[-keep:] if keep > 0 else []


def load_history(state_dir) -> list[dict]:
    """Records oldest first; a missing, unreadable or corrupt file is an empty record."""
    try:
        data = json.loads(history_path(state_dir).read_text("utf-8"))
    except (OSError, ValueError):
        return []
    return prune_history(data) if isinstance(data, list) else []


def save_history(state_dir, records: list[dict]) -> None:
    p = history_path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(records, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(p)


def append_history(state_dir, record: dict) -> list[dict]:
    """Append one battle, prune to HISTORY_KEEP and save; returns the new record list."""
    records = prune_history(load_history(state_dir) + [record])
    save_history(state_dir, records)
    return records


def tally(records: list[dict]) -> tuple[int, int]:
    """(wins, losses)."""
    wins = sum(1 for r in records if r.get("won"))
    return wins, len(records) - wins


def won(res: dict, mine: dict) -> bool:
    """Did `mine` win? By identity when `mine` is one of the two fighters (two colleagues'
    cards can be equal dicts), by equality for a copy."""
    w, l = res["winner"], res["loser"]
    if mine is w or mine is l:
        return mine is w
    return w == mine


def record_from_result(res: dict, mine: dict, other: dict, when: date | None = None) -> dict:
    return {"opponent": other["name"], "trainer": other.get("trainer", "?"), "mine": mine["name"],
            "won": won(res, mine), "winner": res["winner"]["name"], "turns": int(res["turns"]),
            "date": (when or date.today()).isoformat(),
            "power": [B.power_score(mine), B.power_score(other)]}


# ------------------------------------------------------------------ arena
def hp_schedule(res: dict, card_a: dict, card_b: dict) -> list[tuple[int, int]]:
    """HP of (a, b) before the fight and after every hit in res["log"], so the arena can
    deplete the bars one hit at a time. Read back from the log lines simulate() writes; when
    both cards carry the same name the faster side is taken to strike first each turn."""
    hp = [int(card_a["stats"]["hp"]), int(card_b["stats"]["hp"])]
    out = [tuple(hp)]
    same = card_a["name"] == card_b["name"]
    a_first = int(card_a["stats"]["speed"]) >= int(card_b["stats"]["speed"])
    last_turn, hits = None, 0
    for line in res.get("log", []):
        m = _HIT.match(line)
        if not m:
            continue
        turn, defender, left = int(m.group(1)), m.group(3), int(m.group(4))
        hits = hits + 1 if turn == last_turn else 1
        last_turn = turn
        if not same:
            idx = 0 if defender == card_a["name"] else 1
        else:
            idx = (1 if a_first else 0) if hits == 1 else (0 if a_first else 1)
        hp[idx] = min(hp[idx], left)
        out.append(tuple(hp))
    return out


def hp_color(frac: float) -> str:
    """Palette key for an HP bar: green above half, orange above a fifth, red below."""
    return "green" if frac > 0.5 else "orange" if frac > 0.2 else "red"


def tick_ms(steps: int) -> int:
    """Delay between hits: TICK_MS, shortened so a long fight finishes within MAX_FIGHT_MS."""
    return max(MIN_TICK_MS, min(TICK_MS, MAX_FIGHT_MS // max(1, steps)))


def sprite_key(card: dict | None) -> tuple[int, bool] | None:
    """(species_id, shiny) to look a card's sprite up with, or None when the card carries no
    usable species (cards from other builds may not)."""
    if not card:
        return None
    sid = card.get("species")
    if isinstance(sid, bool) or not isinstance(sid, int) or not 1 <= sid <= MAX_SPECIES:
        return None
    return sid, bool(card.get("shiny"))


def banner(res: dict, mine: dict, other: dict) -> dict:
    """Result banner text: title, one line about the winner, one about power."""
    w, l = res["winner"], res["loser"]
    turns = int(res["turns"])
    left = res.get("remaining", {}).get(w["name"], 0)
    return {"won": won(res, mine),
            "title": "Victory!" if won(res, mine) else "Defeat",
            "detail": f"{w['name']} ({w.get('trainer', '?')}) beat {l['name']} ({l.get('trainer', '?')})",
            "power": f"{turns} turn{'s' if turns != 1 else ''} · {left} HP left · "
                     f"power {B.power_score(mine)} vs {B.power_score(other)}"}


_HIT_FULL = re.compile(r"^T(\d+): (.*) used a (\S+) move for (\d+)(?: — ([^(]*?))?\s+\((.*) (\d+) HP\)$")


def hit_row(line: str) -> tuple[str, str]:
    """A log line as a (left, right) pair for one row of the log card: who hit with what for
    how much on the left, the HP the target has left on the right (in a duel the target is
    implied). Unparsable lines come back whole."""
    m = _HIT_FULL.match(line)
    if not m:
        return line, ""
    turn, atk, mtype, dmg, note, _dfd, left = m.groups()
    note = (note or "").strip().rstrip("!")
    return f"T{turn}  {atk} · {mtype} · {dmg} dmg" + (f" · {note}" if note else ""), f"{left} HP"


def own_card(comp, meta: dict | None, state_dir) -> dict | None:
    """The active Pokémon's card, trainer name from settings (what `poketoken card` uses);
    None while it is an egg or the species meta has not loaded."""
    if meta is None:
        return None
    trainer = settings.get(state_dir, "trainer") or B.default_trainer()
    return B.make_card(comp, meta, trainer)


def is_own_hit(line: str, name: str) -> bool:
    """Was this log line one of `name`'s attacks? (Log lines start "T<n>: <attacker> used".)"""
    return line.split(": ", 1)[-1].startswith(f"{name} used ")


# ------------------------------------------------------------------ state
@dataclass
class BattleState:
    """Everything the Battle tab remembers between renders."""
    challenger: dict | None = None      # the pasted card, once it decodes
    error: str = ""                     # inline message under the paste field
    mine: dict | None = None            # my card as it stood when the fight started
    result: dict | None = None          # battle.simulate() output
    schedule: list = field(default_factory=list)
    step: int = 0                       # index into schedule the arena currently shows
    pending: bool = False               # waiting for the type chart
    recorded: bool = False              # this fight is already in battles.json
    history: list | None = None         # cached battles.json (None = not read yet)
    arena_y: float = 0.0                # canvas y of the arena card, to scroll the fight into view
    job = None                          # Tk after() id of the poll / tick
    q: queue.Queue = field(default_factory=queue.Queue)

    @property
    def finished(self) -> bool:
        return self.result is not None and self.step >= len(self.schedule) - 1

    def reset_fight(self) -> None:
        """Forget the current fight (new card, Clear); the challenger and the record stay."""
        self.mine = self.result = None
        self.schedule, self.step, self.pending, self.recorded = [], 0, False, False
        while True:
            try:
                self.q.get_nowait()
            except queue.Empty:
                break
