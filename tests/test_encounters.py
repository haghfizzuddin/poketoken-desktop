"""Wild encounters: triggers, seeding, spawning, throwing, catching, fleeing, expiry."""
from __future__ import annotations

import random
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poketoken import companion as C  # noqa: E402
from poketoken.pokeapi import PokeAPIError  # noqa: E402
from test_companion import FakeAPI  # noqa: E402
from test_history import comp_with_history  # noqa: E402

M = 1_000_000
TODAY = date(2026, 9, 8)
T = TODAY.isoformat()


class SpeciesFakeAPI(FakeAPI):
    def wild_index(self):
        """The wild draws from every species, so the fake offers evolved forms too."""
        if self.offline:
            raise PokeAPIError("offline")
        return self.index + [(2, 45), (17, 120)]

    def species(self, sid):
        caps = dict(self.index + [(2, 45), (17, 120)])
        return {"id": sid, "name": {1: "bulbasaur", 2: "ivysaur", 16: "pidgey", 17: "pidgeotto",
                                    133: "eevee"}.get(sid, f"sp{sid}"),
                "capture_rate": caps.get(sid, 255), "is_legendary": False, "is_mythical": False,
                "evolves_from": None, "chain_url": "",
                "names": {"en": {1: "Bulbasaur", 2: "Ivysaur", 16: "Pidgey", 17: "Pidgeotto",
                                 133: "Eevee"}.get(sid, f"#{sid}")}}


def make(rows=None, seed=1):
    c = comp_with_history(rows or {})
    c.api = SpeciesFakeAPI()
    c.rng = random.Random(seed)
    c.today = T
    c.state.install_baseline_set = True
    return c


