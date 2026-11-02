"""Music Assistant provider for LIFX Lights Sync.

Modeled on the Hue Lights Sync provider
(``music-assistant/server`` providers/hue_entertainment/provider.py
@73c3e59): a PluginProvider that owns a bridge manager, exposes its
configuration through ``get_config_entries``, and live-applies safe
settings in ``update_config``.

Lifecycle:
- ``loaded_in_mass()`` runs discovery (Home Assistant first in ``auto``
  mode, LAN fallback / IP resolution), builds sync targets and hands
  them to the bridge manager, which registers one virtual LIGHT player
  per target with the Sendspin provider;
- ``update_config()`` re-reads the config and pushes brightness, color
  mode, latency and send rate into the running bridges;
- ``unload()`` stops all bridges (restoring light state) and closes
  the LIFX device connections.

The Music Assistant imports below follow the Hue provider; individual
names are marked ``# TODO(live-verify)`` where the exact module path
could not be confirmed offline.
"""

from __future__ import annotations

import logging
from typing import Any

# TODO(live-verify): these module paths mirror hue_entertainment
# @73c3e59 / current music-assistant/server; confirm on first live load.
# Fallback chains are kept so a moved module degrades to a clear import
# error naming the last attempted path, not a silent wrong class.
try:
    from music_assistant.common.models.config_entries import (  # type: ignore[import-not-found]
        ConfigEntry,
        ConfigValueOption,
        ConfigValueType,
    )
except ImportError:  # older layout
    from music_assistant.models.config_entries import (  # type: ignore[import-not-found]
        ConfigEntry,
        ConfigValueOption,
        ConfigValueType,
    )

try:
    from music_assistant.common.models.provider import (  # type: ignore[import-not-found]
        PluginProvider,
    )
except ImportError:
    try:
        from music_assistant.server.models.provider import (  # type: ignore[import-not-found]
            PluginProvider,
        )
    except ImportError:
        from music_assistant.models.provider import (  # type: ignore[import-not-found]
            PluginProvider,
        )

from . import constants as const
from .bridge import LifxBridgeManager
from .discovery import (
    HaLifxLight,
    discover_lan,
    fetch_ha_lifx_lights,
    merge_targets,
)
from .lifx_output import open_devices
from .settings import PluginSettings

_LOGGER = logging.getLogger(__name__)


def _secret_config_value_type() -> Any:
    """Return the password-style config type if MA supports it."""
    # ConfigValueType.SECRET_STRING is the secret type used by other MA
    # providers for tokens/passwords; fall back to a plain string entry.
    return getattr(ConfigValueType, "SECRET_STRING", ConfigValueType.STRING)


