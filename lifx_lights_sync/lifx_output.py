"""LIFX output: render commands -> lifx-async device calls.

One :class:`LifxSender` owns the output side of one sync target:

- ``snapshot()`` records every light's current color/power (and zone
  colors for matrix lights) before a stream starts;
- ``send()`` applies analyzer commands, rate-limited per device —
  bulbs get a single ``Light.set_color()``, matrix lights get a full
  frame, ceiling lights are written through
  ``CeilingLight.set_downlight_colors()`` so the uplight zone (the last
  zone of the fixture) is never overwritten by image pixels;
- ``restore()`` puts the snapshot back when the stream stops (unlike
  Hue, LIFX lights do not revert on their own, so this is mandatory).

All lifx-async imports are lazy (inside methods): this module imports
cleanly on a machine without lifx-async installed, which the unit tests
rely on. Confirmed lifx-async 8.0.0 API used here:

- ``lifx.color.HSBK(hue, saturation, brightness, kelvin)`` and
  ``HSBK.from_rgb(r, g, b)`` with 0.0-1.0 floats;
- ``Light.get_color() -> (HSBK, power, label)``,
  ``Light.set_color(HSBK, duration=seconds)``,
  ``Light.set_power(bool | int, duration=seconds)``;
- ``MatrixLight.set_matrix_colors(tile_index, colors, duration=ms)``,
  ``MatrixLight.get_all_tile_colors() -> list[list[HSBK]]``,
  ``MatrixLight.get_device_chain() -> list[TileInfo(width, height)]``;
- ``CeilingLight.set_downlight_colors(HSBK | list[HSBK], duration=seconds)``,
  ``CeilingLight.downlight_zone_count`` / ``uplight_zone`` properties.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from . import constants as const
from .analyzer import LightColorCommand
from .color import RGB, rgb_to_hsbk
from .discovery import LanDevice, SyncTarget, normalize_serial

_LOGGER = logging.getLogger(__name__)


@dataclass
class DeviceSnapshot:
    """Pre-stream state of one light."""

    serial: str
    power: int | None = None  # 0 or 65535 as reported by lifx-async
    color: tuple[float, float, float, int] | None = None  # HSBK components
    zone_colors: list[tuple[float, float, float, int]] | None = None


def _hsbk_components(color: Any) -> tuple[float, float, float, int] | None:
    """Extract (hue, saturation, brightness, kelvin) from a lifx HSBK."""
    if color is None:
        return None
    try:
        return (
            float(color.hue),
            float(color.saturation),
            float(color.brightness),
            int(color.kelvin),
        )
    except (AttributeError, TypeError, ValueError):
        return None


def _channel_serial_and_zone(channel_id: str) -> tuple[str, int | None]:
    """Split a channel id into (serial, zone index or None)."""
    parts = channel_id.split(":zone:")
    if len(parts) == 2 and parts[1].isdigit():
        return normalize_serial(parts[0]), int(parts[1])
    return normalize_serial(channel_id), None


async def open_devices(
    sources: Mapping[str, str] | Iterable[LanDevice],
) -> dict[str, Any]:
    """Create connected lifx-async device objects for known serial/IPs.

    ``sources`` is either a serial -> IP mapping (the normal path: IPs
    already resolved by discovery, HA config-entry hosts first) or an
    iterable of :class:`~.discovery.LanDevice`.

    Primary path per device: ``Device.from_ip(ip, serial=serial)`` —
    in lifx-async 8.0.0 this factory queries the product and returns an
    instance of the appropriate subclass (Light / MatrixLight /
    CeilingLight / ...). Devices that fail the direct path fall back to
    a ``discover_devices`` sweep matched on serial, using the confirmed
    ``DiscoveredDevice.create_device()``. Failures are logged and
    skipped, never fatal.
    """
    if isinstance(sources, Mapping):
        wanted = {
            normalize_serial(serial): str(ip)
            for serial, ip in sources.items()
            if ip
        }
    else:
        wanted = {
            device.serial: device.ip for device in sources if device.ip
        }
    if not wanted:
        return {}

    opened: dict[str, Any] = {}

    async def _adopt(serial: str, device: Any) -> None:
        try:
            # Devices work without an explicit enter, but entering loads
            # state/geometry (and the ceiling tile chain) up front.
            # TODO(live-verify): Device.__aenter__ loads state in
            # lifx-async 8.0.0; verify entering here does not conflict
            # with the connection the Animator later borrows.
            await device.__aenter__()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Device %s entered without context", serial, exc_info=True)
        opened[serial] = device

    try:
        from lifx.devices.base import Device  # noqa: PLC0415

        for serial, ip in wanted.items():
            try:
                # TODO(live-verify): Device.from_ip returning the
                # concrete subclass is documented in the 8.0.0 source;
                # confirm the returned type for a Ceiling on live hw.
                device = await Device.from_ip(ip, serial=serial)
            except Exception:  # noqa: BLE001
                _LOGGER.debug(
                    "Direct from_ip failed for %s (%s); will try sweep",
                    serial,
                    ip,
                    exc_info=True,
                )
                continue
            if device is not None:
                await _adopt(serial, device)
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Direct device creation unavailable", exc_info=True)

    missing = sorted(set(wanted) - set(opened))
    if missing:
        try:
            from lifx.network.discovery import discover_devices  # noqa: PLC0415

            async for discovered in discover_devices(
                timeout=const.LAN_DISCOVERY_TIMEOUT_S
            ):
                serial = normalize_serial(getattr(discovered, "serial", ""))
                if serial not in missing or serial in opened:
                    continue
                try:
                    device = await discovered.create_device()
                except Exception:  # noqa: BLE001
                    _LOGGER.warning(
                        "Could not create LIFX device %s", serial, exc_info=True
                    )
                    continue
                if device is not None:
                    await _adopt(serial, device)
        except Exception:  # noqa: BLE001
            _LOGGER.warning("LIFX sweep fallback failed", exc_info=True)

    still_missing = sorted(set(wanted) - set(opened))
    if still_missing:
        _LOGGER.warning(
            "LIFX devices not reachable for output: %s", ", ".join(still_missing)
        )
    return opened


class LifxSender:
    """Applies analyzer commands to the lights of one sync target."""

    def __init__(
        self,
        target: SyncTarget,
        devices: Mapping[str, Any],
        send_rate_hz: int = const.DEFAULT_SEND_RATE_HZ,
    ) -> None:
        self._target = target
        self._devices: dict[str, Any] = {
            normalize_serial(serial): device for serial, device in devices.items()
        }
        self._min_interval = 1.0 / max(1, send_rate_hz)
        self._last_send: dict[str, float] = {}
        self._powered_on: set[str] = set()
        self._snapshots: dict[str, DeviceSnapshot] = {}
        self._last_frame: dict[str, dict[int, RGB]] = {}
        self._lock = asyncio.Lock()

    # -- configuration -----------------------------------------------------

    def set_send_rate(self, send_rate_hz: int) -> None:
        self._min_interval = 1.0 / max(1, send_rate_hz)

    @property
    def target(self) -> SyncTarget:
        return self._target

    # -- helpers -------------------------------------------------------------

    def _device(self, serial: str) -> Any | None:
        return self._devices.get(serial)

    @staticmethod
    def _to_hsbk(rgb: RGB) -> Any:
        from lifx.color import HSBK  # noqa: PLC0415

        hue, saturation, brightness, kelvin = rgb_to_hsbk(rgb)
        if saturation == 0.0:
            # HSBK.from_rgb collapses greys to kelvin whites; building the
            # color directly keeps brightness exact for white content.
            return HSBK(hue=hue, saturation=0.0, brightness=brightness, kelvin=kelvin)
        return HSBK.from_rgb(rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0)

    def _rate_limited(self, serial: str) -> bool:
        now = time.monotonic()
        last = self._last_send.get(serial, 0.0)
        if now - last < self._min_interval:
            return True
        self._last_send[serial] = now
        return False

    async def _ensure_power(self, serial: str, device: Any) -> None:
        if serial in self._powered_on:
            return
        try:
            await device.set_power(True)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("set_power failed for %s", serial, exc_info=True)
        self._powered_on.add(serial)

    # -- snapshot / restore --------------------------------------------------

    async def snapshot(self) -> None:
        """Record the current state of every reachable target light."""
        async with self._lock:
            self._snapshots.clear()
            for light in self._target.lights:
                device = self._device(light.serial)
                if device is None:
                    continue
                snapshot = DeviceSnapshot(serial=light.serial)
                try:
                    color, power, _label = await device.get_color()
                    snapshot.color = _hsbk_components(color)
                    snapshot.power = int(power)
                except Exception:  # noqa: BLE001
                    _LOGGER.debug("Snapshot color failed for %s", light.serial, exc_info=True)
                if light.is_matrix:
                    try:
                        tiles = await device.get_all_tile_colors()
                        if tiles:
                            snapshot.zone_colors = [
                                components
                                for components in (
                                    _hsbk_components(zone) for zone in tiles[0]
                                )
                                if components is not None
                            ]
                    except Exception:  # noqa: BLE001
                        _LOGGER.debug(
                            "Snapshot zones failed for %s", light.serial, exc_info=True
                        )
                self._snapshots[light.serial] = snapshot

    async def restore(self) -> None:
        """Restore the snapshot taken by :meth:`snapshot` (best effort)."""
        async with self._lock:
            snapshots, self._snapshots = self._snapshots, {}
            self._powered_on.clear()
            self._last_frame.clear()
        for serial, snapshot in snapshots.items():
            device = self._device(serial)
            if device is None:
                continue
            try:
                if snapshot.zone_colors:
                    await self._restore_zones(serial, device, snapshot)
                elif snapshot.color is not None:
                    await device.set_color(self._hsbk_from_components(snapshot.color))
                if snapshot.power is not None:
                    await device.set_power(bool(snapshot.power))
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Restore failed for %s", serial, exc_info=True)

    def _hsbk_from_components(self, components: tuple[float, float, float, int]) -> Any:
        from lifx.color import HSBK  # noqa: PLC0415

        hue, saturation, brightness, kelvin = components
        return HSBK(
            hue=hue, saturation=saturation, brightness=brightness, kelvin=kelvin
        )

    async def _restore_zones(
        self, serial: str, device: Any, snapshot: DeviceSnapshot
    ) -> None:
        colors = [
            self._hsbk_from_components(components)
            for components in snapshot.zone_colors or []
        ]
        if not colors:
            return
        if type(device).__name__ == "CeilingLight":
            downlight_count = self._downlight_count(serial, colors)
            await device.set_downlight_colors(colors[:downlight_count])
            if len(colors) > downlight_count:
                # The final saved zone is the uplight: put it back too.
                try:
                    await device.set_uplight_color(colors[downlight_count])
                except Exception:  # noqa: BLE001
                    _LOGGER.debug("Uplight restore failed for %s", serial, exc_info=True)
        else:
            await device.set_matrix_colors(0, colors, duration=0)

    def _downlight_count(self, serial: str, colors: Sequence[Any]) -> int:
        light = next(
            (item for item in self._target.lights if item.serial == serial), None
        )
        if light is not None:
            return light.downlight_zone_count
        return max(0, len(colors) - 1)

    # -- sending ---------------------------------------------------------------

    async def send(
        self, commands: Sequence[LightColorCommand], target: SyncTarget | None = None
    ) -> None:
        """Apply one render's commands (rate-limited per device)."""
        target = target or self._target
        bulbs: dict[str, RGB] = {}
        zones: dict[str, dict[int, RGB]] = {}
        for command in commands:
            serial, zone = _channel_serial_and_zone(command.channel_id)
            if zone is None:
                bulbs[serial] = command.rgb
            else:
                zones.setdefault(serial, {})[zone] = command.rgb

        for serial, rgb in bulbs.items():
            device = self._device(serial)
            if device is None or self._rate_limited(serial):
                continue
            if max(rgb) > 0:
                await self._ensure_power(serial, device)
            try:
                await device.set_color(self._to_hsbk(rgb))
            except Exception:  # noqa: BLE001
                _LOGGER.debug("set_color failed for %s", serial, exc_info=True)

        for serial, zone_map in zones.items():
            device = self._device(serial)
            if device is None or self._rate_limited(serial):
                continue
            frame = self._last_frame.setdefault(serial, {})
            frame.update(zone_map)
            self._last_frame[serial] = frame
            if frame and max(max(rgb) for rgb in frame.values()) > 0:
                await self._ensure_power(serial, device)
            try:
                await self._send_matrix_frame(serial, device, frame, target)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Matrix send failed for %s", serial, exc_info=True)

    async def _send_matrix_frame(
        self,
        serial: str,
        device: Any,
        frame: dict[int, RGB],
        target: SyncTarget,
    ) -> None:
        light = next(
            (item for item in target.lights if item.serial == serial), None
        )
        width = (light.width if light else None) or target.width or 1
        height = (light.height if light else None) or target.height or 1
        total_zones = width * height
        is_ceiling = bool(light and light.is_ceiling) or target.is_ceiling

        if is_ceiling and type(device).__name__ == "CeilingLight":
            downlight_count = total_zones - 1 if total_zones > 1 else total_zones
            colors = [
                self._to_hsbk(frame.get(index, (0, 0, 0)))
                for index in range(downlight_count)
            ]
            # set_downlight_colors only touches zones before the uplight
            # zone, so the uplight keeps whatever color it already had.
            await device.set_downlight_colors(colors)
            return

        colors = [
            self._to_hsbk(frame.get(index, (0, 0, 0))) for index in range(total_zones)
        ]
        if is_ceiling and colors:
            # Generic matrix path on a ceiling fixture: preserve the
            # uplight by re-sending its snapshot color in the last zone.
            snapshot = self._snapshots.get(serial)
            if snapshot and snapshot.zone_colors and len(snapshot.zone_colors) == total_zones:
                colors[-1] = self._hsbk_from_components(snapshot.zone_colors[-1])
        # TODO(live-verify): for sustained high-rate animation the
        # lifx-async Animator (device.animator.prepare() then the
        # synchronous Animator.send_frame() with uint16 HSBK tuples) is
        # the library's fast path; set_matrix_colors is the confirmed
        # simple path and is what we ship until frame rates are measured
        # on real hardware.
        await device.set_matrix_colors(0, colors, duration=0)

    async def close(self) -> None:
        """Close all device connections owned by this sender."""
        for serial, device in self._devices.items():
            try:
                await device.close()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Close failed for %s", serial, exc_info=True)
        self._devices.clear()


__all__ = ["DeviceSnapshot", "LifxSender", "open_devices"]
