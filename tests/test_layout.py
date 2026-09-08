"""Pure layout arithmetic for the window."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poketoken import layout as L  # noqa: E402


class LayoutTests(unittest.TestCase):
    def test_columns_for_width(self):
        self.assertEqual(L.columns_for(392), 1)
        self.assertEqual(L.columns_for(700), 1)
        self.assertEqual(L.columns_for(740), 2)
        self.assertEqual(L.columns_for(1100), 3)
        self.assertEqual(L.columns_for(1920), 3)
        self.assertEqual(L.columns_for(1920, max_cols=4), 4)

    def test_column_geometry_spans_usable_width(self):
        geo = L.column_geometry(1000, 3)
        self.assertEqual(len(geo), 3)
        self.assertAlmostEqual(geo[0][0], L.MARGIN)
        self.assertAlmostEqual(geo[-1][0] + geo[-1][1], 1000 - L.MARGIN)
        self.assertTrue(all(abs(w - geo[0][1]) < 1e-9 for _, w in geo))
        x, w = L.column_geometry(392, 1)[0]
        self.assertEqual((x, w), (16, 360))

    def test_masonry_shortest_column_and_pins(self):
        self.assertEqual(L.masonry([100, 50, 50, 200], 1), [(0, 0), (0, 110), (0, 170), (0, 230)])
        placed = L.masonry([300, 100, 100, 100], 2)
        self.assertEqual(placed[0], (0, 0))
        self.assertEqual(placed[1], (1, 0))
        self.assertEqual(placed[2], (1, 110))            # column 1 still shorter than 300
        self.assertEqual(placed[3], (1, 220))
        pinned = L.masonry([100, 100, 100], 3, pinned={1: 0})
        self.assertEqual(pinned[1][0], 0)
        self.assertEqual(L.masonry([], 3), [])
        self.assertEqual(L.masonry([50], 3, pinned={0: 9})[0][0], 2)   # pin clamped to the last column

    def test_top_n_folds_rest(self):
        items = {"a": 50, "b": 30, "c": 15, "d": 5}
        self.assertEqual(L.top_n(items, 2), [("a", 50), ("b", 30), ("other", 20)])
        self.assertEqual(L.top_n(items, 4), [("a", 50), ("b", 30), ("c", 15), ("d", 5)])
        self.assertEqual(L.top_n({}, 3), [])

    def test_short_model(self):
        self.assertEqual(L.short_model("claude-fable-5-1"), "Fable 5.1")
        self.assertEqual(L.short_model("claude-opus-5"), "Opus 5")
        self.assertEqual(L.short_model("claude-haiku-4-5-20251001"), "Haiku 4.5")
        self.assertEqual(L.short_model("<synthetic>"), "<synthetic>")
        self.assertEqual(L.short_model("gpt-5.5"), "gpt-5.5")


if __name__ == "__main__":
    unittest.main()
