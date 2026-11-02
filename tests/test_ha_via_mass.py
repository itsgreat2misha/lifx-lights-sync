"""Unit tests for ha_via_mass: HA discovery via MA's hass provider."""

import sys
import unittest
from collections import namedtuple
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lifx_lights_sync.discovery import HaDiscoveryError  # noqa: E402
from lifx_lights_sync.ha_via_mass import fetch_ha_via_hass_provider  # noqa: E402

# The hass provider's entity registry uses exactly this abbreviated
# NamedTuple shape (platform/device_id/area_id), keyed by entity_id.
HassRegistryEntity = namedtuple(
    "HassRegistryEntity", ["platform", "device_id", "area_id"]
)

DEVICE_ID = "2b09adf9fb01b1dd6e4a67bafd9e8466"
SERIAL = "d073d587112b"

DEVICES = {
    DEVICE_ID: {
        "id": DEVICE_ID,
        "manufacturer": "LIFX",
        "identifiers": [["lifx", SERIAL]],
        "connections": [["mac", "d0:73:d5:87:11:2b"]],
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
    "light.kitchen_ceiling": HassRegistryEntity(
        platform="lifx", device_id=DEVICE_ID, area_id=None
    ),
    "sensor.kitchen_temperature": HassRegistryEntity(
        platform="lifx", device_id=DEVICE_ID, area_id=None
    ),
}
AREAS = {"kitchen": {"area_id": "kitchen", "name": "Kitchen", "aliases": []}}
ENTRIES = [
    {
        "domain": "lifx",
        "data": {"host": "192.168.3.191", "serial": SERIAL},
        "unique_id": SERIAL,
    },
    {"domain": "hue", "data": {"host": "10.0.0.9"}, "unique_id": "hue-bridge"},
    {"domain": "lifx", "data": {"serial": "d073d5000000"}, "unique_id": "x"},
]


class FakeHassClient:
    """Duck-typed stand-in for hass_client's HomeAssistantClient."""

    connected = True

    def __init__(self, entries=ENTRIES, fail=False):
        self._entries = entries
        self._fail = fail
        self.commands = []

    async def send_command(self, command, **kwargs):
        self.commands.append(command)
        if self._fail:
            raise RuntimeError("connection lost")
        if command == "config_entries/get":
            return self._entries
        raise AssertionError(f"unexpected command {command}")


class FakeHassProvider:
    """Duck-typed stand-in for the hass provider instance."""

    available = True

    def __init__(self, client=None, fail_registries=False):
        self.hass = client if client is not None else FakeHassClient()
        self._fail_registries = fail_registries

    async def get_device_registry(self):
        if self._fail_registries:
            raise RuntimeError("registry fetch failed")
        return DEVICES

    async def get_entity_registry(self):
        return ENTITIES

    async def get_area_registry(self):
        return AREAS


class TestFetchViaHassProvider(unittest.IsolatedAsyncioTestCase):
    async def test_lifx_light_with_host_from_config_entries(self):
        provider = FakeHassProvider()
        lights = await fetch_ha_via_hass_provider(provider)
        self.assertEqual(len(lights), 1)
        light = lights[0]
        self.assertEqual(light.serial, SERIAL)
        # Host picked up from the send_command("config_entries/get") result.
        self.assertEqual(light.host, "192.168.3.191")
        self.assertEqual(light.area_name, "Kitchen")
        self.assertEqual(light.entity_ids, ("light.kitchen_ceiling",))
        self.assertEqual(provider.hass.commands, ["config_entries/get"])

    async def test_non_lifx_entries_and_devices_filtered(self):
        lights = await fetch_ha_via_hass_provider(FakeHassProvider())
        serials = [light.serial for light in lights]
        self.assertNotIn("d073d5000000", serials)  # entry without host
        self.assertEqual(serials, [SERIAL])  # Hue device excluded

    async def test_config_entries_failure_is_non_fatal(self):
        provider = FakeHassProvider(client=FakeHassClient(fail=True))
        lights = await fetch_ha_via_hass_provider(provider)
        self.assertEqual(len(lights), 1)
        self.assertIsNone(lights[0].host)

    async def test_registry_failure_raises_discovery_error(self):
        provider = FakeHassProvider(fail_registries=True)
        with self.assertRaises(HaDiscoveryError):
            await fetch_ha_via_hass_provider(provider)

    async def test_none_provider_raises_discovery_error(self):
        with self.assertRaises(HaDiscoveryError):
            await fetch_ha_via_hass_provider(None)

    async def test_unavailable_provider_raises_discovery_error(self):
        provider = FakeHassProvider()
        provider.available = False
        with self.assertRaises(HaDiscoveryError):
            await fetch_ha_via_hass_provider(provider)

    async def test_disconnected_client_raises_discovery_error(self):
        client = FakeHassClient()
        client.connected = False
        provider = FakeHassProvider(client=client)
        with self.assertRaises(HaDiscoveryError):
            await fetch_ha_via_hass_provider(provider)

    async def test_dict_rows_and_list_registries_accepted(self):
        """Adapter also accepts dict-shaped entities and list registries."""

        class DictProvider(FakeHassProvider):
            async def get_device_registry(self):
                return list(DEVICES.values())

            async def get_entity_registry(self):
                return {
                    "light.kitchen_ceiling": {
                        "entity_id": "light.kitchen_ceiling",
                        "device_id": DEVICE_ID,
                        "area_id": None,
                        "platform": "lifx",
                    }
                }

            async def get_area_registry(self):
                return [{"area_id": "kitchen", "name": "Kitchen"}]

        lights = await fetch_ha_via_hass_provider(DictProvider())
        self.assertEqual(len(lights), 1)
        self.assertEqual(lights[0].host, "192.168.3.191")


if __name__ == "__main__":
    unittest.main()
