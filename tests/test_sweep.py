"""Tests for the unicast sweep helpers (pure functions only)."""

from __future__ import annotations

import struct

from lifx_lights_sync.discovery import (
    ResolvedLight,
    SyncTarget,
    apply_sweep_results,
    build_get_service_packet,
    parse_state_service,
    subnets_from_ips,
)
from lifx_lights_sync.lifx_output import device_class_name


def _state_service_packet(serial_hex: str, service: int = 1, msg_type: int = 3) -> bytes:
    """State service packet."""
    header = struct.pack(
        "<HHI8s6sBBQHH",
        36 + 5,
        1024 | (1 << 12),
        7,
        bytes.fromhex(serial_hex) + b"\x00\x00",
        b"\x00" * 6,
        0,
        0,
        0,
        msg_type,
        0,
    )
    return header + struct.pack("<BI", service, 56700)


class TestPacket:
    """Tests for Packet."""

    def test_get_service_packet_shape(self) -> None:
        """Get service packet shape."""
        packet = build_get_service_packet()
        assert (len(packet)) == (36)
        size, bits = struct.unpack_from("<HH", packet, 0)
        assert (size) == (36)
        assert (bits) == (1024 | (1 << 12) | (1 << 13))
        msg_type = struct.unpack_from("<H", packet, 32)[0]
        assert (msg_type) == (2)

    def test_parse_state_service(self) -> None:
        """Parse state service."""
        packet = _state_service_packet("d073d500000b")
        assert (parse_state_service(packet)) == ("d073d500000b")

    def test_parse_rejects_other_types(self) -> None:
        """Parse rejects other types."""
        packet = _state_service_packet("d073d500000b", msg_type=2)
        assert (parse_state_service(packet)) is None

    def test_parse_rejects_non_udp_service(self) -> None:
        """Parse rejects non udp service."""
        packet = _state_service_packet("d073d500000b", service=2)
        assert (parse_state_service(packet)) is None

    def test_parse_rejects_short(self) -> None:
        """Parse rejects short."""
        assert (parse_state_service(b"\x00" * 20)) is None


class TestHelpers:
    """Tests for Helpers."""

    def test_subnets_from_ips(self) -> None:
        """Subnets from ips."""
        assert (subnets_from_ips(["192.0.2.5", "192.0.2.9", "10.0.0.2", "junk", ""])) == (
            ["192.0.2", "10.0.0"]
        )

    def test_apply_sweep_results(self) -> None:
        """Apply sweep results."""
        target = SyncTarget(
            target_id="area-x",
            name="X",
            kind="area",
            lights=[
                ResolvedLight(serial="aaaaaaaaaaaa", name="A", area_name="X"),
                ResolvedLight(serial="bbbbbbbbbbbb", name="B", area_name="X", ip="192.0.2.9"),
                ResolvedLight(serial="cccccccccccc", name="C", area_name="X"),
            ],
        )
        filled = apply_sweep_results(
            [target], {"aaaaaaaaaaaa": "192.0.2.44", "bbbbbbbbbbbb": "198.51.100.1"}
        )
        assert (filled) == (1)
        assert (target.lights[0].ip) == ("192.0.2.44")
        # Existing IPs are never overwritten by a sweep result.
        assert (target.lights[1].ip) == ("192.0.2.9")
        assert (target.lights[2].ip) is None


class TestDeviceClassName:
    """Tests for Device Class Name."""

    def test_selection(self) -> None:
        """Selection."""
        assert (device_class_name(False, False)) == ("Light")
        assert (device_class_name(True, False)) == ("MatrixLight")
        assert (device_class_name(True, True)) == ("CeilingLight")
        assert (device_class_name(False, True)) == ("CeilingLight")
