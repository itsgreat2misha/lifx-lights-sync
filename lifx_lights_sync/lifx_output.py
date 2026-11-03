"""LIFX output: render commands -> lifx-async device calls.

One :class:`LifxSender` owns the output side of one sync target:

- ``snapshot()`` records every light's current color/power (and zone
  colors for matrix lights) before a stream starts;
- ``send()`` applies analyzer commands, rate-limited per device, using
  the LedFx-proven output shapes: bulbs get one fire-and-forget
  ``Light.SetColor`` packet (no ack wait; the bulb interpolates over
  exactly one frame interval; capped at 20 Hz), matrix and ceiling
  lights get full frames through the lifx-async ``Animator``
  (``send_frame`` with protocol HSBK tuples, the snapshot uplight color
  re-injected into the ceiling's last zone every frame);
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


def _protocol_tuple(color: Any) -> tuple[int, int, int, int]:
    """Convert an HSBK to the Animator's plain uint16 protocol tuple."""
    return (
        round(color.hue / 360.0 * 65535) % 65536,
        round(color.saturation * 65535),
        round(color.brightness * 65535),
        int(color.kelvin),
    )


def _channel_serial_and_zone(channel_id: str) -> tuple[str, int | None]:
    """Split a channel id into (serial, zone index or None)."""
    parts = channel_id.split(":zone:")
    if len(parts) == 2 and parts[1].isdigit():
        return normalize_serial(parts[0]), int(parts[1])
    return normalize_serial(channel_id), None


def device_class_name(is_matrix: bool, is_ceiling: bool) -> str:
    """Pick the lifx-async device class for a light's shape."""
    if is_ceiling:
        return "CeilingLight"
    if is_matrix:
        return "MatrixLight"
    return "Light"


async def _create_typed_device(ip: str, serial: str, class_name: str) -> Any:
    """Create a device via the concrete class's ``from_ip`` factory.

    ``Device.from_ip`` returns a BASE Device with no color methods at
    all (proven live: get_color/set_color AttributeErrors, silently
    swallowed, made the whole plugin a no-op) — the factory is
    class-aware, so it must be called on Light / MatrixLight /
    CeilingLight. Falls back to Light when a subclass is unavailable.
    """
    device_cls: Any = None
    try:
        if class_name == "CeilingLight":
            from lifx.devices.ceiling import CeilingLight  # noqa: PLC0415

            device_cls = CeilingLight
        elif class_name == "MatrixLight":
            from lifx.devices.matrix import MatrixLight  # noqa: PLC0415

            device_cls = MatrixLight
    except Exception:  # noqa: BLE001
        device_cls = None
    if device_cls is None:
        from lifx.devices.light import Light  # noqa: PLC0415

        device_cls = Light
    return await device_cls.from_ip(ip, serial=serial)


