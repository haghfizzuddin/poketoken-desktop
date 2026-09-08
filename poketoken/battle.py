"""Battle cards: share your Pokémon as a short text token and fight a colleague's card.

Trust-based and for fun. Both sides run the same deterministic simulation (seeded from both
cards and the shared PokéAPI type chart), so they see the same result without a server.
"""
from __future__ import annotations

import base64
import getpass
import hashlib
import json
import math
import random
import zlib
from dataclasses import dataclass, field
from datetime import date

from . import companion as C

CARD_PREFIX = "PT1."
MOVE_POWER = 60
MAX_TURNS = 100
STAB = 1.5


# ------------------------------------------------------------------ cards
def make_card(comp: C.Companion, meta: dict, trainer: str) -> dict | None:
    """Snapshot of the active Pokémon as it stands right now."""
    view = comp.stats_view(meta)
    a = comp.state.active
    if view is None or a is None:
        return None
    return {
        "v": 1,
        "trainer": trainer[:24],
        "species": a.current_id,
        "name": comp.display_name(),
        "level": view["level"],
        "types": view["types"][:2],
        "stats": {r["key"]: r["value"] for r in view["rows"]},
        "nature": a.nature,
        "shiny": a.shiny_visible,
        "rarity": a.rarity,
        "ivTotal": view["iv_total"],
        "date": date.today().isoformat(),
    }


def encode_card(card: dict) -> str:
    raw = json.dumps(card, separators=(",", ":"), sort_keys=True).encode()
    return CARD_PREFIX + base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode().rstrip("=")


def decode_card(text: str) -> dict:
    text = text.strip()
    if not text.startswith(CARD_PREFIX):
        raise ValueError("not a PokeToken card (expected PT1.…)")
    body = text[len(CARD_PREFIX):]
    body += "=" * (-len(body) % 4)
    try:
        card = json.loads(zlib.decompress(base64.urlsafe_b64decode(body)))
    except (ValueError, zlib.error) as e:
        raise ValueError(f"corrupt card: {e}") from e
    return validate_card(card)


def validate_card(card) -> dict:
    if not isinstance(card, dict) or card.get("v") != 1:
        raise ValueError("unsupported card version")
    stats = card.get("stats")
    if not isinstance(stats, dict) or any(k not in stats for k in C.STAT_KEYS):
        raise ValueError("card is missing stats")
    for k in C.STAT_KEYS:
        v = stats[k]
        if not isinstance(v, int) or not 1 <= v <= 2000:
            raise ValueError(f"implausible {k}: {v!r}")
    types = card.get("types")
    if not isinstance(types, list) or not 1 <= len(types) <= 2 or not all(isinstance(t, str) for t in types):
        raise ValueError("card needs one or two types")
    if not isinstance(card.get("level"), int) or not 1 <= card["level"] <= 100:
        raise ValueError("bad level")
    card["name"] = str(card.get("name", "?"))[:32]
    card["trainer"] = str(card.get("trainer", "?"))[:24]
    return card


def card_summary(card: dict) -> str:
    st = card["stats"]
    return (f"{card['name']}{' ✦' if card.get('shiny') else ''} Lv {card['level']} · {'/'.join(t.title() for t in card['types'])}"
            f" · {card.get('trainer', '?')}  |  HP {st['hp']} Atk {st['attack']} Def {st['defense']}"
            f" SpA {st['special-attack']} SpD {st['special-defense']} Spe {st['speed']}")


# ----------------------------------------------------------------- battle
@dataclass
class Fighter:
    card: dict
    hp: int = field(init=False)

    def __post_init__(self):
        self.hp = self.card["stats"]["hp"]

    @property
    def name(self) -> str:
        return self.card["name"]

    @property
    def types(self) -> list[str]:
        return list(self.card["types"])

    def stat(self, key: str) -> int:
        return int(self.card["stats"][key])


def effectiveness(chart: dict, move_type: str, defender_types: list[str]) -> float:
    mult = 1.0
    for d in defender_types:
        mult *= chart.get(move_type, {}).get(d, 1.0)
    return mult


