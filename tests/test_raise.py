"""Raising a caught or released Pokémon instead of hatching an egg."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poketoken import companion as C, usage as U  # noqa: E402
from test_buddy import dex_entry  # noqa: E402
from test_companion import make  # noqa: E402

M = 1_000_000


def caught(sid=16, name="Pidgey", shiny=True):
    e = dex_entry(sid, sid, [sid], name, shiny=shiny, wild=True)
    e.level = 22
    e.ivs = {k: 29 for k in C.STAT_KEYS}
    e.nature = "timid"
    return e


class EligibilityTests(unittest.TestCase):
    def test_only_caught_and_released_can_be_raised(self):
        comp, _, _, _ = make()
        comp.state.used_since_install = C.FRESH_EGG_PRICE
        comp.state.dex = [caught(), dex_entry(1, 3, [1, 2, 3], "Venusaur")]      # a graduation
        self.assertTrue(comp.can_raise(16)[0])
        ok, why = comp.can_raise(3)
        self.assertFalse(ok)
        self.assertIn("graduated", why)
        self.assertFalse(comp.can_raise(999)[0])
        released = dex_entry(25, 25, [25], "Pikachu", released=True)
        comp.state.dex.append(released)
        self.assertTrue(comp.can_raise(25)[0], "a Pokémon you gave up on can be picked up again")

    def test_it_always_costs_an_egg_because_a_hatch_is_skipped(self):
        comp, _, _, _ = make()
        comp.state.dex = [caught()]
        ok, why = comp.can_raise(16)
        self.assertFalse(ok, "an egg's surprise is the free default; choosing is paid for")
        self.assertIn("skipping a hatch", why)
        comp.state.used_since_install = C.FRESH_EGG_PRICE
        self.assertTrue(comp.can_raise(16)[0])
        comp.state.active = C.MonState(1, [1], [1, 2], rarity="rare", total_forms=2)
        ok, why = comp.can_raise(16)
        self.assertTrue(ok, "the same price, and the companion goes to the Pokédex too")


class RaiseTests(unittest.TestCase):
    def test_raising_while_holding_an_egg_keeps_the_individual(self):
        comp, api, _, _ = make()
        comp.state.used_since_install = C.FRESH_EGG_PRICE
        comp.state.dex = [caught()]
        spent = comp.state.spent_tokens
        ok, msg = comp.raise_caught(16)
        self.assertTrue(ok, msg)
        a = comp.state.active
        self.assertEqual(a.base_id, 16)
        self.assertEqual(a.stage_index, 0)
        self.assertEqual(a.used_at_stage, 0, "a wild level was never training: it starts from scratch")
        self.assertEqual(comp.level(), C.LEVEL_MIN)
        self.assertTrue(a.is_shiny, "the same individual, so it keeps its shininess")
        self.assertEqual(a.nature, "timid")
        self.assertEqual(a.ivs, {k: 29 for k in C.STAT_KEYS})
        self.assertEqual(a.total_forms, 3, "it follows its real line from where it was caught")
        self.assertEqual(comp.state.spent_tokens, spent + C.FRESH_EGG_PRICE, "a hatch was bought out")
        self.assertEqual(comp.state.dex, [], "it left the Pokédex to be raised")
        self.assertEqual([e["kind"] for e in comp.drain_events()], ["raise"])

    def test_raising_mid_raise_pays_and_releases(self):
        comp, _, _, _ = make()
        p = U.PROVIDER_ID
        comp.update({p: 10 * M}, "2026-09-08")
        comp.update({p: 16 * M}, "2026-09-08")                  # hatches a companion
        comp.drain_events()
        old = comp.display_name()
        comp.state.used_since_install += 5_000 * M
        comp.state.dex = [caught()]
        spent = comp.state.spent_tokens
        ok, msg = comp.raise_caught(16)
        self.assertTrue(ok, msg)
        self.assertEqual(comp.state.spent_tokens, spent + C.FRESH_EGG_PRICE)
        self.assertIn(old, msg)
        self.assertEqual(comp.state.active.base_id, 16)
        released = [e for e in comp.state.dex if e.is_released]
        self.assertEqual(len(released), 1, "the old companion went to the Pokédex")
        self.assertEqual(released[0].source, "released")
        self.assertIsNotNone(released[0].level)

    def test_a_raised_catch_evolves_like_any_companion(self):
        comp, _, _, _ = make()
        comp.state.used_since_install = C.FRESH_EGG_PRICE
        comp.state.dex = [caught()]
        comp.raise_caught(16)
        comp.drain_events()
        p = U.PROVIDER_ID
        comp.update({p: 10 * M}, "2026-09-08")                  # baseline
        thr = C.phase_threshold(comp.state.active.rarity, comp.state.active.total_forms, 0)
        comp.update({p: 10 * M + thr + M}, "2026-09-08")
        self.assertEqual(comp.state.active.stage_index, 1, "it grows and evolves like a hatchling")
        self.assertEqual([e["kind"] for e in comp.drain_events()], ["evolve"])

    def test_a_bought_egg_guarantee_survives(self):
        comp, _, _, _ = make()
        comp.state.used_since_install = C.FRESH_EGG_PRICE
        comp.state.dex = [caught()]
        comp.state.egg_tier = "rare"                            # paid for on the shop
        comp.raise_caught(16)
        self.assertEqual(comp.state.egg_tier, "rare", "the guarantee waits for the next egg")

    def test_a_pin_on_the_raised_one_gives_way_to_following_it(self):
        comp, _, _, _ = make()
        comp.state.used_since_install = C.FRESH_EGG_PRICE
        comp.state.dex = [caught()]
        comp.set_buddy(16)
        comp.raise_caught(16)
        self.assertTrue(comp.state.owns_species(16))
        self.assertEqual(comp.buddy_id, 16, "it is the companion now, so the card shows it")
        self.assertIsNone(comp.state.representative_species_id,
                          "and the pin is cleared, so the card will follow its evolutions")


if __name__ == "__main__":
    unittest.main()
