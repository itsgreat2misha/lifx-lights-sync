"""
LIFX Lights Sync provider for Music Assistant.

Discovers LIFX lights and creates virtual Sendspin LIGHT players for
each sync target (one per Home Assistant area, plus one per matrix /
ceiling light). When music plays to a virtual player, the lights in
that target react to the music.

Modeled on Music Assistant's Hue Lights Sync provider
(``music_assistant.providers.hue_entertainment.provider``): provider
shape, config-entry style and the ``update_config`` live-apply pattern
follow it directly, using the current Music Assistant APIs
(music_assistant_models imports, ``ConfigEntryType``,
``PluginProvider`` from ``music_assistant.models.plugin``).

Discovery order for source ``auto`` (the default):
1. Music Assistant's own Home Assistant provider (``hass``), if a
   loaded/available instance exists — it already holds an
   authenticated HA connection, so this plugin needs no HA credentials
   of its own in the common case (see ``ha_via_mass``);
2. a direct Home Assistant URL + long-lived token, if configured;
3. LAN discovery (lifx-async), which also runs in the HA modes as a
   gap-filler for lights without an HA host and as the source of
   matrix geometry.

The direct HA credentials are collected by the setup flow
(``setup_flow.py``) and stored as the instance's ``setup_data``,
following the pattern of Music Assistant's own ``hass`` provider;
they are read back via ``get_setup_value`` in :meth:`_resolved_settings`.
Installs that predate the setup flow keep working: ``get_setup_value``
itself falls back to the stored config values on current servers, and
on older servers without setup-flow support the provider reads those
same config values directly.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from music_assistant.models.plugin import PluginProvider
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType

from . import constants as const
from .bridge import LifxBridgeManager
from .discovery import (
    HaDiscoveryError,
    HaLifxLight,
    apply_sweep_results,
    discover_lan,
    fetch_ha_lifx_lights,
    merge_targets,
    sweep_for_serials,
)
from .ha_via_mass import fetch_ha_via_hass_provider
from .lifx_output import open_devices
from .settings import PluginSettings, resolve_ha_credentials

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.provider import ProviderManifest

_LOGGER = logging.getLogger(__name__)

# Config keys that can be applied to running bridges without a reload.
# brightness_floor reaches the analyzer via bridge.update_settings;
# debug_frame_log is read from the provider's settings object on every
# render tick, so refreshing self._settings (done in update_config
# before the bridges are updated) is enough for both.
_LIVE_SETTING_KEYS = (
    const.CONF_BRIGHTNESS,
    const.CONF_BRIGHTNESS_FLOOR,
    const.CONF_COLOR_MODE,
    const.CONF_LATENCY_MS,
    const.CONF_SEND_RATE_HZ,
    const.CONF_DEBUG_FRAME_LOG,
)


class LifxLightsSyncProvider(PluginProvider):
    """Provider that syncs LIFX lights to music via Sendspin."""

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[ProviderFeature],
    ) -> None:
        """Initialize the provider."""
        super().__init__(mass, manifest, config, supported_features)
        self._bridge_manager: LifxBridgeManager | None = None
        self._devices: dict[str, Any] = {}
        self._settings = self._resolved_settings(config)
        self._rediscovery_task: asyncio.Task[None] | None = None

    @property
    def settings(self) -> PluginSettings:
        """Return the current resolved plugin settings."""
        return self._settings

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """
        Return the (options) config entries for the LIFX Lights Sync provider.

        These are runtime options only. The direct Home Assistant
        credentials are not options: they are collected by the setup
        flow (see ``setup_flow.py``), like in Music Assistant's own
        ``hass`` provider. When Music Assistant's Home Assistant
        provider is configured, no HA credentials are needed at all.
        """
        return (
            ConfigEntry(
                key=const.CONF_DISCOVERY_SOURCE,
                type=ConfigEntryType.STRING,
                default_value=const.DEFAULT_DISCOVERY_SOURCE,
                options=[
                    ConfigValueOption(const.SOURCE_AUTO, "Auto"),
                    ConfigValueOption(
                        const.SOURCE_HASS, "Music Assistant's Home Assistant provider"
                    ),
                    ConfigValueOption(const.SOURCE_HA, "Home Assistant (direct URL + token)"),
                    ConfigValueOption(const.SOURCE_LAN, "LAN only"),
                ],
                category="settings",
            ),
            ConfigEntry(
                key=const.CONF_BRIGHTNESS,
                type=ConfigEntryType.INTEGER,
                default_value=const.DEFAULT_BRIGHTNESS,
                range=(const.MIN_BRIGHTNESS, const.MAX_BRIGHTNESS),
                category="settings",
            ),
            ConfigEntry(
                key=const.CONF_BRIGHTNESS_FLOOR,
                type=ConfigEntryType.INTEGER,
                default_value=const.DEFAULT_BRIGHTNESS_FLOOR,
                range=(const.MIN_BRIGHTNESS_FLOOR, const.MAX_BRIGHTNESS_FLOOR),
                required=False,
                immediate_apply=True,
                category="settings",
            ),
            ConfigEntry(
                key=const.CONF_COLOR_MODE,
                type=ConfigEntryType.STRING,
                default_value=const.DEFAULT_COLOR_MODE,
                options=[
                    ConfigValueOption(mode, title=mode.capitalize()) for mode in const.COLOR_MODES
                ],
                category="settings",
            ),
            ConfigEntry(
                key=const.CONF_LATENCY_MS,
                type=ConfigEntryType.INTEGER,
                default_value=const.DEFAULT_LATENCY_MS,
                range=(const.MIN_LATENCY_MS, const.MAX_LATENCY_MS),
                immediate_apply=True,
                category="settings",
            ),
            ConfigEntry(
                key=const.CONF_SEND_RATE_HZ,
                type=ConfigEntryType.INTEGER,
                default_value=const.DEFAULT_SEND_RATE_HZ,
                range=(const.MIN_SEND_RATE_HZ, const.MAX_SEND_RATE_HZ),
                immediate_apply=True,
                category="settings",
            ),
            ConfigEntry(
                key=const.CONF_DEBUG_FRAME_LOG,
                type=ConfigEntryType.BOOLEAN,
                default_value=const.DEFAULT_DEBUG_FRAME_LOG,
                required=False,
                immediate_apply=True,
                category="settings",
            ),
        )

    async def loaded_in_mass(self) -> None:
        """Discover lights and set up sync-target bridges."""
        self._settings = self._resolved_settings(self.config)
        ha_backed = await self._discover_and_setup()
        if not ha_backed and self._settings.discovery_source in (
            const.SOURCE_AUTO,
            const.SOURCE_HASS,
        ):
            # MA's hass provider may simply not have been loaded yet
            # when this plugin set up (provider load order is not
            # guaranteed); retry once it appears instead of staying
            # LAN-only until the next restart.
            self._rediscovery_task = self.mass.create_task(self._delayed_rediscovery())

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        if self._rediscovery_task and not self._rediscovery_task.done():
            self._rediscovery_task.cancel()
            self._rediscovery_task = None
        if self._bridge_manager:
            await self._bridge_manager.stop_all()
            self._bridge_manager = None
        for device in self._devices.values():
            try:
                await device.close()
            except Exception:
                self.logger.debug("Error closing LIFX device", exc_info=True)
        self._devices = {}

    async def update_config(self, config: ProviderConfig, changed_keys: set[str]) -> None:
        """
        Handle config changes.

        Playback settings (brightness, brightness floor, color mode,
        latency, send rate, debug frame log) are applied to the
        running bridges in place; anything else (discovery source, HA
        connection, log level) goes through the base implementation,
        which reloads the provider.
        """
        # changed_keys arrive namespaced as 'values/<key>'; only skip
        # the reload when every changed key can be applied in place.
        settings_keys = {f"values/{key}" for key in _LIVE_SETTING_KEYS}
        if changed_keys and changed_keys <= settings_keys and self._bridge_manager:
            self._settings = self._resolved_settings(config)
            self._bridge_manager.update_settings(self._settings)
            self.config = config
            return

        await super().update_config(config, changed_keys)

    def _resolved_settings(self, config: ProviderConfig) -> PluginSettings:
        """
        Build settings from a config, overlaying setup-flow credentials.

        The direct Home Assistant URL/token are collected by the setup
        flow and stored as ``setup_data``; on servers that support it
        they are read via ``get_setup_value`` (which itself falls back
        to the stored config values, so installs that entered the
        credentials in the old options form keep working untouched).
        On older servers without ``get_setup_value``, the config values
        are used as-is.
        """
        settings = PluginSettings.from_config(config)
        get_setup_value = getattr(self, "get_setup_value", None)
        if not callable(get_setup_value):
            return settings
        return resolve_ha_credentials(
            settings,
            get_setup_value(const.CONF_HA_URL, None),
            get_setup_value(const.CONF_HA_TOKEN, None),
        )

    async def _delayed_rediscovery(self) -> None:
        """Re-run discovery once MA's hass provider is available."""
        await asyncio.sleep(45)
        self._rediscovery_task = None
        if self.mass.get_provider("hass") is None:
            self.logger.debug("hass provider still unavailable; keeping current discovery")
            return
        self.logger.info(
            "hass provider is now available; re-running discovery to "
            "upgrade from the LAN-only target set"
        )
        if self._bridge_manager:
            await self._bridge_manager.stop_all()
            self._bridge_manager = None
        for device in self._devices.values():
            try:
                await device.close()
            except Exception:
                self.logger.debug("Error closing LIFX device", exc_info=True)
        self._devices = {}
        await self._discover_and_setup()

    async def _discover_and_setup(self) -> bool:  # noqa: PLR0915 - one linear orchestration flow
        """Run discovery and bridge setup; True if HA-sourced."""
        settings = self._settings
        source = settings.discovery_source

        ha_lights: list[HaLifxLight] = []
        ha_source_used: str | None = None

        # 1. Music Assistant's own Home Assistant provider (preferred:
        #    it already holds an authenticated HA connection).
        if source in (const.SOURCE_AUTO, const.SOURCE_HASS):
            ha_lights, ha_source_used = await self._discover_via_hass_provider(
                required=source == const.SOURCE_HASS
            )

        # 2. Direct Home Assistant URL + token (only if configured and
        #    step 1 produced nothing).
        if (
            not ha_lights
            and source in (const.SOURCE_AUTO, const.SOURCE_HA)
            and settings.ha_configured
        ):
            try:
                ha_lights = await fetch_ha_lifx_lights(settings.ha_url, settings.ha_token)
                ha_source_used = "direct Home Assistant connection"
                self.logger.info(
                    "Home Assistant reported %d LIFX lights (direct connection)",
                    len(ha_lights),
                )
            except Exception as err:
                if source == const.SOURCE_HA:
                    self.logger.error("Home Assistant discovery failed: %s", err)
                else:
                    self.logger.warning(
                        "Home Assistant discovery failed (%s); falling back to LAN discovery",
                        err,
                    )
        elif source in (const.SOURCE_AUTO, const.SOURCE_HA) and not ha_lights:
            self.logger.debug(
                "No direct Home Assistant URL/token configured; skipping direct HA discovery"
            )

        # 3. LAN discovery: the only source in lan mode; in the HA modes
        #    a gap-filler (lights whose HA entry lacks a host), a
        #    cross-check, and the source of matrix geometry.
        lan_devices = []
        if source not in (const.SOURCE_HA, const.SOURCE_HASS) or ha_lights:
            try:
                lan_devices = await discover_lan()
            except Exception:
                self.logger.warning("LAN discovery failed", exc_info=True)
            self.logger.info("LAN discovery found %d devices", len(lan_devices))

        targets = merge_targets(
            ha_lights,
            lan_devices,
            include_lan_only=source not in (const.SOURCE_HA, const.SOURCE_HASS),
        )
        if not targets:
            self.logger.warning("No LIFX sync targets discovered")
            self.available = False
            return False

        self.logger.info(
            "Discovery complete via %s: %d sync target(s): %s",
            ha_source_used or "LAN only",
            len(targets),
            ", ".join(target.name for target in targets),
        )

        # Unicast sweep: broadcast discovery misses many bulbs across
        # VLANs, and HA's config_entries/get strips entry data, so the
        # host HA stores is not API-readable. Probe the subnets where
        # lights are already known to live for every serial still
        # missing an IP (raw GetService; see discovery.sweep_for_serials).
        missing_serials = {
            light.serial for target in targets for light in target.lights if not light.ip
        }
        if missing_serials:
            seed_ips = [device.ip for device in lan_devices if device.ip]
            seed_ips += [light.ip for target in targets for light in target.lights if light.ip]
            try:
                found = await sweep_for_serials(missing_serials, seed_ips)
            except Exception:
                self.logger.warning("LIFX unicast sweep failed", exc_info=True)
                found = {}
            filled = apply_sweep_results(targets, found)
            self.logger.info(
                "Unicast sweep found %d of %d lights missing an IP",
                filled,
                len(missing_serials),
            )

        # Open typed devices (Light/MatrixLight/CeilingLight per light
        # shape — a base Device has no color methods at all).
        all_lights = [light for target in targets for light in target.lights]
        try:
            self._devices = await open_devices(all_lights)
        except Exception:
            self.logger.warning("Could not open LIFX devices", exc_info=True)
            self._devices = {}

        self._bridge_manager = LifxBridgeManager(self)
        await self._bridge_manager.setup_bridges(targets, self._devices)
        self.available = bool(self._bridge_manager.bridges)
        return ha_source_used is not None

    async def _discover_via_hass_provider(
        self, required: bool
    ) -> tuple[list[HaLifxLight], str | None]:
        """Try discovery through MA's hass provider; never raises."""
        hass_provider: Any = None
        try:
            hass_provider = self.mass.get_provider("hass")
        except Exception:
            hass_provider = None
        if hass_provider is None:
            msg = "Music Assistant's Home Assistant provider is not loaded"
            if required:
                self.logger.error("%s; no lights will be discovered from HA", msg)
            else:
                self.logger.debug("%s; trying the next discovery source", msg)
            return [], None
        try:
            lights = await fetch_ha_via_hass_provider(hass_provider)
        except HaDiscoveryError as err:
            if required:
                self.logger.error("HA discovery via the hass provider failed: %s", err)
            else:
                self.logger.warning(
                    "HA discovery via the hass provider failed (%s); "
                    "trying the next discovery source",
                    err,
                )
            return [], None
        self.logger.info(
            "Home Assistant reported %d LIFX lights (via MA's hass provider)",
            len(lights),
        )
        return lights, "Music Assistant's Home Assistant provider"


__all__ = ["LifxLightsSyncProvider"]
