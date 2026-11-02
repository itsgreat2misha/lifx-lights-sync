"""Tests for lifx_lights_sync.color (pure stdlib module)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lifx_lights_sync.color import (  # noqa: E402
    build_palette,
    clamp,
    hsbk_to_rgb,
    rgb_distance,
    rgb_to_hsbk,
    scale_rgb,
)


class TestRgbToHsbk(unittest.TestCase):
    def test_pure_red(self):
        hue, saturation, brightness, kelvin = rgb_to_hsbk((255, 0, 0))
        self.assertAlmostEqual(hue, 0.0, places=1)
        self.assertAlmostEqual(saturation, 1.0)
        self.assertAlmostEqual(brightness, 1.0)
        self.assertEqual(kelvin, 3500)

    def test_white_and_black(self):
        self.assertEqual(rgb_to_hsbk((255, 255, 255))[1], 0.0)
        self.assertEqual(rgb_to_hsbk((0, 0, 0))[2], 0.0)

    def test_ranges_for_varied_colors(self):
        for rgb in [(12, 200, 99), (255, 128, 0), (30, 30, 200), (99, 99, 99)]:
            hue, saturation, brightness, _ = rgb_to_hsbk(rgb)
            self.assertGreaterEqual(hue, 0.0)
            self.assertLess(hue, 360.0)
            self.assertTrue(0.0 <= saturation <= 1.0)
            self.assertTrue(0.0 <= brightness <= 1.0)

    def test_brightness_scale(self):
        full = rgb_to_hsbk((200, 100, 50))[2]
        half = rgb_to_hsbk((200, 100, 50), brightness_scale=0.5)[2]
        self.assertAlmostEqual(half, full * 0.5, places=4)

    def test_round_trip_approximately(self):
        for rgb in [(255, 0, 0), (0, 128, 255), (200, 200, 200), (64, 200, 120)]:
            back = hsbk_to_rgb(rgb_to_hsbk(rgb))
            for original, restored in zip(rgb, back):
                self.assertLessEqual(abs(original - restored), 2)

    def test_scale_and_clamp_helpers(self):
        self.assertEqual(scale_rgb((100, 50, 0), 0.5), (50, 25, 0))
        self.assertEqual(scale_rgb((200, 0, 0), 2.0), (255, 0, 0))
        self.assertEqual(clamp(2.0), 1.0)
        self.assertEqual(clamp(-1.0), 0.0)
        self.assertAlmostEqual(rgb_distance((0, 0, 0), (255, 255, 255)), 441.7, places=0)


class TestBuildPalette(unittest.TestCase):
    def test_empty_input_returns_default(self):
        palette = build_palette(None)
        self.assertGreaterEqual(len(palette), 2)
        palette = build_palette({})
        self.assertGreaterEqual(len(palette), 2)

    def test_near_blacks_are_dropped(self):
        palette = build_palette(
            {"primary": {"red": 4, "green": 2, "blue": 3}, "accent": "#ff8800"}
        )
        self.assertNotIn((4, 2, 3), palette)
        self.assertTrue(any(max(rgb) > 100 for rgb in palette))

    def test_all_near_black_falls_back_to_whites(self):
        palette = build_palette({"primary": (1, 1, 1), "accent": (8, 8, 8)})
        self.assertEqual(len(palette), 2)
        self.assertTrue(all(max(rgb) > 150 for rgb in palette))

    def test_monochrome_falls_back_to_warm_cool_whites(self):
        palette = build_palette(
            {"primary": (240, 240, 240), "accent": (200, 200, 200)}
        )
        self.assertEqual(len(palette), 2)
        self.assertNotEqual(palette[0], palette[1])

    def test_near_duplicates_are_culled(self):
        palette = build_palette(
            {
                "primary": (250, 40, 30),
                "accent": (248, 42, 31),  # near-duplicate of primary
                "on_dark": (30, 60, 240),  # genuinely different
            }
        )
        reds = [rgb for rgb in palette if rgb[0] > 200 and rgb[1] < 80]
        self.assertEqual(len(reds), 1)

    def test_single_hue_synthesizes_gradient(self):
        palette = build_palette({"primary": (200, 30, 30)})
        self.assertGreaterEqual(len(palette), 2)
        peaks = [max(rgb) for rgb in palette]
        self.assertEqual(peaks, sorted(peaks))  # gradient is ascending

    def test_saturation_is_boosted(self):
        # A muted blue should come back visibly more saturated.
        muted = (120, 130, 160)
        palette = build_palette({"primary": muted, "accent": (220, 60, 40)})
        boosted = palette[0]
        self.assertGreater(max(boosted) - min(boosted), max(muted) - min(muted))

    def test_background_fields_are_included(self):
        palette = build_palette(
            {
                "primary": (255, 0, 0),
                "background_dark": (0, 0, 220),
                "background_light": (0, 200, 80),
            }
        )
        self.assertGreaterEqual(len(palette), 3)

    def test_hex_and_sequence_values(self):
        palette = build_palette({"primary": "#00cc44", "accent": [220, 40, 90]})
        self.assertEqual(len(palette), 2)


if __name__ == "__main__":
    unittest.main()
