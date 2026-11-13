"""
Pure color helpers: RGB <-> HSBK conversion and palette building.

Stdlib only, no Music Assistant / lifx-async imports, so this module is
fully unit-testable. RGB values are ``(red, green, blue)`` tuples of ints
in the 0-255 range throughout this module.
"""

from __future__ import annotations

import colorsys
from collections.abc import Mapping, Sequence
from typing import Any

from .constants import DEFAULT_KELVIN

RGB = tuple[int, int, int]
HSBK = tuple[float, float, float, int]  # hue deg, saturation, brightness, kelvin

# Palette rules (ported from the Hue Lights Sync analyzer's palette logic)
NEAR_BLACK_MAX = 18  # colors with max channel below this are dropped
MIN_SATURATION = 0.55  # surviving colors are boosted to at least this
DUPLICATE_DISTANCE = 26.0  # RGB euclidean distance below which colors merge
MONOCHROME_SATURATION = 0.08  # below this a color counts as "white-ish"

# Warm/cool white fallback used for monochrome album art.
WARM_WHITE: RGB = (255, 214, 170)
COOL_WHITE: RGB = (191, 219, 255)

# Field order for a Music Assistant / Sendspin server color payload.
_PALETTE_FIELDS = ("primary", "accent", "on_dark", "on_light")


def clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    """Clamp a value into the inclusive range [minimum, maximum]."""
    return max(minimum, min(maximum, value))


def rgb_to_hsbk(rgb: RGB, brightness_scale: float = 1.0, kelvin: int = DEFAULT_KELVIN) -> HSBK:
    """
    Convert an RGB tuple to (hue degrees, saturation, brightness, kelvin).

    ``brightness_scale`` multiplies the HSV value so callers can dim a
    color without changing its hue/saturation.
    """
    r, g, b = (channel / 255.0 for channel in rgb)
    hue, saturation, value = colorsys.rgb_to_hsv(r, g, b)
    return (
        round(hue * 360.0, 3),
        round(clamp(saturation), 6),
        round(clamp(value * brightness_scale), 6),
        int(kelvin),
    )


def hsbk_to_rgb(hsbk: HSBK) -> RGB:
    """Convert (hue degrees, saturation, brightness, kelvin) back to RGB."""
    hue, saturation, brightness, _kelvin = hsbk
    r, g, b = colorsys.hsv_to_rgb((hue % 360.0) / 360.0, clamp(saturation), clamp(brightness))
    return (round(r * 255), round(g * 255), round(b * 255))


def scale_rgb(rgb: RGB, factor: float) -> RGB:
    """Multiply every channel by ``factor`` (clamped to 0-255)."""
    factor = max(0.0, factor)
    return tuple(min(255, round(channel * factor)) for channel in rgb)  # type: ignore[return-value]


def rgb_distance(a: RGB, b: RGB) -> float:
    """Euclidean distance between two RGB colors."""
    distance: float = sum((x - y) ** 2 for x, y in zip(a, b, strict=True)) ** 0.5
    return distance


def _coerce_rgb(value: Any) -> RGB | None:
    """
    Best-effort conversion of a server color value to an RGB tuple.

    Accepts dicts with red/green/blue or r/g/b keys, 3-sequences, and
    ``#rrggbb`` / ``rrggbb`` hex strings. Returns None when it cannot be parsed.
    """
    if value is None:
        return None
    if isinstance(value, Mapping):
        keys = ("red", "green", "blue") if "red" in value else ("r", "g", "b")
        try:
            return tuple(round(float(value[key])) for key in keys)  # type: ignore[return-value]
        except KeyError, TypeError, ValueError:
            return None
    if isinstance(value, str):
        text = value.strip().lstrip("#")
        if len(text) == 6:
            try:
                return (
                    int(text[0:2], 16),
                    int(text[2:4], 16),
                    int(text[4:6], 16),
                )
            except ValueError:
                return None
        return None
    if isinstance(value, Sequence) and len(value) == 3:
        try:
            return tuple(round(float(channel)) for channel in value)  # type: ignore[return-value]
        except TypeError, ValueError:
            return None
    return None