def streak_rows(days, tokens=5 * M, best=None):
    """`days` streak days ending today; today's best 5h block is `best`, earlier days half of it."""
    best = best or tokens
    return {(TODAY - timedelta(days=i)).isoformat(): {"tokens": tokens, "bestBlock": best if i == 0 else best // 2}
            for i in range(days)}


class TriggerTests(unittest.TestCase):
    def test_catch_chance_bounds(self):
        self.assertAlmostEqual(C.catch_chance(255, "pokeBall"), 0.95)
        self.assertAlmostEqual(C.catch_chance(45, "pokeBall"), 45 / 255)
        self.assertAlmostEqual(C.catch_chance(45, "ultraBall"), 90 / 255)
        self.assertAlmostEqual(C.catch_chance(3, "ultraBall"), 0.05)

    def test_triggers(self):
        c = make(streak_rows(3, best=8 * M))
        self.assertEqual([r for _, r in c.encounter_triggers(T)], ["streak day earned", "personal-best 5-hour block"])
        c.state.history[T]["bestBlock"] = 8 * M                     # equal to the best before: not a record
        c.state.history[(TODAY - timedelta(days=1)).isoformat()]["bestBlock"] = 9 * M
        self.assertEqual([r for _, r in c.encounter_triggers(T)], ["streak day earned"])
        quiet = make({T: {"tokens": 500_000, "bestBlock": 500_000}})
        self.assertEqual(quiet.encounter_triggers(T), [])

    def test_seed_then_spawn_once_per_trigger(self):
        c = make(streak_rows(2, best=8 * M))
        self.assertEqual(c.evaluate_encounters(T), [])              # seeding
        self.assertTrue(c.state.encounter_feature_seeded)
        self.assertIsNone(c.current_encounter(T))
        tomorrow = (TODAY + timedelta(days=1)).isoformat()
        c.state.history[tomorrow] = {"tokens": 5 * M, "bestBlock": 2 * M}
        c.today = tomorrow
        spawned = c.evaluate_encounters(tomorrow)
        self.assertEqual(len(spawned), 1)
        enc = c.current_encounter(tomorrow)
        self.assertEqual(enc["trigger"], "streak day earned")
        self.assertEqual(enc["status"], "wild")
        self.assertIn(enc["species"], (1, 2, 16, 17, 133))
        self.assertEqual(c.evaluate_encounters(tomorrow), [])       # same trigger never fires twice
        self.assertEqual([e["kind"] for e in c.drain_events()], ["encounter"])

    def test_one_wild_at_a_time_and_expiry(self):
        c = make(streak_rows(1))
        c.evaluate_encounters(T)
        d1 = (TODAY + timedelta(days=1)).isoformat()
        c.state.history[d1] = {"tokens": 5 * M, "bestBlock": 50 * M}   # both triggers satisfied
        c.today = d1
        self.assertEqual(len(c.evaluate_encounters(d1)), 1)         # second trigger waits for the first to resolve
        self.assertEqual(len(c.evaluate_encounters(d1)), 0)
        d3 = (TODAY + timedelta(days=3)).isoformat()
        c.state.history[d3] = {"tokens": 5 * M, "bestBlock": 1 * M}
        c.today = d3
        c.evaluate_encounters(d3)
        statuses = [e["status"] for e in c.state.encounters]
        self.assertIn("expired", statuses)                          # the first one timed out (TTL 2 days)
        self.assertEqual(statuses.count("wild"), 1)                 # the held-over best-block trigger spawned


class ThrowTests(unittest.TestCase):
    def _with_wild(self):
        c = make(streak_rows(1))
        c.evaluate_encounters(T)
        d1 = (TODAY + timedelta(days=1)).isoformat()
        c.state.history[d1] = {"tokens": 5 * M, "bestBlock": 1 * M}
        c.today = d1
        c.evaluate_encounters(d1)
        c.drain_events()
        return c

    def test_no_ball_no_encounter(self):
        c = make()
        self.assertEqual(c.throw_ball()[0], False)
        c = self._with_wild()
        ok, msg = c.throw_ball()
        self.assertFalse(ok)
        self.assertIn("Shop", msg)
        self.assertEqual(c.throw_ball("mint")[0], False)

    def test_catch_goes_to_dex_with_ivs(self):
        c = self._with_wild()
        c.state.inventory["ultraBall"] = 1
        c.rng.random = lambda: 0.0                                  # always catch
        ok, msg = c.throw_ball()
        self.assertTrue(ok, msg)
        self.assertEqual(c.item_count("ultraBall"), 0)
        self.assertEqual(c.best_ball(), None)
        e = c.state.dex[-1]
        self.assertTrue(e.is_wild)
        self.assertEqual(e.source, "wild")
        self.assertEqual(len(e.ivs), 6)
        self.assertEqual(e.chain_order, [e.final_id])
        self.assertIn(f"{e.base_id}:{e.final_id}", c.state.collected_finals)
        self.assertIsNone(c.current_encounter())
        self.assertEqual([ev["kind"] for ev in c.drain_events()], ["caught"])
        self.assertEqual(C.DexEntry.from_dict(e.to_dict()).source, "wild")

    def test_miss_then_flee(self):
        c = self._with_wild()
        c.state.inventory["pokeBall"] = 2
        rolls = iter([0.99, 0.99, 0.99, 0.0])                       # miss, stay; miss, flee
        c.rng.random = lambda: next(rolls)
        ok, msg = c.throw_ball()
        self.assertFalse(ok)
        self.assertIn("broke free", msg)
        self.assertIsNotNone(c.current_encounter())
        ok, msg = c.throw_ball()
        self.assertFalse(ok)
        self.assertIn("fled", msg)
        self.assertIsNone(c.current_encounter())
        self.assertEqual(c.item_count("pokeBall"), 0)
        self.assertEqual([ev["kind"] for ev in c.drain_events()], ["fled"])

    def test_use_item_dispatch_and_shop(self):
        c = self._with_wild()
        c.state.used_since_install += 1_000 * M
        self.assertTrue(c.buy("greatBall")[0])
        self.assertEqual(c.best_ball(), "greatBall")
        self.assertEqual(c.use_item("bogus")[0], False)
        c.rng.random = lambda: 0.0
        self.assertTrue(c.use_item("greatBall")[0])
        self.assertIn("pokeBall", [r["key"] for r in c.shop_entries()])

    def test_legacy_records_default_source(self):
        e = C.DexEntry.from_dict({"baseID": 1, "finalID": 3, "chainOrder": [1, 2, 3], "rarity": "rare", "caughtAt": None})
        self.assertEqual(e.source, "graduated")
        r = C.DexEntry.from_dict({"baseID": 1, "finalID": 1, "chainOrder": [1], "rarity": "rare", "caughtAt": None,
                                  "releasedAt": "2026-09-01T00:00:00"})
        self.assertEqual(r.source, "released")


if __name__ == "__main__":
    unittest.main()