async def open_devices(
    sources: Mapping[str, str] | Iterable[Any],
) -> dict[str, Any]:
    """Create connected lifx-async device objects for known serial/IPs.

    ``sources`` is either a serial -> IP mapping (devices are created
    as plain Lights) or an iterable of objects carrying ``serial``,
    ``ip``, ``is_matrix`` and ``is_ceiling`` (LanDevice /
    ResolvedLight), which selects the concrete device class per
    light. Devices that fail the direct path fall back to a
    ``discover_devices`` sweep matched on serial, using the confirmed
    ``DiscoveredDevice.create_device()``. Failures are logged and
    skipped, never fatal.
    """
    # serial -> (ip, is_matrix, is_ceiling)
    wanted: dict[str, tuple[str, bool, bool]] = {}
    if isinstance(sources, Mapping):
        for serial, ip in sources.items():
            if ip:
                wanted[normalize_serial(serial)] = (str(ip), False, False)
    else:
        for device in sources:
            ip = getattr(device, "ip", None)
            serial = normalize_serial(getattr(device, "serial", ""))
            if ip and len(serial) == 12:
                wanted[serial] = (
                    str(ip),
                    bool(getattr(device, "is_matrix", False)),
                    bool(getattr(device, "is_ceiling", False)),
                )
    if not wanted:
        return {}

    opened: dict[str, Any] = {}

    async def _adopt(serial: str, device: Any) -> None:
        try:
            # Devices work without an explicit enter, but entering loads
            # state/geometry (and the ceiling tile chain) up front.
            await device.__aenter__()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Device %s entered without context", serial, exc_info=True)
        opened[serial] = device

    for serial, (ip, is_matrix, is_ceiling) in wanted.items():
        class_name = device_class_name(is_matrix, is_ceiling)
        try:
            device = await _create_typed_device(ip, serial, class_name)
        except Exception:  # noqa: BLE001
            _LOGGER.debug(
                "Direct from_ip (%s) failed for %s (%s); will try sweep",
                class_name,
                serial,
                ip,
                exc_info=True,
            )
            continue
        if device is not None:
            await _adopt(serial, device)

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
        self._send_rate_hz = max(1, send_rate_hz)
        self._min_interval = 1.0 / self._send_rate_hz
        self._last_send: dict[str, float] = {}
        self._powered_on: set[str] = set()
        self._snapshots: dict[str, DeviceSnapshot] = {}
        self._last_frame: dict[str, dict[int, RGB]] = {}
        self._animators: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    # -- configuration -----------------------------------------------------

    def set_send_rate(self, send_rate_hz: int) -> None:
        self._send_rate_hz = max(1, send_rate_hz)
        self._min_interval = 1.0 / self._send_rate_hz

    @property
    def _bulb_interval(self) -> float:
        """Bulbs are capped at 20 Hz (LedFx: higher rates strobo)."""
        return max(self._min_interval, 1.0 / 20)

    @property
    def _bulb_duration_ms(self) -> int:
        """Bulb transition: exactly one frame interval (LedFx-style)."""
        return max(1, int(1000 * self._bulb_interval) - 2)

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

    def _rate_limited(self, serial: str, interval: float | None = None) -> bool:
        now = time.monotonic()
        last = self._last_send.get(serial, 0.0)
        if now - last < (self._min_interval if interval is None else interval):
            return True
        self._last_send[serial] = now
        return False

    async def _send_bulb_packet(self, device: Any, rgb: RGB) -> None:
        """Send one bulb color as a raw SetColor packet (LedFx-style).

        Fire-and-forget: no acknowledgement is awaited, so the send
        cadence stays metronome-steady, and the bulb's own hardware
        interpolates over exactly one frame interval.
        """
        from lifx.protocol import packets  # noqa: PLC0415

        color = self._to_hsbk(rgb).to_protocol()
        packet = packets.Light.SetColor(
            color=color, duration=self._bulb_duration_ms
        )
        await device.connection.send_packet(packet)

    async def _get_animator(self, serial: str, device: Any) -> Any | None:
        """Return the device's prepared Animator (matrix fast path)."""
        animator = self._animators.get(serial)
        if animator is None:
            device_animator = getattr(device, "animator", None)
            if device_animator is None:
                return None
            animator = await device_animator.prepare()
            self._animators[serial] = animator
        return animator

    def _close_animators(self) -> None:
        for animator in self._animators.values():
            try:
                animator.close()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Animator close failed", exc_info=True)
        self._animators.clear()

    async def _ensure_power(self, serial: str, device: Any) -> None:
        if serial in self._powered_on:
            return
        try:
            await device.set_power(True)
        except Exception:  # noqa: BLE001
            _LOGGER.warning("set_power failed for %s", serial, exc_info=True)
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
                    _LOGGER.warning("Snapshot color failed for %s", light.serial, exc_info=True)
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
        # Animators bypass the device color methods; close them so the
        # awaited restore calls below own the device state again.
        self._close_animators()
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
            if device is None or self._rate_limited(serial, self._bulb_interval):
                continue
            if max(rgb) > 0:
                await self._ensure_power(serial, device)
            try:
                await self._send_bulb_packet(device, rgb)
            except Exception:  # noqa: BLE001
                _LOGGER.warning("Bulb packet send failed for %s", serial, exc_info=True)

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
                _LOGGER.warning("Matrix send failed for %s", serial, exc_info=True)

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

        # Compose the full frame; on a ceiling fixture the last zone is
        # the uplight, which keeps its snapshot color (the analyzer only
        # drives the downlight zones).
        colors = [
            self._to_hsbk(frame.get(index, (0, 0, 0))) for index in range(total_zones)
        ]
        if is_ceiling and colors:
            snapshot = self._snapshots.get(serial)
            if snapshot and snapshot.zone_colors and len(snapshot.zone_colors) == total_zones:
                colors[-1] = self._hsbk_from_components(snapshot.zone_colors[-1])

        # Fast path (LedFx-style): the lifx-async Animator sends frames
        # synchronously through one socket with ack-gated flow control —
        # no per-frame request/response stalls. It takes plain uint16
        # protocol tuples (HSBK.to_protocol() returns a non-iterable
        # LightHsbk object and raises inside send_frame).
        if type(device).__name__ in ("MatrixLight", "CeilingLight"):
            try:
                animator = await self._get_animator(serial, device)
                if animator is not None:
                    animator.send_frame(
                        [_protocol_tuple(color) for color in colors]
                    )
                    return
            except Exception:  # noqa: BLE001
                _LOGGER.warning(
                    "Animator send failed for %s; falling back", serial, exc_info=True
                )
                self._animators.pop(serial, None)

        if is_ceiling and type(device).__name__ == "CeilingLight":
            # set_downlight_colors only touches zones before the uplight
            # zone, so the uplight keeps whatever color it already had.
            await device.set_downlight_colors(colors[:-1], duration=0.12)
            return
        await device.set_matrix_colors(0, colors, duration=120)

    async def close(self) -> None:
        """Close all device connections owned by this sender."""
        self._close_animators()
        for serial, device in self._devices.items():
            try:
                await device.close()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Close failed for %s", serial, exc_info=True)
        self._devices.clear()


__all__ = ["DeviceSnapshot", "LifxSender", "open_devices"]
