"""Offline tests for the companion loop, using a fake PokéAPI (no network)."""
from __future__ import annotations

import json
import random
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poketoken import companion as C, pricing, usage as U  # noqa: E402
from poketoken.pokeapi import EvoLine, EvoNode, PokeAPIError  # noqa: E402

M = 1_000_000


class FakeAPI:
    """Two lines: #1 Bulbasaur→Ivysaur→Venusaur (common, cap 45 → actually rare by rule),
    and #16 Pidgey→Pidgeotto→Pidgeot (common, cap 255). Base index carries both."""

    def __init__(self):
        self.lines = {
            1: EvoLine(1, EvoNode(1, [EvoNode(2, [EvoNode(3)])]), "rare",
                       {1: {"en": "Bulbasaur"}, 2: {"en": "Ivysaur"}, 3: {"en": "Venusaur"}}),
            16: EvoLine(16, EvoNode(16, [EvoNode(17, [EvoNode(18)])]), "common",
                        {16: {"en": "Pidgey"}, 17: {"en": "Pidgeotto"}, 18: {"en": "Pidgeot"}}),
            133: EvoLine(133, EvoNode(133, [EvoNode(134), EvoNode(135), EvoNode(136)]), "uncommon",
                         {133: {"en": "Eevee"}, 134: {"en": "Vaporeon"}, 135: {"en": "Jolteon"}, 136: {"en": "Flareon"}}),
        }
        self.index = [(1, 45), (16, 255), (133, 45)]
        self.offline = False
        self.sprites: list[tuple] = []

    def base_index(self):
        if self.offline:
            raise PokeAPIError("offline")
        return self.index

    def line(self, base_id):
        if self.offline:
            raise PokeAPIError("offline")
        return self.lines[base_id]

    def sprite(self, *a, **k):
        self.sprites.append(a)
        return None

    def random_base_via_rest(self, rng, tier=None):
        raise PokeAPIError("offline")


def make(rng_seed=1, api=None):
    tmp = Path(tempfile.mkdtemp())
    api = api or FakeAPI()
    clock = {"t": 1_000_000.0}
    comp = C.Companion(api, tmp / "state.json", rng=random.Random(rng_seed), clock=lambda: clock["t"], log=lambda m: None)
    return comp, api, clock, tmp


class EvoPathTests(unittest.TestCase):
    """The species page's line: root to leaf through one form, one branch where the tree forks."""

    def test_linear_chain_through_the_middle(self):
        tree = EvoNode(16, [EvoNode(17, [EvoNode(18)])])
        self.assertEqual(tree.path_through(17), [16, 17, 18])
        self.assertEqual(tree.path_through(18), [16, 17, 18])
        self.assertEqual(tree.path_through(16), [16, 17, 18])

    def test_branch_prefers_an_owned_form_else_the_first(self):
        eevee = EvoNode(133, [EvoNode(134), EvoNode(135), EvoNode(136)])
        self.assertEqual(eevee.path_through(133), [133, 134])
        self.assertEqual(eevee.path_through(133, prefer=lambda s: s == 136), [133, 136])
        self.assertEqual(eevee.path_through(135), [133, 135])            # a leaf: nothing below

    def test_not_in_tree_is_empty(self):
        self.assertEqual(EvoNode(1, [EvoNode(2)]).path_through(99), [])

    def test_line_for_walks_up_to_the_base(self):
        from unittest import mock
        from poketoken.pokeapi import PokeAPI
        d = Path(tempfile.mkdtemp())
        api = PokeAPI(d / "cache", d / "sprites")
        api._species = {195: {"id": 195, "evolves_from": 194}, 194: {"id": 194, "evolves_from": None}}
        with mock.patch.object(api, "line", side_effect=lambda b: ("line", b)) as line:
            self.assertEqual(api.line_for(195), ("line", 194))
            line.assert_called_once_with(194)
        # already in memory under its base: no lookup at all
        api._lines = {194: EvoLine(194, EvoNode(194, [EvoNode(195)]), "common", {})}
        with mock.patch.object(api, "species", side_effect=AssertionError("no lookup")):
            self.assertIs(api.line_for(195), api._lines[194])


