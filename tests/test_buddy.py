"""The buddy pin: a display choice over an owned species, never a change to the companion."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poketoken import companion as C, usage as U  # noqa: E402
from test_companion import FakeAPI, make  # noqa: E402

M = 1_000_000


def dex_entry(base, final, chain, name, shiny=False, released=False, wild=False):
    return C.DexEntry(id=f"e{final}", base_id=base, final_id=final, chain_order=chain, rarity="common",
                      caught_at="2026-09-01T00:00:00", is_shiny=shiny, nature="brave",
                      names={sid: {"en": name} for sid in chain},
                      released_at="2026-09-02T00:00:00" if released else None,
                      source="released" if released else "wild" if wild else "graduated")


class OwnershipTests(unittest.TestCase):
    def test_owns_only_what_was_held(self):
        comp, _, _, _ = make()
        st = comp.state
        st.dex = [dex_entry(16, 18, [16, 17, 18], "Pidgeot")]
        self.assertTrue(st.owns_species(16))
        self.assertTrue(st.owns_species(18))
        self.assertFalse(st.owns_species(25))
        st.active = C.MonState(1, [1, 2], [1, 2, 3], stage_index=1, rarity="rare", total_forms=3)
        self.assertTrue(st.owns_species(1))
        self.assertTrue(st.owns_species(2))
        self.assertFalse(st.owns_species(3), "a form not yet reached must never be pinnable")

    def test_pin_requires_ownership(self):
        comp, _, _, _ = make()
        comp.state.dex = [dex_entry(16, 18, [16, 17, 18], "Pidgeot")]
        ok, msg = comp.set_buddy(25)
        self.assertFalse(ok)
        self.assertIn("not in your Pokédex", msg)
        self.assertIsNone(comp.state.representative_species_id)
        ok, msg = comp.set_buddy(18)
        self.assertTrue(ok)
        self.assertEqual(comp.state.representative_species_id, 18)
        self.assertEqual([e["kind"] for e in comp.drain_events()], ["buddy"])

    def test_buddy_id_falls_back_and_reconciles(self):
        comp, _, _, _ = make()
        st = comp.state
        st.active = C.MonState(1, [1], [1, 2], rarity="rare", total_forms=2)
        self.assertEqual(comp.buddy_id, 1)
        self.assertFalse(comp.buddy_is_pinned)
        st.dex = [dex_entry(16, 18, [16, 17, 18], "Pidgeot")]
        comp.set_buddy(18)
        self.assertEqual(comp.buddy_id, 18)
        self.assertTrue(comp.buddy_is_pinned)
        st.dex = []                                        # the record went away (edited save)
        self.assertEqual(comp.buddy_id, 1, "an unowned pin falls back to the companion")
        self.assertTrue(st.reconcile_representative())
        self.assertIsNone(st.representative_species_id)
        self.assertFalse(st.reconcile_representative())

    def test_clear_and_roundtrip(self):
        comp, api, _, tmp = make()
        comp.state.dex = [dex_entry(16, 18, [16, 17, 18], "Pidgeot")]
        comp.set_buddy(18)
        again = C.Companion(api, comp.path, log=lambda m: None)
        self.assertEqual(again.state.representative_species_id, 18)
        ok, msg = again.set_buddy(None)
        self.assertTrue(ok)
        self.assertIn("cleared", msg)
        self.assertIsNone(C.Companion(api, comp.path, log=lambda m: None).state.representative_species_id)
        bad = C.CompanionState.from_dict({"representativeSpeciesID": "18"})
        self.assertIsNone(bad.representative_species_id)


class BehaviourTests(unittest.TestCase):
    def test_pin_does_not_touch_the_companion(self):
        comp, _, _, _ = make()
        p = U.PROVIDER_ID
        comp.update({p: 10 * M}, "2026-09-08")
        comp.update({p: 16 * M}, "2026-09-08")             # hatches
        before = comp.state.active.to_dict()
        comp.state.dex = [dex_entry(16, 18, [16, 17, 18], "Pidgeot")]
        comp.set_buddy(18)
        comp.update({p: 20 * M}, "2026-09-08")             # keeps growing underneath
        self.assertEqual(comp.state.representative_species_id, 18)
        self.assertEqual(comp.state.active.base_id, before["baseID"])
        self.assertGreater(comp.state.active.used_at_stage, before["usedAtStage"])
        self.assertEqual(comp.buddy_name(), "Pidgeot")
        self.assertNotEqual(comp.display_name(), "Pidgeot")   # display_name stays the companion's

    def test_releasing_the_pinned_companion_clears_the_pin(self):
        comp, _, _, _ = make()
        p = U.PROVIDER_ID
        comp.update({p: 10 * M}, "2026-09-08")
        comp.update({p: 16 * M}, "2026-09-08")
        sid = comp.state.active.current_id
        comp.set_buddy(sid)
        self.assertEqual(comp.state.representative_species_id, sid)
        comp.state.used_since_install += 5_000 * M
        comp.buy("egg:plain")                              # releases the companion into the dex
        self.assertTrue(comp.state.owns_species(sid), "a released Pokémon is still owned")
        self.assertEqual(comp.state.representative_species_id, sid)
        comp.state.dex = []                                # …but if the record goes, so does the pin
        comp.update({p: 21 * M}, "2026-09-08")
        self.assertIsNone(comp.state.representative_species_id)

    def test_owned_species_listing(self):
        comp, _, _, _ = make()
        comp.state.dex = [dex_entry(16, 18, [16, 17, 18], "Pidgeot")]
        comp.state.active = C.MonState(1, [1], [1, 2], rarity="rare", total_forms=2)
        self.assertEqual([sid for sid, _ in comp.owned_species()], [1, 16, 17, 18])
        self.assertEqual(comp.buddy_name(17), "Pidgeot")   # the record's names cover its whole chain


if __name__ == "__main__":
    unittest.main()
