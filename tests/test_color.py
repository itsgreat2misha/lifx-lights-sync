"""Tests for lifx_lights_sync.color (pure stdlib module)."""

from __future__ import annotations

import pytest

from lifx_lights_sync.color import (
    build_palette,
    clamp,
    hsbk_to_rgb,
    rgb_distance,
    rgb_to_hsbk,
    scale_rgb,
)


class TestRgbToHsbk:
    """Tests for Rgb To Hsbk."""

    def test_pure_red(self) -> None:
        """Pure red."""
        hue, saturation, brightness, kelvin = rgb_to_hsbk((255, 0, 0))
        assert (hue) == pytest.approx((0.0), abs=0.05)
        assert (saturation) == pytest.approx(1.0, abs=5e-08)
        assert (brightness) == pytest.approx(1.0, abs=5e-08)
        assert (kelvin) == (3500)

    def test_white_and_black(self) -> None:
        """White and black."""
        assert (rgb_to_hsbk((255, 255, 255))[1]) == (0.0)
        assert (rgb_to_hsbk((0, 0, 0))[2]) == (0.0)

    def test_ranges_for_varied_colors(self) -> None:
        """Ranges for varied colors."""
        for rgb in [(12, 200, 99), (255, 128, 0), (30, 30, 200), (99, 99, 99)]:
            hue, saturation, brightness, _ = rgb_to_hsbk(rgb)
            assert (hue) >= (0.0)
            assert (hue) < (360.0)
            assert 0.0 <= saturation <= 1.0
            assert 0.0 <= brightness <= 1.0

    def test_brightness_scale(self) -> None:
        """Brightness scale."""
        full = rgb_to_hsbk((200, 100, 50))[2]
        half = rgb_to_hsbk((200, 100, 50), brightness_scale=0.5)[2]
        assert (half) == pytest.approx((full * 0.5), abs=5e-05)

    def test_round_trip_approximately(self) -> None:
        """Round trip approximately."""
        for rgb in [(255, 0, 0), (0, 128, 255), (200, 200, 200), (64, 200, 120)]:
            back = hsbk_to_rgb(rgb_to_hsbk(rgb))
            for original, restored in zip(rgb, back, strict=True):
                assert (abs(original - restored)) <= (2)

    def test_scale_and_clamp_helpers(self) -> None:
        """Scale and clamp helpers."""
        assert (scale_rgb((100, 50, 0), 0.5)) == ((50, 25, 0))
        assert (scale_rgb((200, 0, 0), 2.0)) == ((255, 0, 0))
        assert (clamp(2.0)) == (1.0)
        assert (clamp(-1.0)) == (0.0)
        assert (rgb_distance((0, 0, 0), (255, 255, 255))) == pytest.approx((441.7), abs=0.5)


class TestBuildPalette:
    """Tests for Build Palette."""

    def test_empty_input_returns_default(self) -> None:
        """Empty input returns default."""
        palette = build_palette(None)
        assert (len(palette)) >= (2)
        palette = build_palette({})
        assert (len(palette)) >= (2)

    def test_near_blacks_are_dropped(self) -> None:
        """Near blacks are dropped."""
        palette = build_palette({"primary": {"red": 4, "green": 2, "blue": 3}, "accent": "#ff8800"})
        assert ((4, 2, 3)) not in (palette)
        assert any(max(rgb) > 100 for rgb in palette)

    def test_all_near_black_falls_back_to_whites(self) -> None:
        """All near black falls back to whites."""
        palette = build_palette({"primary": (1, 1, 1), "accent": (8, 8, 8)})
        assert (len(palette)) == (2)
        assert all(max(rgb) > 150 for rgb in palette)

    def test_monochrome_falls_back_to_warm_cool_whites(self) -> None:
        """Monochrome falls back to warm cool whites."""
        palette = build_palette({"primary": (240, 240, 240), "accent": (200, 200, 200)})
        assert (len(palette)) == (2)
        assert (palette[0]) != (palette[1])

    def test_near_duplicates_are_culled(self) -> None:
        """Near duplicates are culled."""
        palette = build_palette(
            {
                "primary": (250, 40, 30),
                "accent": (248, 42, 31),  # near-duplicate of primary
                "on_dark": (30, 60, 240),  # genuinely different
            }
        )
        reds = [rgb for rgb in palette if rgb[0] > 200 and rgb[1] < 80]
        assert (len(reds)) == (1)

    def test_single_hue_synthesizes_gradient(self) -> None:
        """Single hue synthesizes gradient."""
        palette = build_palette({"primary": (200, 30, 30)})
        assert (len(palette)) >= (2)
        peaks = [max(rgb) for rgb in palette]
        assert (peaks) == (sorted(peaks))  # gradient is ascending

    def test_saturation_is_boosted(self) -> None:
        # A muted blue should come back visibly more saturated.
        """Saturation is boosted."""
        muted = (120, 130, 160)
        palette = build_palette({"primary": muted, "accent": (220, 60, 40)})
        boosted = palette[0]
        assert (max(boosted) - min(boosted)) > (max(muted) - min(muted))

    def test_background_fields_are_included(self) -> None:
        """Background fields are included."""
        palette = build_palette(
            {
                "primary": (255, 0, 0),
                "background_dark": (0, 0, 220),
                "background_light": (0, 200, 80),
            }
        )
        assert (len(palette)) >= (3)

    def test_hex_and_sequence_values(self) -> None:
        """Hex and sequence values."""
        palette = build_palette({"primary": "#00cc44", "accent": [220, 40, 90]})
        assert (len(palette)) == (2)
