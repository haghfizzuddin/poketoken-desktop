"""Pure layout arithmetic for the window."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poketoken import layout as L  # noqa: E402


class LayoutTests(unittest.TestCase):
    def test_columns_for_width(self):
        # one column below the 'md' breakpoint, whatever the arithmetic would allow
        self.assertEqual(L.columns_for(392), 1)
        self.assertEqual(L.columns_for(700), 1)
        self.assertEqual(L.columns_for(740), 1)
        self.assertEqual(L.columns_for(768), 2)
        self.assertEqual(L.columns_for(1100), 2)          # 'md' is two columns at most
        self.assertEqual(L.columns_for(1280), 3)
        self.assertEqual(L.columns_for(1920), 3)          # capped by MAX_CONTENT, not the screen
        self.assertEqual(L.columns_for(1920, max_cols=4), 3)

    def test_column_geometry_spans_the_content_frame(self):
        geo = L.column_geometry(1000, 3)
        self.assertEqual(len(geo), 3)
        self.assertAlmostEqual(geo[0][0], L.MARGIN)
        self.assertAlmostEqual(geo[-1][0] + geo[-1][1], 1000 - L.MARGIN)
        self.assertTrue(all(abs(w - geo[0][1]) < 1e-9 for _, w in geo))
        x, w = L.column_geometry(392, 1)[0]
        self.assertEqual((x, w), (12, 368))                # compact margin, more usable width
        wide = L.column_geometry(1920, 3)                  # centred, never wider than MAX_CONTENT
        self.assertAlmostEqual(wide[0][0], (1920 - L.MAX_CONTENT) / 2)

    def test_content_frame_centres_and_caps(self):
        self.assertEqual(L.content_frame(392), (12, 368))
        self.assertEqual(L.content_frame(800), (16, 768))
        x, w = L.content_frame(1920)
        self.assertEqual(w, L.MAX_CONTENT)
        self.assertAlmostEqual(x + w / 2, 960)             # centred
        self.assertEqual(L.content_frame(700, max_width=560)[1], 560)

    def test_grid_fits_cells(self):
        self.assertEqual(L.grid(368, 160)[0], 2)
        self.assertEqual(L.grid(1208, 160)[0], 7)
        self.assertEqual(L.grid(1208, 160, max_cols=4)[0], 4)
        cols, cw = L.grid(368, 160)
        self.assertAlmostEqual(cols * cw + (cols - 1) * 12, 368)
        self.assertEqual(L.grid(100, 500)[0], 1)
        self.assertEqual(L.cell_xy(3, 3, 100, 80, 10, 20), (10, 20 + 80 + 12))
        self.assertEqual(L.grid_height(4, 3, 80), 80 * 2 + 12)
        self.assertEqual(L.grid_height(0, 3, 80), 0)

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
