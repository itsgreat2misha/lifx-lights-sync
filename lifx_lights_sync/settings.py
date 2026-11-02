"""Typed, defensive readers for the plugin configuration.

Music Assistant hands a provider its config values as a loose mapping.
Every reader here coerces and clamps, and falls back to the documented
default on missing/invalid input, so a bad value can never crash setup.
No Music Assistant imports: this module is unit-testable in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from . import constants as const


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(float(value))  # accept "80" and 80.0 alike
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _as_choice(value: Any, choices: tuple[str, ...], default: str) -> str:
    text = _as_str(value).lower()
    return text if text in choices else default


@dataclass(frozen=True)
class PluginSettings:
    """Fully-resolved plugin settings."""

    discovery_source: str = const.DEFAULT_DISCOVERY_SOURCE
    ha_url: str = const.DEFAULT_HA_URL
    ha_token: str = const.DEFAULT_HA_TOKEN
    brightness_percent: int = const.DEFAULT_BRIGHTNESS
    color_mode: str = const.DEFAULT_COLOR_MODE
    latency_ms: int = const.DEFAULT_LATENCY_MS
    send_rate_hz: int = const.DEFAULT_SEND_RATE_HZ

    @property
    def brightness(self) -> float:
        """Brightness as a 0.0-1.0 scale factor for the analyzer."""
        return self.brightness_percent / 100.0

    @property
    def latency_us(self) -> int:
        """Latency compensation in microseconds (Sendspin clock units)."""
        return self.latency_ms * 1000

    @property
    def ha_configured(self) -> bool:
        """True when both a Home Assistant URL and token are present."""
        return bool(self.ha_url and self.ha_token)

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> "PluginSettings":
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
        )

    @classmethod
    def from_config_getter(cls, getter: Any) -> "PluginSettings":
        """Build settings from a Music Assistant ``config.get_value`` callable.

        ``getter`` is called once per known key; any exception it raises for
        a key is treated as "not set" so a partial config still resolves.
        """
        values: dict[str, Any] = {}
        for key in (
            const.CONF_DISCOVERY_SOURCE,
            const.CONF_HA_URL,
            const.CONF_HA_TOKEN,
            const.CONF_BRIGHTNESS,
            const.CONF_COLOR_MODE,
            const.CONF_LATENCY_MS,
            const.CONF_SEND_RATE_HZ,
        ):
            try:
                values[key] = getter(key)
            except Exception:  # noqa: BLE001 - config access must not crash setup
                values[key] = None
        return cls.from_mapping(values)


__all__ = ["PluginSettings"]
