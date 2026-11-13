"""Tests for lifx_lights_sync.discovery pure functions."""

from __future__ import annotations

from typing import Any

from lifx_lights_sync.discovery import (
    HaLifxLight,
    LanDevice,
    apply_ha_hosts,
    merge_targets,
    normalize_serial,
    parse_ha_config_entries,
    parse_ha_registries,
)

SERIAL_A = "d073d500000a"
SERIAL_B = "d073d500000b"
SERIAL_C = "d073d500000c"


def ha_fixture() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Build fake HA registry payloads: devices, entities and areas."""
    devices: list[dict[str, Any]] = [
        {
            "id": "dev-a",
            "manufacturer": "LIFX",
            "name": "Kitchen bulb",
            "name_by_user": None,
            "identifiers": [["lifx", SERIAL_A]],
            "connections": [["mac", "d0:73:d5:00:00:0a"]],
            "area_id": "kitchen-id",
        },
        {
            "id": "dev-b",
            "manufacturer": "LIFX",
            "name": "Porch bulb",
            "identifiers": [["lifx", SERIAL_B]],
            "connections": [["mac", "d0:73:d5:00:00:0b"]],
            "area_id": None,
        },
        {
            "id": "dev-switch",
            "manufacturer": "LIFX",
            "name": "Hall switch",
            "identifiers": [["lifx", SERIAL_C]],
            "connections": [],
            "area_id": None,
        },
        {
            "id": "dev-other",
            "manufacturer": "SomeOtherBrand",
            "name": "Not a LIFX",
            "identifiers": [["other", "123456"]],
            "connections": [],
            "area_id": "kitchen-id",
        },
    ]
    entities: list[dict[str, Any]] = [
        {"entity_id": "light.kitchen_bulb", "device_id": "dev-a", "platform": "lifx"},
        {"entity_id": "sensor.kitchen_bulb_rssi", "device_id": "dev-a", "platform": "lifx"},
        {"entity_id": "light.porch_bulb", "device_id": "dev-b", "platform": "lifx"},
        {"entity_id": "switch.hall_switch", "device_id": "dev-switch", "platform": "lifx"},
        {"entity_id": "light.other_light", "device_id": "dev-other", "platform": "other"},
    ]
    areas: list[dict[str, Any]] = [{"area_id": "kitchen-id", "name": "Kitchen"}]
    return devices, entities, areas


class TestNormalizeSerial:
    """Tests for Normalize Serial."""

    def test_plain_serial(self) -> None:
        """Plain serial."""
        assert (normalize_serial(SERIAL_A)) == (SERIAL_A)

    def test_mac_with_colons_and_uppercase(self) -> None:
        """Mac with colons and uppercase."""
        assert (normalize_serial("D0:73:D5:00:00:0A")) == (SERIAL_A)

    def test_dashes_and_dots(self) -> None:
        """Dashes and dots."""
        assert (normalize_serial("d0-73-d5-00-00-0a")) == (SERIAL_A)
        assert (normalize_serial("d073.d500.000a")) == (SERIAL_A)

    def test_garbage_and_none(self) -> None:
        """Garbage and none."""
        assert (normalize_serial(None)) == ("")
        assert (normalize_serial("----")) == ("")
        # Non-hex letters are stripped; short results are returned as-is
        # so callers can reject them by length.
        assert (normalize_serial("not-a-serial")) == ("aea")


class TestParseHaRegistries:
    """Tests for Parse Ha Registries."""

    def test_extracts_lifx_lights_with_areas(self) -> None:
        """Extracts lifx lights with areas."""
        lights = parse_ha_registries(*ha_fixture())
        assert ([light.serial for light in lights]) == ([SERIAL_A, SERIAL_B])
        kitchen = lights[0]
        assert (kitchen.name) == ("Kitchen bulb")
        assert (kitchen.area_name) == ("Kitchen")
        assert (kitchen.entity_ids) == (("light.kitchen_bulb",))
        assert (lights[1].area_name) is None
        assert (kitchen.host) is None  # registries carry no IP

    def test_switch_without_light_entity_is_skipped(self) -> None:
        """Switch without light entity is skipped."""
        lights = parse_ha_registries(*ha_fixture())
        assert (SERIAL_C) not in ([light.serial for light in lights])

    def test_identifier_only_detection(self) -> None:
        """Identifier only detection."""
        devices: list[dict[str, Any]] = [
            {
                "id": "dev-x",
                "manufacturer": None,
                "name": "Mystery",
                "identifiers": [["lifx", SERIAL_A]],
                "connections": [],
                "area_id": None,
            }
        ]
        entities = [{"entity_id": "light.mystery", "device_id": "dev-x"}]
        lights = parse_ha_registries(devices, entities, [])
        assert (len(lights)) == (1)


class TestParseHaConfigEntries:
    """Tests for Parse Ha Config Entries."""

    def entries_fixture(self) -> list[dict[str, Any]]:
        """Build fake HA config-entry payloads."""
        return [
            {
                "domain": "lifx",
                "entry_id": "entry-a",
                "unique_id": SERIAL_A,
                "data": {"host": "192.0.2.10", "serial": SERIAL_A},
            },
            {
                "domain": "lifx",
                "entry_id": "entry-b",
                "unique_id": SERIAL_B,
                "data": {"serial": SERIAL_B},  # no host stored
            },
            {
                "domain": "hue",
                "entry_id": "entry-x",
                "unique_id": "00:17:88:01:00:00:00:01",
                "data": {"host": "192.0.2.99", "serial": "001788010001"},
            },
        ]

    def test_maps_serial_to_host_for_lifx_only(self) -> None:
        """Maps serial to host for lifx only."""
        hosts = parse_ha_config_entries(self.entries_fixture())
        assert (hosts) == ({SERIAL_A: "192.0.2.10"})

    def test_unique_id_fallback_for_serial(self) -> None:
        """Unique id fallback for serial."""
        entries = [
            {
                "domain": "lifx",
                "unique_id": SERIAL_A,
                "data": {"host": "192.0.2.11"},
            }
        ]
        assert (parse_ha_config_entries(entries)) == ({SERIAL_A: "192.0.2.11"})

    def test_apply_ha_hosts_fills_host(self) -> None:
        """Apply ha hosts fills host."""
        lights = parse_ha_registries(*ha_fixture())
        combined = apply_ha_hosts(lights, {SERIAL_A: "192.0.2.10"})
        by_serial = {light.serial: light for light in combined}
        assert (by_serial[SERIAL_A].host) == ("192.0.2.10")
        assert (by_serial[SERIAL_B].host) is None


class TestMergeTargets:
    """Tests for Merge Targets."""

    def test_ha_host_wins_over_differing_lan_ip(self) -> None:
        """Ha host wins over differing lan ip."""
        ha_lights = [
            HaLifxLight(
                serial=SERIAL_A,
                name="Kitchen bulb",
                area_name="Kitchen",
                entity_ids=("light.kitchen_bulb",),
                host="192.0.2.10",
            )
        ]
        lan = [LanDevice(serial=SERIAL_A, ip="192.0.2.200", name="LAN name")]
        targets = merge_targets(ha_lights, lan)
        area_target = next(t for t in targets if t.kind == "area")
        assert (area_target.lights[0].ip) == ("192.0.2.10")

    def test_lan_fills_in_when_ha_host_missing(self) -> None:
        """Lan fills in when ha host missing."""
        ha_lights = [
            HaLifxLight(
                serial=SERIAL_A,
                name="Kitchen bulb",
                area_name="Kitchen",
                entity_ids=("light.kitchen_bulb",),
                host=None,
            )
        ]
        lan = [LanDevice(serial=SERIAL_A, ip="192.0.2.200")]
        targets = merge_targets(ha_lights, lan)
        area_target = next(t for t in targets if t.kind == "area")
        assert (area_target.lights[0].ip) == ("192.0.2.200")

    def test_area_and_ungrouped_targets(self) -> None:
        """Area and ungrouped targets."""
        lights = parse_ha_registries(*ha_fixture())
        targets = merge_targets(lights, [])
        kinds = {target.target_id: target.kind for target in targets}
        assert (kinds.get("area-kitchen")) == ("area")
        assert (kinds.get("ungrouped")) == ("ungrouped")
        ungrouped = next(t for t in targets if t.kind == "ungrouped")
        assert ([light.serial for light in ungrouped.lights]) == ([SERIAL_B])

    def test_lan_only_mode_groups_bulbs_under_one_target(self) -> None:
        """Lan only mode groups bulbs under one target."""
        lan = [
            LanDevice(serial=SERIAL_A, ip="192.0.2.10", name="Bulb A"),
            LanDevice(serial=SERIAL_B, ip="192.0.2.11", name="Bulb B"),
        ]
        targets = merge_targets([], lan)
        assert (len(targets)) == (1)
        assert (targets[0].target_id) == ("lifx-lights")
        assert (targets[0].name) == ("LIFX Lights")
        assert (len(targets[0].channels)) == (2)

    def test_matrix_light_gets_own_target_and_area_membership(self) -> None:
        """Matrix light gets own target and area membership."""
        ha_lights = [
            HaLifxLight(
                serial=SERIAL_A,
                name="Ceiling",
                area_name="Living room",
                entity_ids=("light.ceiling",),
                host="192.0.2.20",
            )
        ]
        lan = [
            LanDevice(
                serial=SERIAL_A,
                ip="192.0.2.200",
                is_matrix=True,
                is_ceiling=True,
                width=8,
                height=8,
            )
        ]
        targets = merge_targets(ha_lights, lan)
        matrix = next(t for t in targets if t.kind == "matrix")
        # 8x8 ceiling: 64 zones minus the uplight zone = 63 channels.
        assert (len(matrix.channels)) == (63)
        assert all(c.is_matrix_zone for c in matrix.channels)
        area = next(t for t in targets if t.kind == "area")
        assert ([c.channel_id for c in area.channels]) == ([SERIAL_A])
        # HA host still wins for the matrix light's control IP.
        assert (matrix.lights[0].ip) == ("192.0.2.20")

    def test_generic_matrix_uses_all_zones(self) -> None:
        """Generic matrix uses all zones."""
        lan = [
            LanDevice(
                serial=SERIAL_A,
                ip="192.0.2.30",
                name="Tile",
                is_matrix=True,
                width=8,
                height=8,
            )
        ]
        targets = merge_targets([], lan)
        matrix = next(t for t in targets if t.kind == "matrix")
        assert (len(matrix.channels)) == (64)

    def test_include_lan_only_false_drops_unknown_lan_devices(self) -> None:
        """Include lan only false drops unknown lan devices."""
        ha_lights = [
            HaLifxLight(
                serial=SERIAL_A,
                name="Known",
                area_name=None,
                entity_ids=("light.known",),
                host="192.0.2.10",
            )
        ]
        lan = [LanDevice(serial=SERIAL_B, ip="192.0.2.50", name="Stranger")]
        targets = merge_targets(ha_lights, lan, include_lan_only=False)
        all_serials = {light.serial for t in targets for light in t.lights}
        assert (SERIAL_B) not in (all_serials)

    def test_empty_inputs(self) -> None:
        """Empty inputs."""
        assert (merge_targets([], [])) == ([])
