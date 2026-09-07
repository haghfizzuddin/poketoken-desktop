"""Blurred evolution preview: the pure image helpers in ui.py, on in-memory sprites (no display)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image, ImageChops, ImageDraw, ImageStat  # noqa: E402

from poketoken import ui  # noqa: E402

TERTIARY = "#AEAEB2"
TERTIARY_RGB = (0xAE, 0xAE, 0xB2)


def sprite(size: int = 52) -> Image.Image:
    """A toy sprite: a red disc with a blue eye, a half-transparent strip, transparent elsewhere."""
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse((8, 10, 44, 46), fill=(220, 40, 40, 255))
    d.ellipse((20, 20, 26, 26), fill=(30, 60, 200, 255))
    d.rectangle((0, 48, size - 1, size - 1), fill=(40, 200, 40, 128))
    return im


def same(a: Image.Image, b: Image.Image) -> bool:
    return a.size == b.size and a.mode == b.mode and ImageChops.difference(a, b).getbbox() is None


def distance(a: Image.Image, b: Image.Image) -> float:
    """Summed per-channel difference over the whole image."""
    return sum(ImageStat.Stat(ImageChops.difference(a, b)).sum)


class BlurRadiusTests(unittest.TestCase):
    def test_endpoints_and_scaling(self):
        self.assertAlmostEqual(ui.blur_radius(0.0), ui.PREVIEW_BLUR)
        self.assertEqual(ui.blur_radius(1.0), 0.0)
        self.assertAlmostEqual(ui.blur_radius(0.0, size=104), 2 * ui.PREVIEW_BLUR)   # radius follows the sprite size

    def test_monotonic_and_clamped(self):
        steps = [i / 20 for i in range(21)]
        radii = [ui.blur_radius(p) for p in steps]
        self.assertEqual(radii, sorted(radii, reverse=True))
        self.assertTrue(all(radii[i] > radii[i + 1] for i in range(len(radii) - 1)))
        self.assertEqual(ui.blur_radius(-3.0), ui.blur_radius(0.0))
        self.assertEqual(ui.blur_radius(7.0), 0.0)


class PreviewLevelTests(unittest.TestCase):
    def test_buckets_down_to_tenths(self):
        self.assertEqual(ui.preview_level(0.0), 0.0)
        self.assertEqual(ui.preview_level(0.05), 0.0)
        self.assertEqual(ui.preview_level(0.13), 0.1)
        self.assertEqual(ui.preview_level(0.999), 0.9)      # never sharp before it actually evolves
        self.assertEqual(ui.preview_level(1.0), 1.0)
        self.assertEqual(ui.preview_level(-1.0), 0.0)
        self.assertEqual(ui.preview_level(5.0), 1.0)
        self.assertEqual(len({ui.preview_level(i / 1000) for i in range(1001)}), ui.PREVIEW_LEVELS + 1)


class SilhouetteTests(unittest.TestCase):
    def test_keeps_alpha_and_uses_the_colour(self):
        im = sprite()
        sil = ui.silhouette(im, TERTIARY)
        self.assertEqual((sil.size, sil.mode), (im.size, "RGBA"))
        self.assertTrue(same(sil.getchannel("A"), im.getchannel("A")))
        self.assertEqual(sil.convert("RGB").getcolors(), [(im.width * im.height, TERTIARY_RGB)])

    def test_input_untouched(self):
        im = sprite()
        before = im.copy()
        ui.silhouette(im, "#000000")
        self.assertTrue(same(im, before))


class PreviewImageTests(unittest.TestCase):
    def test_full_progress_is_the_sharp_sprite(self):
        im = sprite()
        out = ui.preview_image(im, 1.0, TERTIARY)
        self.assertTrue(same(out, im))
        self.assertIsNot(out, im)
        self.assertTrue(same(ui.preview_image(im, 2.5, TERTIARY), im))    # clamped

    def test_zero_progress_is_a_blurred_silhouette(self):
        im = sprite()
        out = ui.preview_image(im, 0.0, TERTIARY)
        self.assertEqual((out.size, out.mode), (im.size, "RGBA"))
        solid = out.getchannel("A").point(lambda a: 255 if a > 128 else 0)
        mean = ImageStat.Stat(out.convert("RGB"), mask=solid).mean
        for got, want in zip(mean, TERTIARY_RGB):
            self.assertLess(abs(got - want), 12, mean)          # colours sit on the silhouette colour
        # the blur spreads the shape past its original bounds (the disc starts at row 10)
        self.assertLess(out.getchannel("A").getbbox()[1], im.getchannel("A").getbbox()[1])
        # and no black bleeds in from the transparent pixels
        darkest = min(ImageStat.Stat(out.convert("RGB"), mask=out.getchannel("A").point(lambda a: 255 if a else 0)).extrema,
                      key=lambda e: e[0])[0]
        self.assertGreater(darkest, 100)

    def test_clears_monotonically_toward_the_sprite(self):
        im = sprite()
        dist = [distance(ui.preview_image(im, p, TERTIARY), im) for p in (0.0, 0.25, 0.5, 0.75, 0.9, 1.0)]
        self.assertEqual(dist, sorted(dist, reverse=True))
        self.assertTrue(all(dist[i] > dist[i + 1] for i in range(len(dist) - 1)))
        self.assertEqual(dist[-1], 0.0)

    def test_input_untouched(self):
        im = sprite()
        before = im.copy()
        ui.preview_image(im, 0.3, TERTIARY)
        self.assertTrue(same(im, before))


if __name__ == "__main__":
    unittest.main()
