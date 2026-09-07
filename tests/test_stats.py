"""IVs, luck, level and the stat formula."""
from __future__ import annotations

import random
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poketoken import companion as C, usage as U  # noqa: E402
from test_history import comp_with_history  # noqa: E402

M = 1_000_000
PIKACHU = {"id": 25, "name": "pikachu", "stats": {"hp": 35, "attack": 55, "defense": 40, "special-attack": 50,
                                                   "special-defense": 50, "speed": 90},
           "types": ["electric"], "abilities": [{"name": "static", "hidden": False}], "height": 4, "weight": 60}


class FormulaTests(unittest.TestCase):
    def test_known_values_level_50_iv_31(self):
        self.assertEqual(C.stat_value("hp", 35, 31, 50, "hardy"), 110)          # (70+31)*50//100 + 50 + 10
        self.assertEqual(C.stat_value("attack", 55, 31, 50, "hardy"), 75)       # (110+31)*50//100 + 5
        self.assertEqual(C.stat_value("attack", 55, 31, 50, "adamant"), 82)     # ×1.1
        self.assertEqual(C.stat_value("special-attack", 50, 31, 50, "adamant"), 63)  # (131*50//100)+5=70 ×0.9
        self.assertEqual(C.nature_mod("speed", "jolly"), 1)
        self.assertEqual(C.nature_mod("special-attack", "jolly"), -1)
        self.assertEqual(C.nature_mod("attack", "serious"), 0)

    def test_every_nature_is_mapped_and_consistent(self):
        for n in C.NATURES:
            up, down = C.NATURE_MODS[n]
            self.assertEqual(up is None, down is None, n)
            if up:
                self.assertNotEqual(up, down, n)
                self.assertIn(up, C.STAT_KEYS)
                self.assertIn(down, C.STAT_KEYS)


class LuckAndIVTests(unittest.TestCase):
    def test_bonus_rolls_raise_the_floor(self):
        c = comp_with_history({})
        c.rng = random.Random(3)
        plain = [sum(c.roll_ivs(0).values()) for _ in range(200)]
        c.rng = random.Random(3)
        lucky = [sum(c.roll_ivs(3).values()) for _ in range(200)]
        self.assertGreater(sum(lucky) / len(lucky), sum(plain) / len(plain) + 30)
        self.assertTrue(all(0 <= v <= 31 for v in c.roll_ivs(2).values()))
        self.assertEqual(set(c.roll_ivs(0)), set(C.STAT_KEYS))

    def test_luck_signals(self):
        today = date(2026, 9, 7)
        rows = {(today - timedelta(days=i)).isoformat(): {"tokens": 5 * M, "cacheRatio": 0.95} for i in range(14)}
        c = comp_with_history(rows)
        lk = c.luck_signals(today.isoformat())
        self.assertEqual(lk["streak"], 14)
        self.assertAlmostEqual(lk["cacheRatio"], 0.95)
        self.assertEqual(lk["bonusRolls"], 4)                       # streak 7 & 14, cache 0.70 & 0.90
        self.assertEqual(lk["shinyDenominator"], 48)                # 64 * 3/4
        c.state.inventory["shinyCharm"] = 1
        self.assertEqual(c.luck_signals(today.isoformat())["shinyDenominator"], 36)
        quiet = comp_with_history({})
        self.assertEqual(quiet.luck_signals(today.isoformat()), {"streak": 0, "cacheRatio": 0.0, "bonusRolls": 0,
                                                                 "shinyDenominator": 64, "charm": False})

    def test_hatch_records_ivs_and_luck_and_graduation_keeps_them(self):
        today = date(2026, 9, 7)
        rows = {(today - timedelta(days=i)).isoformat(): {"tokens": 5 * M, "cacheRatio": 0.9} for i in range(8)}
        c = comp_with_history(rows)
        p = U.PROVIDER_ID
        c.update({p: 10 * M}, today.isoformat())
        c.update({p: 16 * M}, today.isoformat())
        a = c.state.active
        self.assertIsNotNone(a.ivs)
        self.assertEqual(a.luck["bonusRolls"], 3)
        ev = [e for e in c.drain_events() if e["kind"] == "hatch"][0]
        self.assertEqual(ev["bonus_rolls"], 3)
        ivs = dict(a.ivs)
        day = 8
        while c.state.active is not None and day < 40:
            c.update({p: 2_000 * M}, f"2026-09-{day:02d}" if day <= 30 else f"2026-10-{day - 30:02d}")
            day += 1
        self.assertEqual(c.state.dex[-1].ivs, ivs)

    def test_save_roundtrip_and_legacy(self):
        m = C.MonState(1, [1], [1, 2], ivs={k: 10 for k in C.STAT_KEYS}, luck={"bonusRolls": 1})
        back = C.MonState.from_dict(m.to_dict())
        self.assertEqual(back.ivs, m.ivs)
        self.assertEqual(back.luck, {"bonusRolls": 1})
        legacy = C.MonState.from_dict({"baseID": 1, "pathIDs": [1], "stageIndex": 0, "usedAtStage": 0,
                                       "rarity": "common", "totalForms": 1, "ivs": {"hp": 1}})
        self.assertIsNone(legacy.ivs)                                # incomplete IV set → unknown


