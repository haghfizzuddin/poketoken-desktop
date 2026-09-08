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


def won(res: dict, mine: dict, side: int = 0) -> bool:
    """Did `mine` win? By side when simulate() recorded one (two identical cards are otherwise
    indistinguishable), then by identity, then by equality for a copy."""
    if "winner_side" in res:
        return int(res["winner_side"]) == side
    w, l = res["winner"], res["loser"]
    if mine is w or mine is l:
        return mine is w
    return w == mine


def record_from_result(res: dict, mine: dict, other: dict, when: date | None = None, side: int = 0) -> dict:
    return {"opponent": other["name"], "trainer": other.get("trainer", "?"), "mine": mine["name"],
            "won": won(res, mine, side), "winner": res["winner"]["name"], "turns": int(res["turns"]),
            "date": (when or date.today()).isoformat(),
            "power": [B.power_score(mine), B.power_score(other)]}


def record_line(r: dict, me: str | None = None) -> str:
    """One record as a sentence that says who fought whom and how it went — 'Quagsire beat
    Nidorino' — because a row of opponent names all under your own trainer name (you, fighting
    yourself) says nothing. The challenger's trainer is named only when it is someone else."""
    mine, other = r.get("mine") or "?", r.get("opponent") or "?"
    line = f"{mine} beat {other}" if r.get("won") else f"{mine} lost to {other}"
    trainer = r.get("trainer")
    if trainer and trainer not in (me, "?"):
        line += f" · {trainer}"
    return line


# ------------------------------------------------------------------ arena
def hp_schedule(res: dict, card_a: dict, card_b: dict) -> list[tuple[int, int]]:
    """HP of (a, b) before the fight and after every hit, so the arena can deplete the bars one
    hit at a time. Uses the per-side `hits` simulate() records; falls back to parsing the text
    log for results made before that existed (there, two cards with the same name can only be
    told apart by assuming the faster side struck first)."""
    hp = [int(card_a["stats"]["hp"]), int(card_b["stats"]["hp"])]
    out = [tuple(hp)]
    if res.get("hits"):
        for hit in res["hits"]:
            side = int(hit["defender"])
            hp[side] = min(hp[side], int(hit["hp"][side]))
            out.append(tuple(hp))
        return out
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


def banner(res: dict, mine: dict, other: dict, side: int = 0) -> dict:
    """Result banner: title, who beat whom, and the numbers. `side` is which fighter is mine
    (0 = the card passed first to simulate), so identical cards still resolve correctly."""
    w, l = res["winner"], res["loser"]
    turns = int(res["turns"])
    by_side = res.get("remaining_by_side")
    if by_side:
        left = int(by_side[int(res.get("winner_side", 0))])
    else:
        left = res.get("remaining", {}).get(w["name"], 0)
    i_won = won(res, mine, side)
    # with two identical cards the names alone read as nonsense ("Wooper beat Wooper"), so the
    # detail line names the trainers from my point of view instead
    if w["name"] == l["name"]:
        detail = (f"You beat {other.get('trainer', 'the challenger')}" if i_won
                  else f"{other.get('trainer', 'The challenger')} beat you")
    else:
        detail = f"{w['name']} ({w.get('trainer', '?')}) beat {l['name']} ({l.get('trainer', '?')})"
    return {"won": i_won,
            "title": "Victory!" if i_won else "Defeat",
            "detail": detail,
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


def fighter_sid(comp, state_dir) -> int | None:
    """Which Pokémon fights: the one chosen in settings when it is still owned, else whoever is
    being raised. None while there is nothing to field."""
    chosen = settings.get(state_dir, "fighter")
    if isinstance(chosen, int) and not isinstance(chosen, bool) and comp.state.owns_species(chosen):
        return chosen
    a = comp.state.active
    return a.current_id if a else None


def set_fighter(state_dir, sid: int | None) -> None:
    settings.set(state_dir, "fighter", sid)


def own_card(comp, meta: dict | None, state_dir, sid: int | None = None) -> dict | None:
    """The card you field: the active Pokémon as it stands, or a Pokédex record at level 100.
    None while it is an egg or that species' meta has not loaded."""
    if meta is None:
        return None
    trainer = settings.get(state_dir, "trainer") or B.default_trainer()
    a = comp.state.active
    sid = fighter_sid(comp, state_dir) if sid is None else sid
    if sid is None:
        return None
    if a is not None and sid == a.current_id:
        return B.make_card(comp, meta, trainer)
    return B.record_card(comp, sid, meta, trainer)


def is_own_hit(line: str, name: str) -> bool:
    """Was this log line one of `name`'s attacks? (Log lines start "T<n>: <attacker> used".)
    Ambiguous when both fighters share a name — prefer log_rows(), which reads the sides."""
    return line.split(": ", 1)[-1].startswith(f"{name} used ")


def log_rows(res: dict, mine_label: str = "You", other_label: str = "Rival") -> list[tuple[str, str, bool]]:
    """The fight log as (left, right, is_mine) rows. When simulate() recorded sides, the attacker
    is named by side, so two identically named cards still read as two different fighters."""
    hits = res.get("hits")
    if not hits:
        return [(*hit_row(line), False) for line in res.get("log", [])]
    same = res["winner"]["name"] == res["loser"]["name"]
    out = []
    for hit in hits:
        mine = int(hit["attacker"]) == 0
        who = (mine_label if mine else other_label) if same else \
            (res["winner"] if int(hit["attacker"]) == int(res.get("winner_side", 0)) else res["loser"])["name"]
        note = f" · {hit['note']}" if hit.get("note") else ""
        out.append((f"T{hit['turn']}  {who} · {str(hit['type']).title()} · {hit['damage']} dmg{note}",
                    f"{hit['hp'][int(hit['defender'])]} HP", mine))
    return out


# ------------------------------------------------------------------ state
@dataclass
class BattleState:
    """Everything the Battle tab remembers between renders."""
    challenger: dict | None = None      # the pasted card, once it decodes
    error: str = ""                     # inline message under the paste field
    mine: dict | None = None            # my card as fielded (scaled to the fight's level)
    other: dict | None = None           # the challenger as fielded
    flat: bool = True                   # scale both to one level for the fight
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
        self.mine = self.other = self.result = None
        self.schedule, self.step, self.pending, self.recorded = [], 0, False, False
        while True:
            try:
                self.q.get_nowait()
            except queue.Empty:
                break
