"""Tests for the unicast sweep helpers (pure functions only)."""

from __future__ import annotations

import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lifx_lights_sync.discovery import (
    LanDevice,
    ResolvedLight,
    SyncTarget,
    apply_sweep_results,
    build_get_service_packet,
    parse_state_service,
    subnets_from_ips,
)


def _state_service_packet(serial_hex: str, service: int = 1, msg_type: int = 3) -> bytes:
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


class TestPacket(unittest.TestCase):
    def test_get_service_packet_shape(self) -> None:
        packet = build_get_service_packet()
        self.assertEqual(len(packet), 36)
        size, bits = struct.unpack_from("<HH", packet, 0)
        self.assertEqual(size, 36)
        self.assertEqual(bits, 1024 | (1 << 12) | (1 << 13))
        msg_type = struct.unpack_from("<H", packet, 32)[0]
        self.assertEqual(msg_type, 2)

    def test_parse_state_service(self) -> None:
        packet = _state_service_packet("d073d584d445")
        self.assertEqual(parse_state_service(packet), "d073d584d445")

    def test_parse_rejects_other_types(self) -> None:
        packet = _state_service_packet("d073d584d445", msg_type=2)
        self.assertIsNone(parse_state_service(packet))

    def test_parse_rejects_non_udp_service(self) -> None:
        packet = _state_service_packet("d073d584d445", service=2)
        self.assertIsNone(parse_state_service(packet))

    def test_parse_rejects_short(self) -> None:
        self.assertIsNone(parse_state_service(b"\x00" * 20))


class TestHelpers(unittest.TestCase):
    def test_subnets_from_ips(self) -> None:
        self.assertEqual(
            subnets_from_ips(["192.168.3.5", "192.168.3.9", "10.0.0.2", "junk", ""]),
            ["192.168.3", "10.0.0"],
        )

    def test_apply_sweep_results(self) -> None:
        target = SyncTarget(
            target_id="area-x",
            name="X",
            kind="area",
            lights=[
                ResolvedLight(serial="aaaaaaaaaaaa", name="A", area_name="X"),
                ResolvedLight(
                    serial="bbbbbbbbbbbb", name="B", area_name="X", ip="192.168.3.9"
                ),
                ResolvedLight(serial="cccccccccccc", name="C", area_name="X"),
            ],
        )
        filled = apply_sweep_results(
            [target], {"aaaaaaaaaaaa": "192.168.3.44", "bbbbbbbbbbbb": "1.1.1.1"}
        )
        self.assertEqual(filled, 1)
        self.assertEqual(target.lights[0].ip, "192.168.3.44")
        # Existing IPs are never overwritten by a sweep result.
        self.assertEqual(target.lights[1].ip, "192.168.3.9")
        self.assertIsNone(target.lights[2].ip)


if __name__ == "__main__":
    unittest.main()


class TestDeviceClassName(unittest.TestCase):
    def test_selection(self) -> None:
        from lifx_lights_sync.lifx_output import device_class_name

        self.assertEqual(device_class_name(False, False), "Light")
        self.assertEqual(device_class_name(True, False), "MatrixLight")
        self.assertEqual(device_class_name(True, True), "CeilingLight")
        self.assertEqual(device_class_name(False, True), "CeilingLight")
