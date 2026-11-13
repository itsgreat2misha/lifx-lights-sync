"""Unit tests for ha_via_mass: HA discovery via MA's hass provider."""

from __future__ import annotations

from typing import Any, NamedTuple

import pytest

from lifx_lights_sync.discovery import HaDiscoveryError
from lifx_lights_sync.ha_via_mass import fetch_ha_via_hass_provider


# The hass provider's entity registry uses exactly this abbreviated
# NamedTuple shape (platform/device_id/area_id), keyed by entity_id.
class HassRegistryEntity(NamedTuple):
    """Abbreviated hass entity-registry row, as returned by the provider."""

    platform: str
    device_id: str
    area_id: str | None


DEVICE_ID = "2b09adf9fb01b1dd6e4a67bafd9e8466"
SERIAL = "d073d500000a"

DEVICES = {
    DEVICE_ID: {
        "id": DEVICE_ID,
        "manufacturer": "LIFX",
        "identifiers": [["lifx", SERIAL]],
        "connections": [["mac", "d0:73:d5:00:00:0a"]],
        "name": "Kitchen Ceiling",
        "name_by_user": None,
        "area_id": "kitchen",
    },
    "other-device": {
        "id": "other-device",
        "manufacturer": "Signify",
        "identifiers": [["hue", "abc"]],
        "connections": [],
        "name": "Hue bulb",
        "name_by_user": None,
        "area_id": "kitchen",
    },
}
ENTITIES = {
    "light.kitchen_ceiling": HassRegistryEntity(platform="lifx", device_id=DEVICE_ID, area_id=None),
    "sensor.kitchen_temperature": HassRegistryEntity(
        platform="lifx", device_id=DEVICE_ID, area_id=None
    ),
}
AREAS = {"kitchen": {"area_id": "kitchen", "name": "Kitchen", "aliases": []}}


class FakeHassClient:
    """
    Duck-typed stand-in for hass_client's HomeAssistantClient.

    The via-MA path must only use the provider's public registry
    methods, so ``send_command`` records any call and raises: a raw
    WebSocket command from this module is a regression.
    """

    connected = True

    def __init__(self) -> None:
        """Initialise the fake with an empty command log."""
        self.commands: list[str] = []

    async def send_command(self, command: str, **kwargs: Any) -> None:
        """Record a raw command and fail: the via-MA path must never send one."""
        self.commands.append(command)
        raise AssertionError(f"via-MA discovery must not send raw commands: {command}")


class FakeHassProvider:
    """Duck-typed stand-in for the hass provider instance."""

    available = True

    def __init__(self, client: FakeHassClient | None = None, fail_registries: bool = False) -> None:
        """Initialise the fake provider."""
        self.hass = client if client is not None else FakeHassClient()
        self._fail_registries = fail_registries

    async def get_device_registry(self) -> Any:
        """Return the fake device registry."""
        if self._fail_registries:
            raise RuntimeError("registry fetch failed")
        return DEVICES

    async def get_entity_registry(self) -> Any:
        """Return the fake entity registry."""
        return ENTITIES

    async def get_area_registry(self) -> Any:
        """Return the fake area registry."""
        return AREAS


class TestFetchViaHassProvider:
    """Tests for Fetch Via Hass Provider."""

    async def test_lifx_light_from_registries_without_config_entries(self) -> None:
        """Lifx light from registries without config entries."""
        provider = FakeHassProvider()
        lights = await fetch_ha_via_hass_provider(provider)
        assert (len(lights)) == (1)
        light = lights[0]
        assert (light.serial) == (SERIAL)
        # No host on this path: HA entry hosts are not exposed through
        # the hass provider's public methods, so IPs come from LAN /
        # sweep at merge time. And no raw commands may be sent at all.
        assert (light.host) is None
        assert (light.area_name) == ("Kitchen")
        assert (light.entity_ids) == (("light.kitchen_ceiling",))
        assert (provider.hass.commands) == ([])

    async def test_non_lifx_devices_filtered(self) -> None:
        """Non lifx devices filtered."""
        lights = await fetch_ha_via_hass_provider(FakeHassProvider())
        serials = [light.serial for light in lights]
        assert (serials) == ([SERIAL])  # Hue device excluded

    async def test_registry_failure_raises_discovery_error(self) -> None:
        """Registry failure raises discovery error."""
        provider = FakeHassProvider(fail_registries=True)
        with pytest.raises(HaDiscoveryError):
            await fetch_ha_via_hass_provider(provider)

    async def test_none_provider_raises_discovery_error(self) -> None:
        """None provider raises discovery error."""
        with pytest.raises(HaDiscoveryError):
            await fetch_ha_via_hass_provider(None)

    async def test_unavailable_provider_raises_discovery_error(self) -> None:
        """Unavailable provider raises discovery error."""
        provider = FakeHassProvider()
        provider.available = False
        with pytest.raises(HaDiscoveryError):
            await fetch_ha_via_hass_provider(provider)

    async def test_disconnected_client_raises_discovery_error(self) -> None:
        """Disconnected client raises discovery error."""
        client = FakeHassClient()
        client.connected = False
        provider = FakeHassProvider(client=client)
        with pytest.raises(HaDiscoveryError):
            await fetch_ha_via_hass_provider(provider)

    async def test_dict_rows_and_list_registries_accepted(self) -> None:
        """Adapter also accepts dict-shaped entities and list registries."""

        class DictProvider(FakeHassProvider):
            """Fake provider returning dict-shaped entities and list registries."""

            async def get_device_registry(self) -> Any:
                """Return the fake device registry."""
                return list(DEVICES.values())

            async def get_entity_registry(self) -> Any:
                """Return the fake entity registry."""
                return {
                    "light.kitchen_ceiling": {
                        "entity_id": "light.kitchen_ceiling",
                        "device_id": DEVICE_ID,
                        "area_id": None,
                        "platform": "lifx",
                    }
                }

            async def get_area_registry(self) -> Any:
                """Return the fake area registry."""
                return [{"area_id": "kitchen", "name": "Kitchen"}]

        lights = await fetch_ha_via_hass_provider(DictProvider())
        assert (len(lights)) == (1)
        assert (lights[0].host) is None
