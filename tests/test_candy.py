"""Rare Candy grants from streaks and the weekly goal."""
from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poketoken import companion as C  # noqa: E402
from test_history import comp_with_history  # noqa: E402

M = 1_000_000
TODAY = date(2026, 9, 7)


def streak_rows(days: int, per_day: int = 2 * M, end: date = TODAY) -> dict[str, dict]:
    return {(end - timedelta(days=i)).isoformat(): {"tokens": per_day} for i in range(days)}


class MilestoneTests(unittest.TestCase):
    def test_reached_and_next(self):
        self.assertEqual(C.streak_milestones_reached(2), [])
        self.assertEqual(C.streak_milestones_reached(7), [(3, 1), (7, 2)])
        self.assertEqual(C.streak_milestones_reached(61), [(3, 1), (7, 2), (14, 3), (30, 5), (60, 5)])
        self.assertEqual(C.next_streak_milestone(0), (3, 1))
        self.assertEqual(C.next_streak_milestone(7), (14, 3))
        self.assertEqual(C.next_streak_milestone(30), (60, 5))
        self.assertEqual(C.next_streak_milestone(75), (90, 5))


class GrantTests(unittest.TestCase):
    def test_seed_then_pay_once(self):
        c = comp_with_history(streak_rows(10))
        today = TODAY.isoformat()
        self.assertEqual(c.evaluate_candy_grants(today), [])           # seeding: nothing paid
        self.assertTrue(c.state.candy_feature_seeded)
        self.assertEqual(c.item_count("rareCandy"), 0)
        self.assertEqual(c.evaluate_candy_grants(today), [])           # still nothing new
        # streak grows to 14 → the 14-day milestone pays 3, exactly once
        c.state.history.update(streak_rows(14))
        grants = c.evaluate_candy_grants(today)
        self.assertEqual([(g["count"], g["reason"]) for g in grants], [(3, "14-day streak")])
        self.assertEqual(c.item_count("rareCandy"), 3)
        self.assertEqual(c.evaluate_candy_grants(today), [])
        self.assertEqual([e["kind"] for e in c.drain_events()], ["candy"])

    def test_new_streak_pays_again_from_three(self):
        c = comp_with_history({})
        today = TODAY.isoformat()
        c.evaluate_candy_grants(today)                                  # seed with no streak
        c.state.history.update(streak_rows(3))
        self.assertEqual(c.evaluate_candy_grants(today)[0]["count"], 1)
        # a gap, then a fresh 3-day streak starting later → a different key → pays again
        later = TODAY + timedelta(days=10)
        c.state.history.update(streak_rows(3, end=later))
        self.assertEqual(c.evaluate_candy_grants(later.isoformat())[0]["count"], 1)
        self.assertEqual(c.item_count("rareCandy"), 2)

    def test_weekly_goal_pays_five_once(self):
        rows = {"2026-08-17": 60 * M, "2026-08-24": 100 * M, "2026-08-31": 80 * M}
        c = comp_with_history(rows)
        today = TODAY.isoformat()
        c.evaluate_candy_grants(today)                                  # seed (goal not reached)
        c.state.history["2026-09-07"] = {"tokens": 80 * M}              # hits the 80M median
        grants = c.evaluate_candy_grants(today)
        self.assertEqual(grants, [{"key": "week:2026-W37", "count": 5, "reason": "weekly goal reached"}])
        self.assertEqual(c.evaluate_candy_grants(today), [])
        self.assertEqual(c.item_count("rareCandy"), 5)

    def test_update_triggers_grants(self):
        from poketoken import usage as U
        c = comp_with_history(streak_rows(6))
        p = U.PROVIDER_ID
        c.update({p: 10 * M}, TODAY.isoformat())                        # baseline + seed (streak 6: 3-day paid)
        c.state.history.update(streak_rows(7))
        c.update({p: 11 * M}, TODAY.isoformat())
        self.assertEqual(c.item_count("rareCandy"), 2)                  # 7-day milestone


if __name__ == "__main__":
    unittest.main()
