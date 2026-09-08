"""Choosing which Pokémon fights: a Pokédex record fields at level 100 with its saved IVs."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poketoken import battle as B, battle_ui as BU, companion as C, settings  # noqa: E402
from test_battle import CHART  # noqa: E402
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


class FlatLevelTests(unittest.TestCase):
    """A fight at one level: the level gap goes, everything the player earned stays."""

    def _cards(self):
        veteran = B.record_card(self.comp, 18, pidgeot_meta(), "Vet")
        young = B.make_card(self.comp, dict(PIKACHU, id=1, types=["grass", "poison"]), "New")
        return veteran, young

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.comp, _, _, _ = make()
        self.comp.state.active = C.MonState(1, [1], [1, 2], stage_index=0, used_at_stage=30_000_000,
                                            rarity="rare", total_forms=2, nature="jolly",
                                            ivs={k: 20 for k in C.STAT_KEYS})
        self.comp.state.dex = [dex_entry(16, 18, [16, 17, 18], "Pidgeot")]
        self.comp.state.dex[0].ivs = {k: 31 for k in C.STAT_KEYS}
        self.comp.state.dex[0].level = 100

    def test_at_level_is_exact_for_v2_cards(self):
        vet, _ = self._cards()
        self.assertEqual(vet["v"], B.CARD_VERSION)
        self.assertIsNotNone(vet["base"])
        at50 = B.at_level(vet, 50)
        self.assertEqual(at50["level"], 50)
        self.assertNotIn("approx", at50)
        self.assertEqual(at50["stats"]["hp"], C.stat_value("hp", vet["base"]["hp"], 31, 50, vet["nature"]))
        self.assertIs(B.at_level(at50, 50), at50)                  # already there, unchanged
        self.assertEqual(B.at_level(vet, 999)["level"], C.LEVEL_MAX)
        B.validate_card(at50)

    def test_at_level_approximates_a_v1_card(self):
        vet, _ = self._cards()
        legacy = {k: v for k, v in vet.items() if k not in ("base", "ivs")}
        legacy["v"] = 1
        out = B.at_level(B.validate_card(legacy), 50)
        self.assertTrue(out["approx"])
        self.assertLess(out["stats"]["hp"], vet["stats"]["hp"])
        self.assertGreater(out["stats"]["hp"], 1)

    def test_flat_closes_the_level_gap_but_not_the_earned_one(self):
        vet, young = self._cards()
        raw_gap = B.power_score(vet) / B.power_score(young)
        fa, fb = B.fielded(vet, young, flat=True)
        flat_gap = B.power_score(fa) / B.power_score(fb)
        self.assertEqual((fa["level"], fb["level"]), (B.FLAT_LEVEL, B.FLAT_LEVEL))
        self.assertGreater(raw_gap, 5, "raw levels are lopsided")
        self.assertLess(flat_gap, raw_gap / 2, "flat levels close most of the gap")
        self.assertGreater(flat_gap, 1.0, "the better species and IVs still win")

    def test_raw_mode_leaves_the_cards_alone(self):
        vet, young = self._cards()
        fa, fb = B.fielded(vet, young, flat=False)
        self.assertIs(fa, vet)
        self.assertIs(fb, young)

    def test_the_fight_is_the_same_on_both_machines(self):
        vet, young = self._cards()
        fa, fb = B.fielded(vet, young, flat=True)
        r1 = B.simulate(fa, fb, CHART)
        fa2, fb2 = B.fielded(vet, young, flat=True)
        r2 = B.simulate(fa2, fb2, CHART)
        self.assertEqual(r1["turns"], r2["turns"])
        self.assertEqual(r1["winner_side"], r2["winner_side"])
        self.assertEqual(r1["remaining_by_side"], r2["remaining_by_side"])
