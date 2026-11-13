"""
Readers for the LIFX Lights Sync playback/discovery settings.

Mirrors the Hue Lights Sync ``settings.py`` in Music Assistant:
each reader takes the provider's ``ProviderConfig`` and normalizes
the raw value returned
by ``config.get_value(key)`` (stored values can come back as str/float,
so numbers go through ``int(float(str(value)))``).

On top of the Hue-style readers, :class:`PluginSettings` bundles all
values into one immutable object for the bridge/provider plumbing. Its
builder methods never raise: missing/invalid input falls back to the
documented defaults, so a bad value can never crash setup. This module
has no Music Assistant imports and is unit-testable in isolation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from . import constants as const

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(float(str(value)))  # accept "80" and 80.0 alike
    except TypeError, ValueError:
        return default
    return max(minimum, min(maximum, number))


def _as_choice(value: Any, choices: tuple[str, ...], default: str) -> str:
    text = _as_str(value).lower()
    return text if text in choices else default


def validate_ha_credential_pair(ha_url: str, ha_token: str) -> str | None:
    """
    Validate that a direct Home Assistant URL/token pair is complete.

    Both empty is valid (no direct connection configured); exactly one
    of the two is not. Returns a strings.json error slug for the
    incomplete case, or None when the pair is usable. Kept pure (no
    Music Assistant imports) so the setup flow and the tests can both
    use it.
    """
    if ha_token and not ha_url:
        return "ha_url_required"
    if ha_url and not ha_token:
        return "ha_token_required"
    return None


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


# -- Hue-style readers (take the ProviderConfig) ---------------------------


def get_discovery_source(config: ProviderConfig) -> str:
    """Return the configured discovery source (auto/hass/ha/lan)."""
    return _as_choice(
        config.get_value(const.CONF_DISCOVERY_SOURCE),
        const.DISCOVERY_SOURCES,
        const.DEFAULT_DISCOVERY_SOURCE,
    )


def get_ha_url(config: ProviderConfig) -> str:
    """Return the configured direct Home Assistant base URL (may be empty)."""
    return _as_str(config.get_value(const.CONF_HA_URL)).rstrip("/")


def get_ha_token(config: ProviderConfig) -> str:
    """Return the configured direct Home Assistant token (may be empty)."""
    return _as_str(config.get_value(const.CONF_HA_TOKEN))


def get_brightness(config: ProviderConfig) -> int:
    """Return the configured brightness percentage."""
    return _as_int(
        config.get_value(const.CONF_BRIGHTNESS),
        const.DEFAULT_BRIGHTNESS,
        const.MIN_BRIGHTNESS,
        const.MAX_BRIGHTNESS,
    )


def get_brightness_floor(config: ProviderConfig) -> int:
    """Return the configured brightness floor percentage (0 = per-mode)."""
    return _as_int(
        config.get_value(const.CONF_BRIGHTNESS_FLOOR),
        const.DEFAULT_BRIGHTNESS_FLOOR,
        const.MIN_BRIGHTNESS_FLOOR,
        const.MAX_BRIGHTNESS_FLOOR,
    )


def get_color_mode(config: ProviderConfig) -> str:
    """Return the configured visualization mode."""
    return _as_choice(
        config.get_value(const.CONF_COLOR_MODE),
        const.COLOR_MODES,
        const.DEFAULT_COLOR_MODE,
    )


def get_latency_ms(config: ProviderConfig) -> int:
    """Return the configured latency compensation in milliseconds."""
    return _as_int(
        config.get_value(const.CONF_LATENCY_MS),
        const.DEFAULT_LATENCY_MS,
        const.MIN_LATENCY_MS,
        const.MAX_LATENCY_MS,
    )


def get_send_rate_hz(config: ProviderConfig) -> int:
    """Return the configured per-light send rate cap (sends/second)."""
    return _as_int(
        config.get_value(const.CONF_SEND_RATE_HZ),
        const.DEFAULT_SEND_RATE_HZ,
        const.MIN_SEND_RATE_HZ,
        const.MAX_SEND_RATE_HZ,
    )


def get_debug_frame_log(config: ProviderConfig) -> bool:
    """Return True when per-second commanded-brightness logging is on."""
    return _as_bool(
        config.get_value(const.CONF_DEBUG_FRAME_LOG),
        const.DEFAULT_DEBUG_FRAME_LOG,
    )


def resolve_ha_credentials(
    base: PluginSettings, setup_url: Any, setup_token: Any
) -> PluginSettings:
    """
    Overlay setup-flow credentials onto config-derived settings.

    ``setup_url``/``setup_token`` are the values from the provider's
    ``setup_data`` (via ``get_setup_value``); a value that is not None
    wins over the config-derived one, including an empty string (the
    user cleared the credential in the setup flow). None means the
    setup flow never ran, so the legacy config value - entered in the
    options form by installs that predate the setup flow - is kept.
    """
    if setup_url is None and setup_token is None:
        return base
    ha_url = base.ha_url
    if setup_url is not None:
        # An explicit empty string clears the credential; it must not
        # fall back to the config value the way a missing value does.
        ha_url = _as_str(setup_url).rstrip("/")
    ha_token = base.ha_token
    if setup_token is not None:
        ha_token = _as_str(setup_token)
    return replace(base, ha_url=ha_url, ha_token=ha_token)


# -- Bundled settings object -------------------------------------------------


@dataclass(frozen=True)
class PluginSettings:
    """Fully-resolved plugin settings."""

    discovery_source: str = const.DEFAULT_DISCOVERY_SOURCE
    ha_url: str = const.DEFAULT_HA_URL
    ha_token: str = const.DEFAULT_HA_TOKEN
    brightness_percent: int = const.DEFAULT_BRIGHTNESS
    brightness_floor_percent: int = const.DEFAULT_BRIGHTNESS_FLOOR
    color_mode: str = const.DEFAULT_COLOR_MODE
    latency_ms: int = const.DEFAULT_LATENCY_MS
    send_rate_hz: int = const.DEFAULT_SEND_RATE_HZ
    debug_frame_log: bool = const.DEFAULT_DEBUG_FRAME_LOG

    @property
    def brightness(self) -> float:
        """Brightness as a 0.0-1.0 scale factor for the analyzer."""
        return self.brightness_percent / 100.0

    @property
    def brightness_floor(self) -> float:
        """Resting brightness floor as a 0.0-1.0 factor (0 = per-mode)."""
        return self.brightness_floor_percent / 100.0

    @property
    def latency_us(self) -> int:
        """Latency compensation in microseconds (Sendspin clock units)."""
        return self.latency_ms * 1000

    @property
    def ha_configured(self) -> bool:
        """True when both a direct Home Assistant URL and token are present."""
        return bool(self.ha_url and self.ha_token)

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> PluginSettings:
        """Build settings from a raw config mapping (never raises)."""
        values = values or {}
        return cls(
            discovery_source=_as_choice(
                values.get(const.CONF_DISCOVERY_SOURCE),
                const.DISCOVERY_SOURCES,
                const.DEFAULT_DISCOVERY_SOURCE,
            ),
            ha_url=_as_str(values.get(const.CONF_HA_URL)).rstrip("/"),
            ha_token=_as_str(values.get(const.CONF_HA_TOKEN)),
            brightness_percent=_as_int(
                values.get(const.CONF_BRIGHTNESS),
                const.DEFAULT_BRIGHTNESS,
                const.MIN_BRIGHTNESS,
                const.MAX_BRIGHTNESS,
            ),
            brightness_floor_percent=_as_int(
                values.get(const.CONF_BRIGHTNESS_FLOOR),
                const.DEFAULT_BRIGHTNESS_FLOOR,
                const.MIN_BRIGHTNESS_FLOOR,
                const.MAX_BRIGHTNESS_FLOOR,
            ),
            color_mode=_as_choice(
                values.get(const.CONF_COLOR_MODE),
                const.COLOR_MODES,
                const.DEFAULT_COLOR_MODE,
            ),
            latency_ms=_as_int(
                values.get(const.CONF_LATENCY_MS),
                const.DEFAULT_LATENCY_MS,
                const.MIN_LATENCY_MS,
                const.MAX_LATENCY_MS,
            ),
            send_rate_hz=_as_int(
                values.get(const.CONF_SEND_RATE_HZ),
                const.DEFAULT_SEND_RATE_HZ,
                const.MIN_SEND_RATE_HZ,
                const.MAX_SEND_RATE_HZ,
            ),
            debug_frame_log=_as_bool(
                values.get(const.CONF_DEBUG_FRAME_LOG),
                const.DEFAULT_DEBUG_FRAME_LOG,
            ),
        )

    @classmethod
    def from_config(cls, config: ProviderConfig) -> PluginSettings:
        """
        Build settings from a Music Assistant ``ProviderConfig``.

        Uses the Hue-style readers above (``config.get_value`` per key).
        """
        return cls(
            discovery_source=get_discovery_source(config),
            ha_url=get_ha_url(config),
            ha_token=get_ha_token(config),
            brightness_percent=get_brightness(config),
            brightness_floor_percent=get_brightness_floor(config),
            color_mode=get_color_mode(config),
            latency_ms=get_latency_ms(config),
            send_rate_hz=get_send_rate_hz(config),
            debug_frame_log=get_debug_frame_log(config),
        )


__all__ = [
    "PluginSettings",
    "get_brightness",
    "get_brightness_floor",
    "get_color_mode",
    "get_debug_frame_log",
    "get_discovery_source",
    "get_ha_token",
    "get_ha_url",
    "get_latency_ms",
    "get_send_rate_hz",
    "resolve_ha_credentials",
    "validate_ha_credential_pair",
]
