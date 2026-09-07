"""Activity history: per-day stats, best 5h block, streaks, weekly goal."""
from __future__ import annotations

import random
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poketoken import companion as C, usage as U  # noqa: E402
from test_companion import FakeAPI  # noqa: E402

M = 1_000_000


def entry(day: str, hour: int, tokens: int, model="claude-fable-5-1", cache_read=0) -> U.Entry:
    ts = U._iso_epoch(f"{day}T{hour:02d}:00:00+00:00")
    import datetime as dt
    local_day = dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    return U.Entry(id=f"{day}-{hour}-{tokens}", ts=ts, local_day=local_day, model=model,
                   input=tokens - cache_read, output=0, cache_write_5m=0, cache_write_1h=0, cache_read=cache_read)


def comp_with_history(rows: dict[str, int]) -> C.Companion:
    tmp = Path(tempfile.mkdtemp())
    c = C.Companion(FakeAPI(), tmp / "s.json", rng=random.Random(1), clock=lambda: 0, log=lambda m: None)
    c.state.history = {d: {"tokens": t} for d, t in rows.items()}
    return c


class DayStatsTests(unittest.TestCase):
    def test_best_block_two_pointer(self):
        d = "2026-09-01"
        es = [entry(d, 1, 10), entry(d, 2, 20), entry(d, 3, 30), entry(d, 9, 5), entry(d, 10, 7)]
        self.assertEqual(U.best_block(es), 60)         # 01:00-03:00 fits in one 5h window; 09:00+ does not
        self.assertEqual(U.best_block([]), 0)

    def test_day_stats_fields(self):
        es = [entry("2026-09-01", 12, 100, cache_read=80), entry("2026-09-02", 12, 50)]
        st = U.day_stats(es)
        days = sorted(st)
        self.assertEqual(len(days), 2)
        first = st[days[0]]
        self.assertEqual(first["tokens"], 100)
        self.assertEqual(first["cacheRead"], 80)
        self.assertAlmostEqual(first["cacheRatio"], 0.8)
        self.assertEqual(first["bestBlock"], 100)
        self.assertGreater(first["cost"], 0)


class StreakTests(unittest.TestCase):
    def test_streak_counts_today_when_over_threshold(self):
        today = date(2026, 9, 7)
        rows = {(today - timedelta(days=i)).isoformat(): 2 * M for i in range(5)}
        c = comp_with_history(rows)
        self.assertEqual(c.streak(today.isoformat()), (5, (today - timedelta(days=4)).isoformat(), True))

    def test_streak_survives_quiet_today(self):
        today = date(2026, 9, 7)
        rows = {(today - timedelta(days=i)).isoformat(): 2 * M for i in range(1, 4)}
        rows[today.isoformat()] = 200_000                    # under 1M so far
        c = comp_with_history(rows)
        self.assertEqual(c.streak(today.isoformat()), (3, (today - timedelta(days=3)).isoformat(), False))

    def test_gap_breaks_streak(self):
        today = date(2026, 9, 7)
        rows = {today.isoformat(): 5 * M, (today - timedelta(days=2)).isoformat(): 5 * M}
        c = comp_with_history(rows)
        self.assertEqual(c.streak(today.isoformat())[0], 1)
        self.assertEqual(comp_with_history({}).streak(today.isoformat()), (0, None, False))


class WeeklyGoalTests(unittest.TestCase):
    def test_locked_until_two_weeks(self):
        c = comp_with_history({"2026-09-01": 10 * M})           # W36 only, current week W37 (Sept 7)
        g = c.weekly_goal("2026-09-07")
        self.assertIsNone(g["target"])
        self.assertEqual(g["weeks_needed"], 1)

    def test_median_with_floor(self):
        rows = {"2026-08-17": 60 * M, "2026-08-24": 100 * M, "2026-08-31": 80 * M, "2026-09-07": 30 * M}
        c = comp_with_history(rows)
        g = c.weekly_goal("2026-09-07")
        self.assertEqual(g["target"], 80 * M)                   # median of 60/100/80
        self.assertEqual(g["current"], 30 * M)
        self.assertAlmostEqual(g["progress"], 0.375)
        low = comp_with_history({"2026-08-24": 1 * M, "2026-08-31": 2 * M})
        self.assertEqual(low.weekly_goal("2026-09-07")["target"], C.WEEKLY_GOAL_MIN)

    def test_week_key_iso(self):
        self.assertEqual(C.Companion.week_key("2026-09-07"), "2026-W37")
        self.assertEqual(C.Companion.week_key("2026-01-01"), "2026-W01")


class HistoryPersistenceTests(unittest.TestCase):
    def test_record_merge_prune_roundtrip(self):
        c = comp_with_history({})
        old = (date(2026, 9, 7) - timedelta(days=C.HISTORY_DAYS + 5)).isoformat()
        c.record_history({old: {"tokens": 5}, "2026-09-06": {"tokens": 7}}, "2026-09-07")
        c.record_history({"2026-09-06": {"tokens": 9}, "2026-09-07": {"tokens": 1}}, "2026-09-07", backfill=True)
        self.assertNotIn(old, c.state.history)
        self.assertEqual(c.state.history["2026-09-06"]["tokens"], 9)
        self.assertTrue(c.state.history_backfilled)
        again = C.Companion(FakeAPI(), c.path, log=lambda m: None)
        self.assertEqual(again.state.history, c.state.history)
        self.assertTrue(again.state.history_backfilled)
        self.assertEqual(C.CompanionState.from_dict({"history": {"x": {"nope": 1}, "y": "bad"}}).history, {})


if __name__ == "__main__":
    unittest.main()