class BalanceTests(unittest.TestCase):
    def test_phase_thresholds_sum_to_graduation_total(self):
        for rarity, total in C.GRADUATION_TOTAL.items():
            for k in (1, 2, 3):
                s = sum(C.phase_threshold(rarity, k, i) for i in range(k))
                self.assertAlmostEqual(s, total, delta=k)   # rounding per stage

    def test_common_three_stage_first_threshold_is_125m(self):
        self.assertEqual(C.phase_threshold("common", 3, 0), 125 * M)
        self.assertEqual(C.phase_threshold("common", 3, 1), 250 * M)
        self.assertEqual(C.phase_threshold("common", 3, 2), 375 * M)

    def test_egg_prices(self):
        self.assertEqual(C.egg_price(None), 1_000 * M)
        self.assertEqual(C.egg_price("uncommon"), 2_500 * M)
        self.assertEqual(C.egg_price("rare"), 4_000 * M)


class LoopTests(unittest.TestCase):
    def test_baseline_then_incubate_hatch_evolve_graduate(self):
        comp, api, clock, _ = make()
        p = U.PROVIDER_ID
        # First observation only sets the baseline — nothing credited.
        comp.update({p: 40 * M}, "2026-09-07")
        self.assertTrue(comp.state.install_baseline_set)
        self.assertEqual(comp.state.egg_usage, 0)
        self.assertIsNotNone(comp.state.pending_hatch_id)      # egg prefetch rolled a species
        # +3M → still an egg.
        comp.update({p: 43 * M}, "2026-09-07")
        self.assertTrue(comp.is_egg)
        self.assertEqual(comp.state.egg_usage, 3 * M)
        # +4M → crosses 5M, hatches; 2M overflow carries into the hatchling.
        comp.update({p: 47 * M}, "2026-09-07")
        self.assertFalse(comp.is_egg)
        a = comp.state.active
        self.assertEqual(a.used_at_stage, 2 * M)
        self.assertEqual(a.stage_index, 0)
        self.assertEqual(comp.state.used_since_install, 7 * M)
        self.assertIn(a.base_id, api.lines)
        # Drive to graduation with big daily deltas across day changes.
        forms = a.total_forms
        total = C.GRADUATION_TOTAL[a.rarity]
        comp.update({p: 100 * M}, "2026-09-08")            # new day → whole today total credited
        self.assertEqual(comp.state.used_since_install, 107 * M)
        day = 9
        while comp.state.active is not None and day < 60:
            comp.update({p: total // 4}, f"2026-09-{day:02d}" if day <= 30 else f"2026-10-{day - 30:02d}")
            day += 1
        self.assertIsNone(comp.state.active, "should have graduated")
        self.assertEqual(len(comp.state.dex), 1)
        entry = comp.state.dex[0]
        self.assertEqual(len(entry.chain_order), forms)
        self.assertFalse(entry.is_released)
        self.assertIn(f"{entry.base_id}:{entry.final_id}", comp.state.collected_finals)
        kinds = [e["kind"] for e in comp.drain_events()]
        self.assertEqual(kinds.count("hatch"), 1)
        self.assertEqual(kinds.count("evolve"), forms - 1)
        self.assertEqual(kinds.count("graduate"), 1)
        self.assertEqual(comp.state.egg_usage, 0)           # fresh egg

    def test_regression_rebases_instead_of_crediting(self):
        comp, _, _, _ = make()
        p = U.PROVIDER_ID
        comp.update({p: 10 * M}, "2026-09-07")
        comp.update({p: 12 * M}, "2026-09-07")
        self.assertEqual(comp.state.egg_usage, 2 * M)
        comp.update({p: 5 * M}, "2026-09-07")            # log rotated / smaller
        self.assertEqual(comp.state.egg_usage, 2 * M)
        comp.update({p: 6 * M}, "2026-09-07")
        self.assertEqual(comp.state.egg_usage, 3 * M)

    def test_empty_observation_does_not_move_ledger(self):
        comp, _, _, _ = make()
        p = U.PROVIDER_ID
        comp.update({p: 10 * M}, "2026-09-07")
        comp.update({}, "2026-09-07", has_usage_data=False)
        comp.update({p: 11 * M}, "2026-09-07")
        self.assertEqual(comp.state.egg_usage, 1 * M)

    def test_offline_keeps_egg_and_banks_usage(self):
        comp, api, _, _ = make()
        p = U.PROVIDER_ID
        comp.update({p: 10 * M}, "2026-09-07")
        api.offline = True
        comp.state.pending_hatch_id = None
        comp.update({p: 20 * M}, "2026-09-07")          # 10M ≥ hatch threshold but no network
        self.assertTrue(comp.is_egg)
        self.assertEqual(comp.state.egg_usage, 10 * M)
        api.offline = False
        comp.update({p: 21 * M}, "2026-09-07")
        self.assertFalse(comp.is_egg)
        self.assertEqual(comp.state.active.used_at_stage, 6 * M)

    def test_state_roundtrip(self):
        comp, api, _, tmp = make()
        p = U.PROVIDER_ID
        comp.update({p: 10 * M}, "2026-09-07")
        comp.update({p: 16 * M}, "2026-09-07")
        raw = json.loads((tmp / "state.json").read_text())
        self.assertIn("usedSinceInstall", raw)
        comp2 = C.Companion(api, tmp / "state.json", rng=random.Random(2), clock=lambda: 0, log=lambda m: None)
        self.assertEqual(comp2.state.active.to_dict(), comp.state.active.to_dict())
        self.assertEqual(comp2.state.claimed_today_tokens_by_provider, {p: 16 * M})

    def test_corrupt_state_is_backed_up(self):
        comp, api, _, tmp = make()
        (tmp / "state.json").write_text("{not json")
        comp2 = C.Companion(api, tmp / "state.json", log=lambda m: None)
        self.assertTrue(comp2.is_egg)
        self.assertTrue(any(f.name.startswith("state.json.corrupt-") for f in tmp.iterdir()))


class ShopBagTests(unittest.TestCase):
    def _rich(self):
        comp, api, _, _ = make()
        p = U.PROVIDER_ID
        comp.update({p: 10 * M}, "2026-09-07")
        comp.update({p: 20 * M}, "2026-09-07")                     # hatches
        comp.state.used_since_install += 5_000 * M               # pretend wallet
        return comp

    def test_buy_and_use_candy(self):
        comp = self._rich()
        w = comp.wallet
        ok, _ = comp.buy("rareCandy")
        self.assertTrue(ok)
        self.assertEqual(comp.wallet, w - C.RARE_CANDY_PRICE)
        used_before = comp.state.used_since_install
        ok, msg = comp.use_rare_candy()
        self.assertTrue(ok, msg)
        self.assertEqual(comp.state.used_since_install, used_before)   # candy is not "usage"
        self.assertEqual(comp.item_count("rareCandy"), 0)

    def test_shiny_charm_is_one_time(self):
        comp = self._rich()
        self.assertTrue(comp.buy("shinyCharm")[0])
        self.assertFalse(comp.buy("shinyCharm")[0])
        self.assertTrue(comp.owns_shiny_charm)

    def test_mint_changes_nature(self):
        comp = self._rich()
        comp.buy("mint")
        before = comp.state.active.nature
        ok, _ = comp.use_mint()
        self.assertTrue(ok)
        self.assertNotEqual(comp.state.active.nature, before)

    def test_egg_purchase_releases_and_guarantees(self):
        comp = self._rich()
        ok, msg = comp.buy("egg:rare")
        self.assertTrue(ok, msg)
        self.assertTrue(comp.is_egg)
        self.assertEqual(comp.state.egg_tier, "rare")
        self.assertEqual(len(comp.state.dex), 1)
        self.assertTrue(comp.state.dex[0].is_released)
        # the rare guarantee narrows the pool to cap ≤ 45 → ids 1 / 133 only
        self.assertIn(comp.state.pending_hatch_id, (1, 133))
        self.assertFalse(comp.buy("egg:bogus")[0])

    def test_guaranteed_egg_never_hatches_below_tier(self):
        # FakeAPI: id 133 has cap 45 (passes the rare filter) but its line says "uncommon" —
        # the hatch guard must discard that roll and try again; id 1 is genuinely rare.
        for seed in range(6):
            comp, api, _, _ = make(rng_seed=seed)
            p = U.PROVIDER_ID
            comp.update({p: 10 * M}, "2026-09-07")
            comp.state.used_since_install += 5_000 * M
            self.assertTrue(comp.buy("egg:rare")[0])
            day = 8
            while comp.is_egg and day < 20:
                comp.update({p: 100 * M}, f"2026-09-{day:02d}")
                day += 1
            self.assertFalse(comp.is_egg, f"seed {seed}: never hatched")
            self.assertEqual(comp.state.active.base_id, 1, f"seed {seed}: hatched below the rare guarantee")
            self.assertIsNone(comp.state.egg_tier)


class UsageTests(unittest.TestCase):
    LINE = ('{"type":"assistant","timestamp":"2026-09-07T07:57:07.826Z","requestId":"req_1",'
            '"message":{"id":"msg_1","model":"claude-fable-5-1","usage":{"input_tokens":2,'
            '"cache_creation_input_tokens":19035,"cache_read_input_tokens":26932,"output_tokens":447,'
            '"cache_creation":{"ephemeral_1h_input_tokens":19035,"ephemeral_5m_input_tokens":0}}}}')

    def test_parse_line(self):
        e = U.parse_line(self.LINE)
        self.assertIsNotNone(e)
        self.assertEqual(e.id, "msg_1|req_1")
        self.assertEqual(e.total, 2 + 19035 + 26932 + 447)
        self.assertEqual(e.cache_write_1h, 19035)
        self.assertEqual(e.cache_write_5m, 0)
        expected = (2 * 10 + 447 * 50 + 19035 * 20 + 26932 * 0.25) / M
        self.assertAlmostEqual(e.cost, expected, places=9)

    def test_non_assistant_lines_ignored(self):
        self.assertIsNone(U.parse_line('{"type":"user","message":{"usage":{}},"timestamp":"x"}'))
        self.assertIsNone(U.parse_line("not json"))

    def test_dedup_keeps_max(self):
        a = U.parse_line(self.LINE)
        b = U.parse_line(self.LINE.replace('"output_tokens":447', '"output_tokens":900'))
        out = U.dedup_keep_max([a, b, a])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].output, 900)

    def test_incremental_reader(self):
        tmp = Path(tempfile.mkdtemp())
        f = tmp / "s.jsonl"
        f.write_text(self.LINE + "\n")
        r = U.UsageReader([tmp])
        self.assertEqual(len(r.scan(0)), 1)
        with f.open("a") as fh:
            fh.write(self.LINE.replace("msg_1", "msg_2") + "\n")
        self.assertEqual(len(r.scan(0)), 2)
        self.assertEqual(r.last_scan_lines, 1)           # only the appended line was parsed
        f.write_text(self.LINE + "\n")                    # rewritten smaller → full reparse
        self.assertEqual(len(r.scan(0)), 1)

    def test_project_label_and_aggregates(self):
        home = str(Path.home()).replace("/", "-")
        self.assertEqual(U.project_label(f"/x/.claude/projects/{home}-repo-app/s.jsonl"), "repo-app")
        self.assertEqual(U.project_label(f"/x/.claude/projects/{home}/s.jsonl"), "~")
        self.assertEqual(U.project_label("/x/.claude/projects/-srv-work/s.jsonl"), "srv-work")
        self.assertEqual(U.project_label("/nowhere/s.jsonl"), "")
        a = U.parse_line(self.LINE, "repo-app")
        b = U.parse_line(self.LINE.replace("msg_1", "msg_2"), "other-proj")
        import datetime as dt
        snap = U.summarize([a, b], dt.datetime.fromtimestamp(a.ts))
        self.assertEqual(snap.projects_today, {"repo-app": a.total, "other-proj": b.total} if a.total >= b.total
                         else {"other-proj": b.total, "repo-app": a.total})
        self.assertAlmostEqual(sum(snap.models_cost_today.values()), a.cost + b.cost)

    def test_burn_tiers(self):
        self.assertEqual(U.burn_tier(None), "idle")
        self.assertEqual(U.burn_tier(999), "idle")
        self.assertEqual(U.burn_tier(50_000), "normal")
        self.assertEqual(U.burn_tier(150_000), "fast")
        self.assertEqual(U.burn_tier(500_000), "blazing")

    def test_pricing_fallbacks(self):
        self.assertEqual(pricing.rate("claude-fable-5-1").cache_read, 0.25 / M)
        self.assertEqual(pricing.rate("claude-fable-5-1-20261201").cache_read, 0.25 / M)
        self.assertEqual(pricing.rate("claude-fable-5-2-preview").cache_read, 1.0 / M)   # unknown Fable: conservative
        self.assertEqual(pricing.rate("claude-opus-9").input, 5 / M)
        self.assertEqual(pricing.rate("<synthetic>"), pricing.ZERO)


