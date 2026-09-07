"""Sprite fitting helpers (pure, no display)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poketoken import ui  # noqa: E402


class FitTests(unittest.TestCase):
    def test_fit_scale_is_largest_whole_factor(self):
        self.assertEqual(ui.fit_scale(62, 71, 360), 5)
        self.assertEqual(ui.fit_scale(96, 96, 360), 3)
        self.assertEqual(ui.fit_scale(40, 40, 360), 9)
        self.assertEqual(ui.fit_scale(400, 40, 360), 0)       # must downscale
        self.assertEqual(ui.fit_scale(0, 10, 360), 1)

    def test_union_bbox_covers_every_frame(self):
        a = Image.new("RGBA", (96, 96)); a.paste((255, 0, 0, 255), (10, 20, 30, 40))
        b = Image.new("RGBA", (96, 96)); b.paste((0, 255, 0, 255), (50, 5, 60, 70))
        self.assertEqual(ui.union_bbox([a, b]), (10, 5, 60, 70))
        self.assertIsNone(ui.union_bbox([Image.new("RGBA", (8, 8))]))

    def test_defaults(self):
        self.assertIn(ui.DEFAULT_SPRITE_BOX, ui.SPRITE_BOXES)
        self.assertEqual(ui.DEFAULT_SPRITE_BOX, 320)


if __name__ == "__main__":
    unittest.main()