def best_move_type(chart: dict, attacker: Fighter, defender: Fighter) -> tuple[str, float]:
    """One move per own type plus a Normal fallback; pick the most effective (STAB included)."""
    best = ("normal", effectiveness(chart, "normal", defender.types))
    for t in attacker.types:
        eff = effectiveness(chart, t, defender.types) * STAB
        if eff > best[1]:
            best = (t, eff)
    return best


def damage(attacker: Fighter, defender: Fighter, multiplier: float, rng: random.Random) -> int:
    """Gen 5 shape with a fixed 60-power move; physical or special, whichever the attacker is better at."""
    if attacker.stat("attack") >= attacker.stat("special-attack"):
        a, d = attacker.stat("attack"), defender.stat("defense")
    else:
        a, d = attacker.stat("special-attack"), defender.stat("special-defense")
    level = attacker.card["level"]
    base = ((2 * level // 5 + 2) * MOVE_POWER * a // max(1, d)) // 50 + 2
    roll = rng.uniform(0.85, 1.0)
    return max(1 if multiplier > 0 else 0, int(base * multiplier * roll))


def battle_seed(card_a: dict, card_b: dict) -> int:
    keys = sorted(json.dumps(c, sort_keys=True, separators=(",", ":")) for c in (card_a, card_b))
    return int.from_bytes(hashlib.sha256("|".join(keys).encode()).digest()[:8], "big")


def simulate(card_a: dict, card_b: dict, chart: dict) -> dict:
    """Deterministic: the same two cards always produce the same fight on both machines."""
    rng = random.Random(battle_seed(card_a, card_b))
    a, b = Fighter(card_a), Fighter(card_b)
    log: list[str] = []
    hits: list[dict] = []          # the same events as `log`, but by side, so a replay cannot
    turn = 0                        # mis-attribute a hit when both cards share a name

    while a.hp > 0 and b.hp > 0 and turn < MAX_TURNS:
        turn += 1
        first, second = (a, b) if a.stat("speed") > b.stat("speed") or (
            a.stat("speed") == b.stat("speed") and rng.random() < 0.5) else (b, a)
        for atk, dfd in ((first, second), (second, first)):
            if atk.hp <= 0 or dfd.hp <= 0:
                continue
            mtype, mult = best_move_type(chart, atk, dfd)
            dmg = damage(atk, dfd, mult, rng)
            dfd.hp = max(0, dfd.hp - dmg)
            note = " — super effective!" if mult >= 2 * (STAB if mtype in atk.types else 1) else \
                   " — not very effective" if 0 < mult < (STAB if mtype in atk.types else 1) else \
                   " — no effect" if mult == 0 else ""
            log.append(f"T{turn}: {atk.name} used a {mtype.title()} move for {dmg}{note}  ({dfd.name} {dfd.hp} HP)")
            hits.append({"turn": turn, "attacker": 0 if atk is a else 1, "defender": 0 if dfd is a else 1,
                         "type": mtype, "damage": dmg, "note": note.strip(" —"), "hp": [a.hp, b.hp]})
    if a.hp > 0 and b.hp <= 0:
        winner = a
    elif b.hp > 0 and a.hp <= 0:
        winner = b
    else:                                                  # timeout: higher remaining HP share wins
        winner = a if a.hp / a.stat("hp") >= b.hp / b.stat("hp") else b
    return {"winner": winner.card, "loser": (b if winner is a else a).card,
            "winner_side": 0 if winner is a else 1, "turns": turn,
            # keyed by name for readers that print it; by side because two cards can share a name
            "remaining": {a.name: a.hp, b.name: b.hp}, "remaining_by_side": [a.hp, b.hp],
            "log": log, "hits": hits}


def power_score(card: dict) -> int:
    """A quick GO-style comparable: attack-weighted geometric mean of the six stats."""
    st = card["stats"]
    atk = max(st["attack"], st["special-attack"])
    dfd = (st["defense"] + st["special-defense"]) / 2
    return int(math.sqrt(atk * dfd * st["hp"] * (1 + st["speed"] / 400)) )


def default_trainer() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return "trainer"