if __name__ == "__main__":
    unittest.main()


class DittoTests(unittest.TestCase):
    def test_hit_rule(self):
        self.assertTrue(C.ditto_disguise_hit("common", 2, 128))
        self.assertFalse(C.ditto_disguise_hit("common", 2, 129))
        self.assertFalse(C.ditto_disguise_hit("common", 1, 128))
        self.assertFalse(C.ditto_disguise_hit("rare", 3, 128))

    def test_disguise_reveals_at_first_threshold_and_hides_shiny(self):
        comp, api, _, _ = make(rng_seed=4)
        api.lines[132] = EvoLine(132, EvoNode(132), "rare", {132: {"en": "Ditto"}})
        p = U.PROVIDER_ID
        comp.update({p: 10 * M}, "2026-09-07")
        comp.state.pending_hatch_id = 16                      # Pidgey: common, 3 forms
        comp.rng.getrandbits = lambda n: 0                    # every roll hits: shiny AND ditto
        comp.update({p: 16 * M}, "2026-09-07")
        a = comp.state.active
        self.assertEqual(a.base_id, 16)
        self.assertTrue(a.is_shiny)
        self.assertEqual(a.ditto_disguise, 16)
        self.assertTrue(a.is_disguised)
        self.assertFalse(a.shiny_visible)                     # hidden while disguised
        hatch = [e for e in comp.drain_events() if e["kind"] == "hatch"][0]
        self.assertFalse(hatch["shiny"])
        thr = C.phase_threshold("common", 3, 0)
        comp.update({p: 16 * M + thr + 5 * M}, "2026-09-07")  # crosses the first evolution threshold
        a = comp.state.active
        self.assertEqual(a.base_id, 132)
        self.assertTrue(a.ditto_revealed)
        self.assertFalse(a.is_disguised)
        self.assertTrue(a.shiny_visible)
        self.assertEqual(a.rarity, "rare")
        self.assertEqual(a.total_forms, 1)
        self.assertEqual(a.used_at_stage, 6 * M)             # 5M past the threshold + 1M hatch overflow
        kinds = [e["kind"] for e in comp.drain_events()]
        self.assertIn("dittoReveal", kinds)
        self.assertNotIn("evolve", kinds)
        self.assertEqual(comp.display_name(), "Ditto")

    def test_reveal_survives_offline(self):
        comp, api, _, _ = make(rng_seed=4)
        api.lines[132] = EvoLine(132, EvoNode(132), "rare", {132: {"en": "Ditto"}})
        p = U.PROVIDER_ID
        comp.update({p: 10 * M}, "2026-09-07")
        comp.state.pending_hatch_id = 16
        comp.rng.getrandbits = lambda n: 0
        comp.update({p: 16 * M}, "2026-09-07")
        thr = C.phase_threshold("common", 3, 0)
        real_line = api.line
        api.line = lambda sid: (_ for _ in ()).throw(PokeAPIError("offline")) if sid == 132 else real_line(sid)
        comp.update({p: 16 * M + thr + M}, "2026-09-07")
        self.assertTrue(comp.state.active.is_disguised)      # kept, not lost
        self.assertEqual(comp.state.active.used_at_stage, thr + 2 * M)   # incl. 1M hatch overflow
        api.line = real_line
        comp.update({p: 16 * M + thr + 2 * M}, "2026-09-07")
        self.assertEqual(comp.state.active.base_id, 132)
        self.assertEqual(comp.state.active.used_at_stage, 3 * M)