def _saturation(rgb: RGB) -> float:
    r, g, b = (channel / 255.0 for channel in rgb)
    return colorsys.rgb_to_hsv(r, g, b)[1]


def _boost_saturation(rgb: RGB) -> RGB:
    """
    Raise a color's saturation to at least MIN_SATURATION.

    Near-white colors (saturation below MONOCHROME_SATURATION) are left
    alone: they carry no hue worth boosting.
    """
    r, g, b = (channel / 255.0 for channel in rgb)
    hue, saturation, value = colorsys.rgb_to_hsv(r, g, b)
    if saturation < MONOCHROME_SATURATION or saturation >= MIN_SATURATION:
        return rgb
    nr, ng, nb = colorsys.hsv_to_rgb(hue, MIN_SATURATION, value)
    return (round(nr * 255), round(ng * 255), round(nb * 255))


def _shade_gradient(rgb: RGB) -> list[RGB]:
    """Synthesize a 3-step shade gradient from a single color."""
    return [scale_rgb(rgb, factor) for factor in (0.45, 0.7, 1.0)]


def default_palette() -> list[RGB]:
    """Fallback palette used before the server sends any album colors."""
    return [(255, 64, 48), (255, 149, 0), (64, 156, 255), (175, 82, 222)]


def build_palette(server_colors: Mapping[str, Any] | None) -> list[RGB]:
    """
    Build an ordered working palette from a server color payload.

    Rules (mirroring the Hue Lights Sync blueprint):
    - take primary, accent, on_dark, on_light, then any ``background_*``
      fields, in that order;
    - drop near-black colors (they render as "off" on a bulb);
    - boost saturation of surviving chromatic colors;
    - cull near-duplicates;
    - if fewer than two distinct hues remain, synthesize a shade gradient;
    - monochrome payloads fall back to a warm/cool white pair.
    Always returns at least two colors.
    """
    if not server_colors:
        return default_palette()

    ordered_keys = [key for key in _PALETTE_FIELDS if key in server_colors]
    ordered_keys += sorted(key for key in server_colors if key.startswith("background_"))

    candidates: list[RGB] = []
    for key in ordered_keys:
        rgb = _coerce_rgb(server_colors.get(key))
        if rgb is None:
            continue
        if max(rgb) < NEAR_BLACK_MAX:
            continue  # near-black renders as off; useless in a palette
        candidates.append(_boost_saturation(rgb))

    unique: list[RGB] = []
    for rgb in candidates:
        if all(rgb_distance(rgb, kept) >= DUPLICATE_DISTANCE for kept in unique):
            unique.append(rgb)

    if not unique:
        return [WARM_WHITE, COOL_WHITE]

    if all(_saturation(rgb) < MONOCHROME_SATURATION for rgb in unique):
        # Album art with no real color: warm/cool whites still give the
        # analyzer something to alternate between on beats.
        return [WARM_WHITE, COOL_WHITE]

    distinct_hues = {round(colorsys.rgb_to_hsv(*(c / 255.0 for c in rgb))[0], 1) for rgb in unique}
    if len(unique) < 2 or len(distinct_hues) < 2:
        return _shade_gradient(unique[0])

    return unique


def palette_color_at(palette: Sequence[RGB], index: int, fraction: float) -> RGB:
    """
    Interpolate between palette[index] and the next palette color.

    ``fraction`` (0.0-1.0) blends toward the following color, wrapping
    around the end of the palette. Used for spatial palette offsets.
    """
    if not palette:
        return (0, 0, 0)
    current = palette[index % len(palette)]
    following = palette[(index + 1) % len(palette)]
    fraction = clamp(fraction)
    return tuple(round(a + (b - a) * fraction) for a, b in zip(current, following, strict=True))  # type: ignore[return-value]


__all__ = [
    "HSBK",
    "RGB",
    "build_palette",
    "clamp",
    "default_palette",
    "hsbk_to_rgb",
    "palette_color_at",
    "rgb_distance",
    "rgb_to_hsbk",
    "scale_rgb",
]