class LifxLightsSyncProvider(PluginProvider):
    """Plugin provider syncing LIFX lights to the Sendspin visualizer."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._settings = PluginSettings()
        self._manager: LifxBridgeManager | None = None
        self._devices: dict[str, Any] = {}

    # -- configuration -------------------------------------------------------

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, Any] | None = None,
    ) -> tuple[ConfigEntry, ...]:
        """Return the plugin's config entries (mirrors the Hue plugin)."""
        return (
            ConfigEntry(
                key=const.CONF_DISCOVERY_SOURCE,
                type=ConfigValueType.STRING,
                label="Discovery source",
                description=(
                    "Where to find your LIFX lights. 'auto' asks Home "
                    "Assistant first (names and areas) and uses the LAN "
                    "to find IP addresses; 'ha' uses only lights known "
                    "to Home Assistant; 'lan' ignores Home Assistant."
                ),
                default_value=const.DEFAULT_DISCOVERY_SOURCE,
                options=[
                    ConfigValueOption("Auto (Home Assistant first)", const.SOURCE_AUTO),
                    ConfigValueOption("Home Assistant only", const.SOURCE_HA),
                    ConfigValueOption("LAN only", const.SOURCE_LAN),
                ],
                required=True,
            ),
            ConfigEntry(
                key=const.CONF_HA_URL,
                type=ConfigValueType.STRING,
                label="Home Assistant URL",
                description=(
                    "Base URL of your Home Assistant instance, "
                    "e.g. http://homeassistant.local:8123"
                ),
                default_value=const.DEFAULT_HA_URL,
                depends_on=const.CONF_DISCOVERY_SOURCE,
                depends_on_value=const.SOURCE_AUTO,
            ),
            ConfigEntry(
                key=const.CONF_HA_TOKEN,
                type=_secret_config_value_type(),
                label="Home Assistant long-lived access token",
                description=(
                    "Created in Home Assistant under your profile -> "
                    "Security -> Long-lived access tokens. Used read-only "
                    "to list LIFX devices, entities and areas."
                ),
                default_value=const.DEFAULT_HA_TOKEN,
            ),
            ConfigEntry(
                key=const.CONF_BRIGHTNESS,
                type=ConfigValueType.INTEGER,
                label="Brightness (%)",
                description="Maximum brightness of the sync effect, in percent.",
                default_value=const.DEFAULT_BRIGHTNESS,
                range=(const.MIN_BRIGHTNESS, const.MAX_BRIGHTNESS),
            ),
            ConfigEntry(
                key=const.CONF_COLOR_MODE,
                type=ConfigValueType.STRING,
                label="Color mode",
                description="Visualizer style applied to the lights.",
                default_value=const.DEFAULT_COLOR_MODE,
                options=[
                    ConfigValueOption("Smooth", const.MODE_SMOOTH),
                    ConfigValueOption("Ambient", const.MODE_AMBIENT),
                    ConfigValueOption("Flashing", const.MODE_FLASHING),
                    ConfigValueOption("Energetic", const.MODE_ENERGETIC),
                ],
                required=True,
            ),
            ConfigEntry(
                key=const.CONF_LATENCY_MS,
                type=ConfigValueType.INTEGER,
                label="Latency compensation (ms)",
                description=(
                    "Shift the visualizer render time to compensate for "
                    "speaker/network latency."
                ),
                default_value=const.DEFAULT_LATENCY_MS,
                range=(const.MIN_LATENCY_MS, const.MAX_LATENCY_MS),
            ),
            ConfigEntry(
                key=const.CONF_SEND_RATE_HZ,
                type=ConfigValueType.INTEGER,
                label="Max sends per second (per light)",
                description=(
                    "Rate limit for color updates sent to each LIFX light."
                ),
                default_value=const.DEFAULT_SEND_RATE_HZ,
                range=(const.MIN_SEND_RATE_HZ, const.MAX_SEND_RATE_HZ),
            ),
        )

    def _read_settings(self) -> PluginSettings:
        config = getattr(self, "config", None)
        getter = getattr(config, "get_value", None)
        if callable(getter):
            return PluginSettings.from_config_getter(getter)
        # TODO(live-verify): Provider.config.get_value is the accessor in
        # current MA; this fallback covers a plain mapping config.
        if isinstance(config, dict):
            return PluginSettings.from_mapping(config)
        return PluginSettings()

    # -- lifecycle -------------------------------------------------------------

    async def loaded_in_mass(self) -> None:
        """Discover lights and register all sync targets."""
        self._settings = self._read_settings()
        settings = self._settings

        ha_lights: list[HaLifxLight] = []
        want_ha = settings.discovery_source in (const.SOURCE_AUTO, const.SOURCE_HA)
        if want_ha and settings.ha_configured:
            try:
                ha_lights = await fetch_ha_lifx_lights(
                    settings.ha_url, settings.ha_token
                )
                _LOGGER.info(
                    "LIFX Lights Sync: Home Assistant reported %d LIFX lights",
                    len(ha_lights),
                )
            except Exception as err:  # noqa: BLE001
                if settings.discovery_source == const.SOURCE_HA:
                    _LOGGER.error(
                        "LIFX Lights Sync: Home Assistant discovery failed: %s", err
                    )
                else:
                    _LOGGER.warning(
                        "LIFX Lights Sync: Home Assistant discovery failed "
                        "(%s); falling back to LAN discovery",
                        err,
                    )
        elif want_ha:
            _LOGGER.info(
                "LIFX Lights Sync: no Home Assistant URL/token configured; "
                "using LAN discovery"
            )

        # LAN discovery in HA modes is a gap-filler / cross-check, not
        # the primary resolver: HA config entries already provided a
        # host for most lights (see discovery.fetch_ha_lifx_lights).
        # The LAN sweep fills in lights whose HA entry lacks a host and
        # supplies matrix geometry. In lan/auto-without-HA modes it is
        # the only source.
        lan_devices = []
        if settings.discovery_source != const.SOURCE_HA or ha_lights:
            try:
                lan_devices = await discover_lan()
            except Exception:  # noqa: BLE001
                _LOGGER.warning(
                    "LIFX Lights Sync: LAN discovery failed", exc_info=True
                )
        _LOGGER.info(
            "LIFX Lights Sync: LAN discovery found %d devices", len(lan_devices)
        )

        targets = merge_targets(
            ha_lights,
            lan_devices,
            include_lan_only=settings.discovery_source != const.SOURCE_HA,
        )
        if not targets:
            _LOGGER.warning("LIFX Lights Sync: no sync targets discovered")
            return

        # Resolved IPs already encode the precedence (HA host > LAN).
        ip_by_serial = {
            light.serial: light.ip
            for target in targets
            for light in target.lights
            if light.ip
        }
        try:
            self._devices = await open_devices(ip_by_serial)
        except Exception:  # noqa: BLE001
            _LOGGER.warning(
                "LIFX Lights Sync: could not open LIFX devices", exc_info=True
            )
            self._devices = {}

        self._manager = LifxBridgeManager(self.mass, settings)
        bridges = await self._manager.setup(targets, self._devices)
        _LOGGER.info(
            "LIFX Lights Sync: registered %d sync targets: %s",
            len(bridges),
            ", ".join(bridge.target.name for bridge in bridges),
        )

    async def update_config(
        self, config: dict[str, Any] | None = None
    ) -> None:
        """Live-apply updated config values to the running bridges."""
        # TODO(live-verify): update_config's exact signature in current
        # MA (called with the new values dict by the config controller);
        # we re-read through the standard accessor when possible.
        if config:
            merged = {
                key: getattr(self._settings, field_name)
                for key, field_name in (
                    (const.CONF_DISCOVERY_SOURCE, "discovery_source"),
                    (const.CONF_HA_URL, "ha_url"),
                    (const.CONF_HA_TOKEN, "ha_token"),
                    (const.CONF_BRIGHTNESS, "brightness_percent"),
                    (const.CONF_COLOR_MODE, "color_mode"),
                    (const.CONF_LATENCY_MS, "latency_ms"),
                    (const.CONF_SEND_RATE_HZ, "send_rate_hz"),
                )
            }
            merged.update(config)
            self._settings = PluginSettings.from_mapping(merged)
        else:
            self._settings = self._read_settings()
        if self._manager is not None:
            self._manager.update_settings(self._settings)

    async def unload(self, is_removed: bool = False) -> None:
        """Stop all bridges (restoring lights) and close devices."""
        if self._manager is not None:
            await self._manager.unload()
            self._manager = None
        for device in self._devices.values():
            try:
                await device.close()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Error closing LIFX device", exc_info=True)
        self._devices = {}


__all__ = ["LifxLightsSyncProvider"]
