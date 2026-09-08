"""Battle tab helpers: the battles.json record, the hit-by-hit HP schedule, formatting (offline)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poketoken import battle as B, battle_ui as BU, companion as C, settings  # noqa: E402
from test_battle import CHART, card  # noqa: E402
from test_history import comp_with_history  # noqa: E402
from test_stats import PIKACHU  # noqa: E402


def rec(i: int, won: bool = True) -> dict:
    return {"opponent": f"Foe{i}", "trainer": "Rival", "mine": "Me", "won": won, "winner": "Me" if won else f"Foe{i}",
            "turns": i, "date": "2026-09-08", "power": [100, 90]}


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def test_missing_file_is_empty(self):
        self.assertEqual(BU.load_history(self.d), [])
        self.assertEqual(BU.tally([]), (0, 0))
        self.assertFalse(BU.history_path(self.d).exists())

    def test_append_roundtrip_and_prune(self):
        out = None
        for i in range(12):
            out = BU.append_history(self.d, rec(i, won=i % 3 == 0))
        self.assertEqual(len(out), BU.HISTORY_KEEP)
        self.assertEqual([r["turns"] for r in out], list(range(2, 12)))          # oldest two dropped, oldest first
        self.assertEqual(BU.load_history(self.d), out)                           # what was saved is what loads
        self.assertEqual(json.loads(BU.history_path(self.d).read_text("utf-8")), out)
        self.assertEqual(BU.tally(out), (3, 7))                                  # turns 3, 6, 9 won
        self.assertFalse(BU.history_path(self.d).with_suffix(".json.tmp").exists())

    def test_prune_keeps_only_dicts(self):
        self.assertEqual(BU.prune_history([1, "x", rec(1), None, rec(2)]), [rec(1), rec(2)])
        self.assertEqual(BU.prune_history([rec(i) for i in range(5)], keep=2), [rec(3), rec(4)])
        self.assertEqual(BU.prune_history([rec(1)], keep=0), [])

    def test_corrupt_file_reads_as_empty_and_recovers(self):
        p = BU.history_path(self.d)
        for junk in ("{not json", '{"a": 1}', "42", ""):
            p.write_text(junk)
            self.assertEqual(BU.load_history(self.d), [], junk)
        p.write_text('[1, "x", {"won": true, "turns": 3}]')
        self.assertEqual(BU.load_history(self.d), [{"won": True, "turns": 3}])
        p.write_text("{not json")
        self.assertEqual(BU.append_history(self.d, rec(7)), [rec(7)])            # append over a corrupt file works


class ScheduleTests(unittest.TestCase):
    def test_schedule_tracks_the_log(self):
        a, b = card("Sparky", ["electric"]), card("Splash", ["water"])
        res = B.simulate(a, b, CHART)
        sched = BU.hp_schedule(res, a, b)
        self.assertEqual(sched[0], (120, 120))
        self.assertEqual(len(sched), len(res["log"]) + 1)
        self.assertEqual(sched[-1], (res["remaining"]["Sparky"], res["remaining"]["Splash"]))
        for prev, cur in zip(sched, sched[1:]):
            self.assertLessEqual(cur[0], prev[0])
            self.assertLessEqual(cur[1], prev[1])
            self.assertNotEqual(cur, prev)                                       # every hit lands somewhere
        self.assertEqual(sched[-1][1], 0)                                        # Splash fainted
        self.assertEqual(BU.hp_schedule({"log": ["garbage"]}, a, b), [(120, 120)])
        self.assertEqual(BU.hp_schedule({}, a, b), [(120, 120)])

    def test_same_name_cards_use_speed_order(self):
        a, b = card("Pikachu", ["electric"], speed=90), card("Pikachu", ["electric"], speed=50, hp=60)
        res = B.simulate(a, b, CHART)
        sched = BU.hp_schedule(res, a, b)
        self.assertEqual(len(sched), len(res["log"]) + 1)
        loser_idx = 0 if res["loser"] is a else 1
        self.assertEqual(sched[-1][loser_idx], 0)
        self.assertGreater(sched[-1][1 - loser_idx], 0)
        for prev, cur in zip(sched, sched[1:]):
            self.assertLessEqual(cur, prev)

    def test_hp_color_and_tick(self):
        self.assertEqual(BU.hp_color(1.0), "green")
        self.assertEqual(BU.hp_color(0.51), "green")
        self.assertEqual(BU.hp_color(0.5), "orange")
        self.assertEqual(BU.hp_color(0.21), "orange")
        self.assertEqual(BU.hp_color(0.2), "red")
        self.assertEqual(BU.hp_color(0.0), "red")
        self.assertEqual(BU.tick_ms(1), BU.TICK_MS)
        self.assertEqual(BU.tick_ms(40), BU.TICK_MS)
        self.assertEqual(BU.tick_ms(100), BU.MAX_FIGHT_MS // 100)
        self.assertEqual(BU.tick_ms(10_000), BU.MIN_TICK_MS)
        self.assertEqual(BU.tick_ms(0), BU.TICK_MS)


class FormattingTests(unittest.TestCase):
    def test_sprite_key(self):
        self.assertEqual(BU.sprite_key(card("x", ["fire"])), (1, False))
        self.assertEqual(BU.sprite_key(dict(card("x", ["fire"]), shiny=1)), (1, True))
        self.assertIsNone(BU.sprite_key(None))
        for bad in (0, -3, BU.MAX_SPECIES + 1, "25", True, None, 2.5):
            self.assertIsNone(BU.sprite_key(dict(card("x", ["fire"]), species=bad)), repr(bad))
        c = card("x", ["fire"]); del c["species"]
        self.assertIsNone(BU.sprite_key(c))

    def test_won_prefers_identity(self):
        a, b = card("Twin", ["normal"]), card("Twin", ["normal"])          # equal dicts, different objects
        res = B.simulate(a, b, CHART)
        self.assertNotEqual(BU.won(res, a), BU.won(res, b))
        self.assertTrue(BU.won(res, dict(res["winner"])) or res["winner"] == res["loser"])

    def test_record_and_banner(self):
        a, b = card("Sparky", ["electric"]), card("Splash", ["water"])
        res = B.simulate(a, b, CHART)
        r = BU.record_from_result(res, a, b, when=date(2026, 9, 8))
        self.assertEqual(r, {"opponent": "Splash", "trainer": "t", "mine": "Sparky", "won": True, "winner": "Sparky",
                             "turns": res["turns"], "date": "2026-09-08", "power": [B.power_score(a), B.power_score(b)]})
        self.assertEqual(BU.record_from_result(res, a, b)["date"], date.today().isoformat())
        bn = BU.banner(res, a, b)
        self.assertTrue(bn["won"])
        self.assertEqual(bn["title"], "Victory!")
        self.assertEqual(bn["detail"], "Sparky (t) beat Splash (t)")
        self.assertEqual(bn["power"], f"{res['turns']} turns · {res['remaining']['Sparky']} HP left · "
                                      f"power {B.power_score(a)} vs {B.power_score(b)}")
        lost = BU.banner(res, b, a)
        self.assertFalse(lost["won"])
        self.assertEqual(lost["title"], "Defeat")
        self.assertTrue(BU.banner(dict(res, turns=1), a, b)["power"].startswith("1 turn ·"))

    def test_hit_row(self):
        self.assertEqual(BU.hit_row("T1: Sparky used a Electric move for 40 — super effective!  (Splash 80 HP)"),
                         ("T1  Sparky · Electric · 40 dmg · super effective", "80 HP"))
        self.assertEqual(BU.hit_row("T12: Mr. Mime used a Normal move for 4  (Splash 0 HP)"),
                         ("T12  Mr. Mime · Normal · 4 dmg", "0 HP"))
        self.assertEqual(BU.hit_row("T2: A used a Fire move for 3 — not very effective  (B 9 HP)"),
                         ("T2  A · Fire · 3 dmg · not very effective", "9 HP"))
        self.assertEqual(BU.hit_row("T2: A used a Normal move for 0 — no effect  (B (2) 9 HP)"),
                         ("T2  A · Normal · 0 dmg · no effect", "9 HP"))
        self.assertEqual(BU.hit_row("something else"), ("something else", ""))
        res = B.simulate(card("Sparky", ["electric"]), card("Splash", ["water"]), CHART)
        for ln in res["log"]:                                                    # every real line parses
            left, right = BU.hit_row(ln)
            self.assertTrue(right.endswith(" HP"), ln)
            self.assertTrue(left.startswith("T"), ln)

    def test_is_own_hit(self):
        self.assertTrue(BU.is_own_hit("T1: Sparky used a Electric move for 40  (Splash 80 HP)", "Sparky"))
        self.assertFalse(BU.is_own_hit("T1: Sparky used a Electric move for 40  (Splash 80 HP)", "Splash"))
        self.assertFalse(BU.is_own_hit("T1: Sparky Jr used a Normal move for 4  (Splash 80 HP)", "Sparky"))

    def test_own_card_needs_active_and_meta(self):
        d = Path(tempfile.mkdtemp())
        c = comp_with_history({})
        meta = dict(PIKACHU, id=1, types=["grass", "poison"])
        self.assertIsNone(BU.own_card(c, meta, d))                               # egg
        c.state.active = C.MonState(1, [1], [1, 2, 3], rarity="rare", total_forms=3, nature="jolly",
                                    ivs={k: 31 for k in C.STAT_KEYS})
        self.assertIsNone(BU.own_card(c, None, d))                               # meta not loaded yet
        mine = BU.own_card(c, meta, d)
        self.assertEqual(mine["name"], "Bulbasaur")
        self.assertEqual(mine["species"], 1)
        self.assertTrue(mine["trainer"])                                         # default trainer
        settings.set(d, "trainer", "Fizz")
        self.assertEqual(BU.own_card(c, meta, d)["trainer"], "Fizz")
        self.assertEqual(B.decode_card(B.encode_card(mine)), mine)


class StateTests(unittest.TestCase):
    def test_reset_and_finished(self):
        st = BU.BattleState()
        self.assertFalse(st.finished)
        st.q.put(("chart", {})); st.q.put(("err", "x"))
        st.mine, st.result, st.schedule, st.step, st.pending, st.recorded = {}, {"log": []}, [(1, 1), (0, 1)], 0, True, True
        self.assertFalse(st.finished)
        st.step = 1
        self.assertTrue(st.finished)
        st.challenger, st.error, st.history = {"name": "x"}, "bad", [rec(1)]
        st.reset_fight()
        self.assertTrue(st.q.empty())
        self.assertEqual((st.mine, st.result, st.schedule, st.step, st.pending, st.recorded), (None, None, [], 0, False, False))
        self.assertEqual((st.challenger, st.error, st.history), ({"name": "x"}, "bad", [rec(1)]))   # untouched
        self.assertFalse(st.finished)


if __name__ == "__main__":
    unittest.main()
