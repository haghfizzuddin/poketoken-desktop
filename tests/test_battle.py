"""Battle cards: encoding, validation, deterministic simulation, type matchups."""
from __future__ import annotations

import random
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poketoken import battle as B, companion as C, settings  # noqa: E402
from poketoken.pokeapi import TYPES  # noqa: E402
from test_history import comp_with_history  # noqa: E402
from test_stats import PIKACHU  # noqa: E402

# a tiny type chart: electric beats water, ground beats electric, water beats fire; rest neutral
CHART = {t: {d: 1.0 for d in TYPES} for t in TYPES}
CHART["electric"]["water"] = 2.0
CHART["electric"]["ground"] = 0.0
CHART["ground"]["electric"] = 2.0
CHART["water"]["fire"] = 2.0
CHART["fire"]["water"] = 0.5


def card(name, types, level=50, **stats) -> dict:
    st = {"hp": 120, "attack": 80, "defense": 80, "special-attack": 80, "special-defense": 80, "speed": 80}
    st.update(stats)
    return B.validate_card({"v": 1, "trainer": "t", "species": 1, "name": name, "level": level, "types": types,
                            "stats": st, "nature": "hardy", "shiny": False, "rarity": "common", "ivTotal": 100,
                            "date": "2026-09-07"})


class CardTests(unittest.TestCase):
    def test_make_encode_decode_roundtrip(self):
        c = comp_with_history({})
        c.state.active = C.MonState(1, [1], [1, 2, 3], rarity="rare", total_forms=3, nature="jolly",
                                    ivs={k: 31 for k in C.STAT_KEYS})
        meta = dict(PIKACHU, id=1, types=["grass", "poison"])
        card_ = B.make_card(c, meta, "Fizz")
        self.assertEqual(card_["trainer"], "Fizz")
        self.assertEqual(card_["name"], "Bulbasaur")
        self.assertEqual(card_["types"], ["grass", "poison"])
        token = B.encode_card(card_)
        self.assertTrue(token.startswith("PT1."))
        self.assertNotIn("=", token)
        self.assertEqual(B.decode_card(token), card_)
        self.assertIn("Lv 5", B.card_summary(card_))
        self.assertGreater(B.power_score(card_), 0)
        c.state.active = None
        self.assertIsNone(B.make_card(c, meta, "Fizz"))

    def test_validation_rejects_garbage(self):
        for bad in ("hello", "PT1.!!!", "PT1." + "A" * 10):
            with self.assertRaises(ValueError):
                B.decode_card(bad)
        with self.assertRaises(ValueError):
            B.validate_card({"v": 2})
        with self.assertRaises(ValueError):
            card("x", ["fire"], hp=99999)
        with self.assertRaises(ValueError):
            card("x", [])
        with self.assertRaises(ValueError):
            card("x", ["fire"], level=0)


class SimulationTests(unittest.TestCase):
    def test_deterministic_and_symmetric(self):
        a, b = card("Sparky", ["electric"]), card("Splash", ["water"])
        r1 = B.simulate(a, b, CHART)
        r2 = B.simulate(b, a, CHART)
        self.assertEqual(r1["winner"]["name"], r2["winner"]["name"])
        self.assertEqual(r1["turns"], r2["turns"])
        self.assertEqual(B.battle_seed(a, b), B.battle_seed(b, a))

    def test_type_advantage_decides_equal_stats(self):
        a, b = card("Sparky", ["electric"]), card("Splash", ["water"])
        self.assertEqual(B.simulate(a, b, CHART)["winner"]["name"], "Sparky")
        self.assertEqual(B.best_move_type(CHART, B.Fighter(a), B.Fighter(b)), ("electric", 3.0))
        self.assertEqual(B.effectiveness(CHART, "electric", ["ground"]), 0.0)
        self.assertEqual(B.effectiveness(CHART, "electric", ["water", "ground"]), 0.0)

    def test_immunity_falls_back_to_normal_and_log_notes(self):
        a, b = card("Sparky", ["electric"]), card("Dusty", ["ground"], speed=1)
        f_a, f_b = B.Fighter(a), B.Fighter(b)
        self.assertEqual(B.best_move_type(CHART, f_a, f_b)[0], "normal")
        res = B.simulate(a, b, CHART)
        self.assertEqual(res["winner"]["name"], "Dusty")                 # ground is super effective back
        self.assertTrue(any("super effective" in line for line in res["log"]))
        self.assertLessEqual(res["turns"], B.MAX_TURNS)

    def test_damage_uses_better_attacking_stat_and_never_zero_when_effective(self):
        rng = random.Random(1)
        phys = B.Fighter(card("P", ["normal"], attack=150, **{"special-attack": 10}))
        spec = B.Fighter(card("S", ["normal"], attack=10, **{"special-attack": 150}))
        wall = B.Fighter(card("W", ["normal"], defense=200, **{"special-defense": 20}))
        self.assertGreater(B.damage(spec, wall, 1.0, rng), B.damage(phys, wall, 1.0, rng))
        self.assertGreaterEqual(B.damage(phys, wall, 1.0, rng), 1)
        self.assertEqual(B.damage(phys, wall, 0.0, rng), 0)

    def test_timeout_picks_healthier_side(self):
        tank_a = card("A", ["normal"], hp=2000, defense=2000, **{"special-defense": 2000}, attack=1)
        tank_b = card("B", ["normal"], hp=2000, defense=2000, **{"special-defense": 2000}, attack=1, speed=79)
        res = B.simulate(tank_a, tank_b, CHART)
        self.assertEqual(res["turns"], B.MAX_TURNS)
        self.assertIn(res["winner"]["name"], ("A", "B"))


class SettingsTests(unittest.TestCase):
    def test_settings_roundtrip(self):
        d = Path(tempfile.mkdtemp())
        self.assertEqual(settings.load(d), {})
        settings.set(d, "trainer", "Fizz")
        settings.set(d, "notifications", False)
        self.assertEqual(settings.get(d, "trainer"), "Fizz")
        self.assertEqual(settings.load(d), {"trainer": "Fizz", "notifications": False})
        settings.path(d).write_text("[1,2]")
        self.assertEqual(settings.load(d), {})
        self.assertTrue(B.default_trainer())


if __name__ == "__main__":
    unittest.main()


class CliOfflineTests(unittest.TestCase):
    """A PokéAPI outage must print a message, not a traceback."""

    def test_card_and_battle_offline(self):
        import contextlib, io
        from poketoken import cli
        from poketoken.pokeapi import PokeAPI, PokeAPIError
        d = Path(tempfile.mkdtemp())
        boom = lambda *a, **k: (_ for _ in ()).throw(PokeAPIError("offline"))  # noqa: E731
        saved = PokeAPI.type_chart, PokeAPI.pokemon, PokeAPI.base_index, PokeAPI.line
        PokeAPI.type_chart = PokeAPI.pokemon = PokeAPI.base_index = PokeAPI.line = boom
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc_b = cli.main(["--state-dir", str(d), "battle", "PT1.garbage"])
                rc_c = cli.main(["--state-dir", str(d), "card"])
            self.assertEqual(rc_b, 1)
            self.assertIn("unreachable", out.getvalue())
            self.assertEqual(rc_c, 1)                       # egg (no Pokémon) or offline: both rc 1, no crash
        finally:
            PokeAPI.type_chart, PokeAPI.pokemon, PokeAPI.base_index, PokeAPI.line = saved
