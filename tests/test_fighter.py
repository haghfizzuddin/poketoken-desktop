"""Choosing which Pokémon fights: a Pokédex record fields at level 100 with its saved IVs."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poketoken import battle as B, battle_ui as BU, companion as C, settings  # noqa: E402
from test_buddy import dex_entry  # noqa: E402
from test_companion import make  # noqa: E402
from test_stats import PIKACHU  # noqa: E402


def pidgeot_meta():
    return dict(PIKACHU, id=18, name="pidgeot", types=["normal", "flying"],
                stats={"hp": 83, "attack": 80, "defense": 75, "special-attack": 70,
                       "special-defense": 70, "speed": 101})


class FighterChoiceTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.comp, _, _, _ = make()
        self.comp.state.active = C.MonState(1, [1], [1, 2], stage_index=0, used_at_stage=0,
                                            rarity="rare", total_forms=2, nature="jolly",
                                            ivs={k: 20 for k in C.STAT_KEYS})
        self.comp.state.dex = [dex_entry(16, 18, [16, 17, 18], "Pidgeot")]
        self.comp.state.dex[0].ivs = {k: 31 for k in C.STAT_KEYS}

    def test_defaults_to_the_companion(self):
        self.assertEqual(BU.fighter_sid(self.comp, self.dir), 1)
        BU.set_fighter(self.dir, 18)
        self.assertEqual(BU.fighter_sid(self.comp, self.dir), 18)
        BU.set_fighter(self.dir, None)
        self.assertEqual(BU.fighter_sid(self.comp, self.dir), 1)

    def test_an_unowned_or_junk_choice_falls_back(self):
        BU.set_fighter(self.dir, 999)
        self.assertEqual(BU.fighter_sid(self.comp, self.dir), 1, "unowned choice must not stick")
        settings.set(self.dir, "fighter", "18")
        self.assertEqual(BU.fighter_sid(self.comp, self.dir), 1)
        settings.set(self.dir, "fighter", True)
        self.assertEqual(BU.fighter_sid(self.comp, self.dir), 1)

    def test_record_card_is_level_100_with_saved_ivs(self):
        card = B.record_card(self.comp, 18, pidgeot_meta(), "Fizz")
        self.assertEqual(card["level"], 100)
        self.assertEqual(card["species"], 18)
        self.assertEqual(card["name"], "Pidgeot")
        self.assertEqual(card["types"], ["normal", "flying"])
        self.assertEqual(card["ivTotal"], 186)
        self.assertEqual(card["stats"]["hp"], C.stat_value("hp", 83, 31, 100, "brave"))
        self.assertEqual(card["nature"], "brave")
        B.validate_card(card)
        self.assertIsNone(B.record_card(self.comp, 18, dict(pidgeot_meta(), id=99), "Fizz"))
        self.assertIsNone(B.record_card(self.comp, 25, PIKACHU, "Fizz"), "not in the dex")

    def test_record_without_ivs_still_fields(self):
        self.comp.state.dex[0].ivs = None
        card = B.record_card(self.comp, 18, pidgeot_meta(), "Fizz")
        self.assertIsNone(card["ivTotal"])
        self.assertEqual(card["stats"]["hp"], C.stat_value("hp", 83, 0, 100, "brave"))
        B.validate_card(card)

    def test_own_card_follows_the_choice(self):
        settings.set(self.dir, "trainer", "Fizz")
        BU.set_fighter(self.dir, 18)
        card = BU.own_card(self.comp, pidgeot_meta(), self.dir)
        self.assertEqual((card["name"], card["level"], card["trainer"]), ("Pidgeot", 100, "Fizz"))
        BU.set_fighter(self.dir, None)
        meta1 = dict(PIKACHU, id=1, types=["grass", "poison"])
        own = BU.own_card(self.comp, meta1, self.dir)
        self.assertEqual(own["species"], 1)
        self.assertLess(own["level"], 100, "the companion fields at the level it has grown to")
        self.assertIsNone(BU.own_card(self.comp, None, self.dir))

    def test_a_fielded_record_beats_a_young_companion(self):
        """Not a rule, just the consequence worth knowing: level 100 outclasses a fresh hatch."""
        settings.set(self.dir, "trainer", "Fizz")
        rec = B.record_card(self.comp, 18, pidgeot_meta(), "Fizz")
        young = B.make_card(self.comp, dict(PIKACHU, id=1, types=["grass"]), "Fizz")
        self.assertGreater(B.power_score(rec), B.power_score(young))


if __name__ == "__main__":
    unittest.main()
