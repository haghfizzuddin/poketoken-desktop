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
CARD_VERSION = 2            # 2 carries base stats and IVs, so a card can be re-levelled exactly
FLAT_LEVEL = 50             # the level both sides are scaled to for a fair fight (VGC flat rules)
# a card arrives from another person, so treat it as hostile input: a real one is ~300 characters
# and inflates to well under a kilobyte. These caps stop a crafted token from inflating to
# gigabytes in memory before the contents are ever checked.
MAX_CARD_CHARS = 4096
MAX_CARD_BYTES = 64 * 1024
MOVE_POWER = 60
MAX_TURNS = 100
STAB = 1.5


# ------------------------------------------------------------------ cards
def _card(trainer: str, species: int, name: str, view: dict, nature, shiny: bool, rarity: str,
          base: dict | None = None, ivs: dict | None = None) -> dict:
    return {
        "v": CARD_VERSION,
        "trainer": trainer[:24],
        "species": species,
        "name": name,
        "level": view["level"],
        "types": view["types"][:2],
        "stats": {r["key"]: r["value"] for r in view["rows"]},
        "nature": nature,
        "shiny": bool(shiny),
        "rarity": rarity,
        "ivTotal": view["iv_total"],
        # base stats and IVs travel with the card so either side can recompute it at any level
        "base": dict(base) if base else None,
        "ivs": dict(ivs) if ivs else None,
        "date": date.today().isoformat(),
    }


def make_card(comp: C.Companion, meta: dict, trainer: str) -> dict | None:
    """Snapshot of the active Pokémon as it stands right now."""
    view = comp.stats_view(meta)
    a = comp.state.active
    if view is None or a is None:
        return None
    return _card(trainer, a.current_id, comp.display_name(), view, a.nature, a.shiny_visible, a.rarity,
                 base=meta.get("stats"), ivs=a.ivs)


def record_card(comp: C.Companion, sid: int, meta: dict, trainer: str) -> dict | None:
    """A card for a Pokémon in the Pokédex. Graduated, released and caught Pokémon are done
    growing, so they field at level 100 with the IVs they were recorded with (the same numbers
    the species page shows). Records made before IVs existed field with unknown IVs, which the
    stat formula reads as zero."""
    if not meta or int(meta.get("id", -1)) != int(sid):
        return None
    entry = next((e for e in sorted(comp.state.dex, key=lambda e: e.caught_at or "", reverse=True)
                  if e.final_id == sid), None) or \
            next((e for e in comp.state.dex if sid in e.chain_order), None)
    if entry is None:
        return None
    view = C.Companion.stats_view_static(meta, entry.ivs, entry.nature, level=entry.battle_level())
    return _card(trainer, sid, comp.buddy_name(sid), view, entry.nature, entry.is_shiny, entry.rarity,
                 base=meta.get("stats"), ivs=entry.ivs)


def encode_card(card: dict) -> str:
    raw = json.dumps(card, separators=(",", ":"), sort_keys=True).encode()
    return CARD_PREFIX + base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode().rstrip("=")


def decode_card(text: str) -> dict:
    text = text.strip()
    if not text.startswith(CARD_PREFIX):
        raise ValueError("not a PokeToken card (expected PT1.…)")
    if len(text) > MAX_CARD_CHARS:
        raise ValueError("that is too long to be a card")
    body = text[len(CARD_PREFIX):]
    body += "=" * (-len(body) % 4)
    try:
        raw = base64.urlsafe_b64decode(body)
        # bounded inflate: whatever the compression ratio claims, stop at MAX_CARD_BYTES
        unzip = zlib.decompressobj()
        data = unzip.decompress(raw, MAX_CARD_BYTES)
        if unzip.unconsumed_tail:
            raise ValueError("card is far larger than any real card")
        card = json.loads(data)
    except (ValueError, zlib.error) as e:
        raise ValueError(f"corrupt card: {e}") from e
    return validate_card(card)


def validate_card(card) -> dict:
    if not isinstance(card, dict) or card.get("v") not in (1, CARD_VERSION):
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
    for key in ("base", "ivs"):                   # optional, but must be sane when present
        v = card.get(key)
        if v is None:
            continue
        if not isinstance(v, dict) or any(k not in v for k in C.STAT_KEYS):
            card[key] = None
            continue
        lo, hi = (1, 255) if key == "base" else (0, C.IV_MAX)
        if any(not isinstance(v[k], int) or isinstance(v[k], bool) or not lo <= v[k] <= hi for k in C.STAT_KEYS):
            card[key] = None
    card["name"] = str(card.get("name", "?"))[:32]
    card["trainer"] = str(card.get("trainer", "?"))[:24]
    return card


def at_level(card: dict, level: int) -> dict:
    """The same Pokémon as it would be at `level`. A version 2 card carries base stats and IVs,
    so this is the games' formula again, exactly. An older card only knows its final numbers, so
    they are scaled and the result is marked approximate."""
    level = max(1, min(C.LEVEL_MAX, int(level)))
    if int(card.get("level", level)) == level:
        return card
    out = dict(card, level=level)
    base, ivs = card.get("base"), card.get("ivs")
    if isinstance(base, dict) and all(k in base for k in C.STAT_KEYS):
        out["stats"] = {k: C.stat_value(k, int(base[k]), int((ivs or {}).get(k, 0)), level, card.get("nature"))
                        for k in C.STAT_KEYS}
    else:
        f = level / max(1, int(card.get("level", 1)))
        out["stats"] = {k: max(1, round(int(v) * f)) for k, v in card["stats"].items()}
        out["approx"] = True
    return out


def fielded(card_a: dict, card_b: dict, flat: bool = True) -> tuple[dict, dict]:
    """The two cards as they take the field: scaled to a common level unless raw levels were
    asked for. Flat is the default because level otherwise decides almost everything — it swings
    power about 5.6x across the range, where species is 1.9x and IVs 1.2x."""
    if not flat:
        return card_a, card_b
    return at_level(card_a, FLAT_LEVEL), at_level(card_b, FLAT_LEVEL)


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