class LevelAndViewTests(unittest.TestCase):
    def test_level_runs_5_to_100(self):
        c = comp_with_history({})
        self.assertEqual(c.level(), 5)
        c.state.active = C.MonState(25, [25], [25, 26], stage_index=0, used_at_stage=0, rarity="common", total_forms=2,
                                    nature="jolly", ivs={k: 31 for k in C.STAT_KEYS})
        self.assertEqual(c.level(), 5)
        c.state.active.used_at_stage = C.phase_threshold("common", 2, 0) // 2   # a quarter of the way overall
        self.assertEqual(c.level(), 5 + round(95 * 0.1666667))
        c.state.active.stage_index = 1
        c.state.active.path_ids = [25, 26]
        c.state.active.used_at_stage = C.phase_threshold("common", 2, 1)
        self.assertEqual(c.level(), 100)

    def test_static_view_for_records(self):
        v = C.Companion.stats_view_static(PIKACHU, {k: 31 for k in C.STAT_KEYS}, "timid")
        self.assertEqual(v["level"], 100)
        rows = {r["key"]: r for r in v["rows"]}
        self.assertEqual(rows["hp"]["value"], C.stat_value("hp", 35, 31, 100, "timid"))
        self.assertEqual(rows["speed"]["mod"], 1)
        self.assertIsNone(v["luck"])
        unknown = C.Companion.stats_view_static(PIKACHU, None, None)
        self.assertIsNone(unknown["iv_total"])
        self.assertTrue(all(r["iv"] is None for r in unknown["rows"]))

    def test_stats_view(self):
        c = comp_with_history({})
        c.state.active = C.MonState(25, [25], [25, 26], rarity="common", total_forms=2, nature="jolly",
                                    ivs={k: 31 for k in C.STAT_KEYS}, luck={"bonusRolls": 2})
        v = c.stats_view(PIKACHU)
        self.assertEqual(v["level"], 5)
        rows = {r["key"]: r for r in v["rows"]}
        self.assertEqual(rows["hp"]["value"], C.stat_value("hp", 35, 31, 5, "jolly"))
        self.assertEqual(rows["speed"]["mod"], 1)
        self.assertEqual(v["types"], ["electric"])
        self.assertEqual(v["iv_total"], 186)
        self.assertIsNone(c.stats_view({"id": 26, "stats": {}}))     # meta for another species
        c.state.active = None
        self.assertIsNone(c.stats_view(PIKACHU))


if __name__ == "__main__":
    unittest.main()
