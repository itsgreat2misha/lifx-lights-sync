"""Discovery: Home Assistant first, LAN as fallback and gap-filler.

Strategy (see README for the diagram):

1. Query Home Assistant for the device, entity and area registries
   (identity, friendly names, areas, bound ``light.*`` entities) AND
   its LIFX config entries (``config_entries/get``). Each per-device
   LIFX config entry stores ``data = {"host": <ip>, "serial":
   <serial>}`` — this is how HA itself reaches the light, and its
   discovery keeps ``host`` current when the IP changes. So HA
   provides the *primary* serial -> IP mapping. (HA diagnostics
   redacts host/serial on display, which is easy to mistake for "HA
   doesn't store IPs" — the config entry data does.)
   The HA data is fetched either through Music Assistant's own
   ``hass`` provider (preferred — it already holds an authenticated
   connection; see ``ha_via_mass``) or directly over HA's WebSocket
   API with a configured URL + long-lived token
   (:func:`fetch_ha_lifx_lights`).
2. Discover devices on the LAN with lifx-async (UDP broadcast + mDNS).
   The LAN is (a) the fallback when HA is absent/unreachable, (b) a
   gap-filler for lights whose HA entry lacks a host, and (c) an
   optional cross-check — plus it is the only source of matrix geometry.
3. Merge on the normalized serial with strict precedence: an HA host
   always wins over a differing LAN IP for the same serial. LAN devices
   HA does not know are still usable in auto/lan modes; HA lights with
   no IP from either source are kept as targets without an IP (skipped
   by the sender until a later discovery finds them).

The pure functions (:func:`normalize_serial`, :func:`parse_ha_registries`,
:func:`merge_targets`) have no third-party imports and are unit-tested.
The async fetchers import aiohttp / lifx-async lazily.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from . import constants as const
from .analyzer import LightChannel

_LOGGER = logging.getLogger(__name__)

_HEX_DIGITS = re.compile(r"[^0-9a-f]")


class HaDiscoveryError(RuntimeError):
    """Home Assistant discovery could not be performed.

    Raised by the HA fetchers when the source is absent, unavailable or
    errors out, so the provider can fall back to the next source (or to
    LAN discovery) with a clear log line.
    """


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HaLifxLight:
    """A LIFX light as known by Home Assistant."""

    serial: str
    name: str
    area_name: str | None
    entity_ids: tuple[str, ...]
    device_id: str = ""
    mac: str | None = None
    # Primary control IP, from the LIFX config entry's data["host"].
    # None when HA has no config entry (or no host) for this light.
    host: str | None = None


@dataclass(frozen=True)
class LanDevice:
    """A LIFX device found on the local network."""

    serial: str
    ip: str
    name: str | None = None
    product: int | None = None
    is_matrix: bool = False
    is_ceiling: bool = False
    width: int | None = None
    height: int | None = None
    port: int = const.LIFX_UDP_PORT


@dataclass(frozen=True)
class ResolvedLight:
    """One physical light after the HA/LAN merge."""

    serial: str
    name: str
    area_name: str | None
    entity_ids: tuple[str, ...] = ()
    ip: str | None = None
    is_matrix: bool = False
    is_ceiling: bool = False
    width: int | None = None
    height: int | None = None
    product: int | None = None

    @property
    def zone_count(self) -> int:
        if self.width and self.height:
            return self.width * self.height
        return 1

    @property
    def downlight_zone_count(self) -> int:
        """Zones that carry image pixels (excludes a ceiling uplight)."""
        if self.is_ceiling and self.zone_count > 1:
            return self.zone_count - 1
        return self.zone_count


@dataclass
class SyncTarget:
    """One virtual LIGHT player: a group of channels rendered together."""

    target_id: str
    name: str
    kind: str
    lights: list[ResolvedLight] = field(default_factory=list)
    channels: list[LightChannel] = field(default_factory=list)
    is_matrix: bool = False
    is_ceiling: bool = False
    width: int | None = None
    height: int | None = None

    @property
    def serials(self) -> list[str]:
        return [light.serial for light in self.lights]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def normalize_serial(value: Any) -> str:
    """Normalize a serial / MAC to 12 lowercase hex characters.

    Accepts colon/dash/dot separated MACs, HA's separator-free serials
    and mixed-case input. Longer hex strings keep their last 12 digits
    (an EUI-64 contains the 48-bit address at its tail for our purposes);
    shorter strings are returned as-is so callers can reject them.
    """
    if value is None:
        return ""
    digits = _HEX_DIGITS.sub("", str(value).strip().lower())
    if len(digits) > 12:
        digits = digits[-12:]
    return digits


def slugify(text: str) -> str:
    """Turn a display name into a target-id safe slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    return slug or "target"


def _registry_rows(payload: Any) -> list[dict[str, Any]]:
    """Accept a registry payload as a list of dicts (HA's wire format)."""
    if isinstance(payload, Mapping):
        # Some test fixtures wrap rows in a key; accept that too.
        for key in ("devices", "entities", "areas", "result"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
        return []
    if isinstance(payload, Sequence):
        return [row for row in payload if isinstance(row, dict)]
    return []


def _is_lifx_device(device: Mapping[str, Any]) -> bool:
    manufacturer = str(device.get("manufacturer") or "")
    if "lifx" in manufacturer.lower():
        return True
    for identifier in device.get("identifiers") or []:
        # HA identifiers arrive as [domain, id] pairs (lists over JSON).
        if isinstance(identifier, Sequence) and not isinstance(identifier, str):
            if identifier and str(identifier[0]).lower() == "lifx":
                return True
        elif isinstance(identifier, str) and identifier.lower().startswith("lifx"):
            return True
    return False


def _device_serial(device: Mapping[str, Any]) -> str:
    for identifier in device.get("identifiers") or []:
        if (
            isinstance(identifier, Sequence)
            and not isinstance(identifier, str)
            and len(identifier) >= 2
            and str(identifier[0]).lower() == "lifx"
        ):
            serial = normalize_serial(identifier[1])
            if len(serial) == 12:
                return serial
    for connection in device.get("connections") or []:
        if (
            isinstance(connection, Sequence)
            and not isinstance(connection, str)
            and len(connection) >= 2
            and str(connection[0]).lower() == "mac"
        ):
            serial = normalize_serial(connection[1])
            if len(serial) == 12:
                return serial
    return ""


def _device_mac(device: Mapping[str, Any]) -> str | None:
    for connection in device.get("connections") or []:
        if (
            isinstance(connection, Sequence)
            and not isinstance(connection, str)
            and len(connection) >= 2
            and str(connection[0]).lower() == "mac"
        ):
            return normalize_serial(connection[1]) or None
    return None


def parse_ha_registries(
    devices: Any, entities: Any, areas: Any
) -> list[HaLifxLight]:
    """Extract LIFX lights from raw HA registry payloads.

    Devices are kept only when at least one ``light.*`` entity is bound
    to them (this filters out LIFX switches and other non-light gear).
    Area resolution prefers an entity-level area override, then the
    device's area.
    """
    area_names = {
        str(area.get("area_id") or area.get("id")): str(area.get("name") or "")
        for area in _registry_rows(areas)
        if (area.get("area_id") or area.get("id")) and area.get("name")
    }

    entities_by_device: dict[str, list[dict[str, Any]]] = {}
    for entity in _registry_rows(entities):
        device_id = entity.get("device_id")
        if device_id:
            entities_by_device.setdefault(str(device_id), []).append(entity)

    lights: list[HaLifxLight] = []
    for device in _registry_rows(devices):
        if not _is_lifx_device(device):
            continue
        device_id = str(device.get("id") or "")
        bound = entities_by_device.get(device_id, [])
        light_entities = [
            entity
            for entity in bound
            if str(entity.get("entity_id") or "").startswith("light.")
        ]
        if not light_entities:
            continue
        serial = _device_serial(device)
        if len(serial) != 12:
            _LOGGER.debug("Skipping LIFX device %s without a usable serial", device_id)
            continue

        area_id = None
        for entity in light_entities:
            if entity.get("area_id"):
                area_id = str(entity["area_id"])
                break
        if area_id is None and device.get("area_id"):
            area_id = str(device["area_id"])

        lights.append(
            HaLifxLight(
                serial=serial,
                name=str(device.get("name_by_user") or device.get("name") or serial),
                area_name=area_names.get(area_id) if area_id else None,
                entity_ids=tuple(
                    sorted(str(entity["entity_id"]) for entity in light_entities)
                ),
                device_id=device_id,
                mac=_device_mac(device),
            )
        )
    lights.sort(
        key=lambda light: (light.area_name is None, light.area_name or "", light.name)
    )
    return lights


def parse_ha_config_entries(entries: Any) -> dict[str, str]:
    """Map serial -> host from HA config entries (``config_entries/get``).

    Only entries with ``domain == "lifx"`` are considered. The serial
    comes from ``entry.data["serial"]`` (falling back to
    ``entry.unique_id``, which is also the serial for LIFX entries);
    the host from ``entry.data["host"]``. Entries without a usable
    serial or host are skipped.
    """
    hosts: dict[str, str] = {}
    for entry in _registry_rows(entries):
        if str(entry.get("domain") or "").lower() != "lifx":
            continue
        data = entry.get("data")
        if not isinstance(data, Mapping):
            continue
        serial = normalize_serial(data.get("serial") or entry.get("unique_id"))
        host = str(data.get("host") or "").strip()
        if len(serial) == 12 and host:
            hosts[serial] = host
    return hosts


def apply_ha_hosts(
    lights: Sequence[HaLifxLight], hosts_by_serial: Mapping[str, str]
) -> list[HaLifxLight]:
    """Combine registry-derived lights with config-entry hosts.

    Pure combine step: returns new HaLifxLight objects with ``host``
    filled from ``hosts_by_serial`` where a host exists for the serial.
    Lights without a matching entry keep ``host=None`` (the LAN sweep
    may still fill the gap at merge time).
    """
    from dataclasses import replace  # noqa: PLC0415 - tiny, keeps top imports lean

    combined: list[HaLifxLight] = []
    for light in lights:
        host = hosts_by_serial.get(light.serial)
        combined.append(replace(light, host=host) if host else light)
    return combined


# ---------------------------------------------------------------------------
# Merge: HA lights + LAN devices -> sync targets
# ---------------------------------------------------------------------------


def _resolve(
    ha_lights: Sequence[HaLifxLight], lan_devices: Sequence[LanDevice]
) -> tuple[list[ResolvedLight], dict[str, LanDevice]]:
    lan_by_serial = {device.serial: device for device in lan_devices}
    resolved: list[ResolvedLight] = []
    seen: set[str] = set()
    for ha_light in ha_lights:
        lan = lan_by_serial.get(ha_light.serial)
        seen.add(ha_light.serial)
        if (
            ha_light.host
            and lan is not None
            and lan.ip
            and lan.ip != ha_light.host
        ):
            # Cross-check only: HA's config-entry host is authoritative
            # (HA's own discovery maintains it), so keep it and merely
            # note that the LAN currently sees the light elsewhere.
            _LOGGER.debug(
                "LIFX %s: HA host %s differs from LAN IP %s; using HA host",
                ha_light.serial,
                ha_light.host,
                lan.ip,
            )
        resolved.append(
            ResolvedLight(
                serial=ha_light.serial,
                name=ha_light.name,
                area_name=ha_light.area_name,
                entity_ids=ha_light.entity_ids,
                # Precedence: HA config-entry host > LAN-discovered IP.
                ip=ha_light.host or (lan.ip if lan else None),
                is_matrix=bool(lan and lan.is_matrix),
                is_ceiling=bool(lan and lan.is_ceiling),
                width=lan.width if lan else None,
                height=lan.height if lan else None,
                product=lan.product if lan else None,
            )
        )
    for lan in lan_devices:
        if lan.serial in seen:
            continue
        resolved.append(
            ResolvedLight(
                serial=lan.serial,
                name=lan.name or lan.serial,
                area_name=None,
                ip=lan.ip,
                is_matrix=lan.is_matrix,
                is_ceiling=lan.is_ceiling,
                width=lan.width,
                height=lan.height,
                product=lan.product,
            )
        )
    return resolved, lan_by_serial


def _whole_light_channels(lights: Sequence[ResolvedLight]) -> list[LightChannel]:
    count = len(lights)
    return [
        LightChannel(
            channel_id=light.serial,
            position=(index / (count - 1)) if count > 1 else 0.0,
        )
        for index, light in enumerate(lights)
    ]


def _matrix_target(light: ResolvedLight) -> SyncTarget:
    width = light.width or 1
    height = light.height or 1
    channels: list[LightChannel] = []
    for zone_index in range(light.downlight_zone_count):
        column = zone_index % width
        channels.append(
            LightChannel(
                channel_id=f"{light.serial}:zone:{zone_index}",
                position=(column / (width - 1)) if width > 1 else 0.0,
                is_matrix_zone=True,
            )
        )
    return SyncTarget(
        target_id=f"matrix-{light.serial}",
        name=light.name,
        kind=const.TARGET_KIND_MATRIX,
        lights=[light],
        channels=channels,
        is_matrix=True,
        is_ceiling=light.is_ceiling,
        width=width,
        height=height,
    )


def merge_targets(
    ha_lights: Sequence[HaLifxLight],
    lan_devices: Sequence[LanDevice],
    include_lan_only: bool = True,
) -> list[SyncTarget]:
    """Build sync targets from HA lights and LAN devices.

    - one target per HA area containing LIFX lights (whole-light channels;
      matrix lights participate here as single channels too);
    - one individual target per matrix/ceiling light (zone channels; a
      ceiling's uplight zone is never included);
    - lights without an area go to an "Ungrouped" target;
    - in LAN-only mode (no HA lights at all), non-matrix lights group
      under a single "LIFX Lights" target instead.
    """
    lan_only_mode = not ha_lights
    if not include_lan_only:
        lan_devices = [
            device
            for device in lan_devices
            if device.serial in {light.serial for light in ha_lights}
        ]
    resolved, _lan = _resolve(ha_lights, lan_devices)
    if not resolved:
        return []

    targets: list[SyncTarget] = []

    if lan_only_mode:
        bulbs = [light for light in resolved if not light.is_matrix]
        if bulbs:
            targets.append(
                SyncTarget(
                    target_id=const.LAN_TARGET_ID,
                    name=const.LAN_TARGET_NAME,
                    kind=const.TARGET_KIND_ALL,
                    lights=bulbs,
                    channels=_whole_light_channels(bulbs),
                )
            )
    else:
        by_area: dict[str, list[ResolvedLight]] = {}
        ungrouped: list[ResolvedLight] = []
        for light in resolved:
            if light.area_name:
                by_area.setdefault(light.area_name, []).append(light)
            else:
                ungrouped.append(light)
        for area_name in sorted(by_area):
            lights = sorted(by_area[area_name], key=lambda item: item.name)
            targets.append(
                SyncTarget(
                    target_id=f"area-{slugify(area_name)}",
                    name=area_name,
                    kind=const.TARGET_KIND_AREA,
                    lights=lights,
                    channels=_whole_light_channels(lights),
                )
            )
        if ungrouped:
            ungrouped.sort(key=lambda item: item.name)
            targets.append(
                SyncTarget(
                    target_id=const.UNGROUPED_TARGET_ID,
                    name=const.UNGROUPED_TARGET_NAME,
                    kind=const.TARGET_KIND_UNGROUPED,
                    lights=ungrouped,
                    channels=_whole_light_channels(ungrouped),
                )
            )

    for light in sorted(
        (item for item in resolved if item.is_matrix), key=lambda item: item.name
    ):
        targets.append(_matrix_target(light))

    return targets


# ---------------------------------------------------------------------------
# Home Assistant (async, aiohttp imported lazily)
# ---------------------------------------------------------------------------


async def fetch_ha_lifx_lights(
    ha_url: str, token: str, timeout_s: float = 10.0
) -> list[HaLifxLight]:
    """Fetch and parse the HA registries over the WebSocket API.

    Raises RuntimeError with a human-readable message on any failure so
    the provider can log it and fall back to LAN discovery.
    """
    import aiohttp  # noqa: PLC0415 - lazy: plugin must import without aiohttp

    ws_url = ha_url.rstrip("/") + "/api/websocket"
    commands = {
        1: "config/device_registry/list",
        2: "config/entity_registry/list",
        3: "config/area_registry/list",
        # LIFX config entries carry data.host + data.serial: the primary
        # serial -> IP source (see module docstring).
        4: "config_entries/get",
    }
    # Extra payload fields per command id.
    command_extras: dict[int, dict[str, Any]] = {4: {"domain": "lifx"}}
    results: dict[int, Any] = {}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(ws_url, timeout=timeout_s) as ws:
                greeting = await ws.receive_json(timeout=timeout_s)
                if greeting.get("type") != "auth_required":
                    raise RuntimeError(f"Unexpected HA greeting: {greeting.get('type')}")
                await ws.send_json({"type": "auth", "access_token": token})
                auth_reply = await ws.receive_json(timeout=timeout_s)
                if auth_reply.get("type") != "auth_ok":
                    raise RuntimeError("Home Assistant rejected the access token")
                for command_id, command_type in commands.items():
                    payload = {"id": command_id, "type": command_type}
                    payload.update(command_extras.get(command_id, {}))
                    await ws.send_json(payload)
                while len(results) < len(commands):
                    message = await ws.receive_json(timeout=timeout_s)
                    if message.get("type") == "result" and message.get("id") in commands:
                        if not message.get("success", True):
                            if message["id"] == 4:
                                # Config entries are an enhancement
                                # (primary IPs); registries + LAN still
                                # work without them.
                                _LOGGER.warning(
                                    "HA config_entries/get failed: %s",
                                    message.get("error"),
                                )
                                results[4] = []
                                continue
                            raise RuntimeError(
                                f"HA command {commands[message['id']]} failed: "
                                f"{message.get('error')}"
                            )
                        results[message["id"]] = message.get("result")
    except RuntimeError:
        raise
    except Exception as err:  # noqa: BLE001 - normalized for the provider
        raise RuntimeError(f"Could not query Home Assistant at {ha_url}: {err}") from err

    lights = parse_ha_registries(results[1], results[2], results[3])
    hosts = parse_ha_config_entries(results.get(4))
    return apply_ha_hosts(lights, hosts)


# ---------------------------------------------------------------------------
# LAN discovery (async, lifx-async imported lazily)
# ---------------------------------------------------------------------------


async def discover_lan(timeout_s: float = const.LAN_DISCOVERY_TIMEOUT_S) -> list[LanDevice]:
    """Discover LIFX devices on the LAN with lifx-async.

    Uses the library's confirmed ``lifx.network.discovery.discover_devices``
    async generator (UDP broadcast; the library's ``lifx.discover`` also
    merges mDNS, but returns fully-constructed devices at higher cost).
    Each result is enriched via ``DiscoveredDevice.create_device()`` for
    label, product id and matrix geometry; enrichment failures leave the
    basic serial/IP record intact.
    """
    from lifx.network.discovery import discover_devices  # noqa: PLC0415

    devices: list[LanDevice] = []
    async for discovered in discover_devices(timeout=timeout_s):
        serial = normalize_serial(getattr(discovered, "serial", ""))
        if len(serial) != 12:
            continue
        lan = LanDevice(
            serial=serial,
            ip=str(getattr(discovered, "ip", "")),
            port=int(getattr(discovered, "port", const.LIFX_UDP_PORT)),
        )
        enriched = await _enrich_lan_device(discovered, lan)
        devices.append(enriched)
    devices.sort(key=lambda device: device.serial)
    return devices


async def _enrich_lan_device(discovered: Any, lan: LanDevice) -> LanDevice:
    """Best-effort enrichment of a discovered device (never raises)."""
    device: Any = None
    try:
        device = await discovered.create_device()
        if device is None:
            return lan
        name = None
        try:
            name = await device.get_label()
        except Exception:  # noqa: BLE001
            name = getattr(device, "label", None)

        product = None
        try:
            version = await device.get_version()
            product = getattr(version, "product", None)
        except Exception:  # noqa: BLE001
            product = None

        class_name = type(device).__name__
        is_ceiling = class_name == "CeilingLight"
        is_matrix = is_ceiling or class_name == "MatrixLight"
        # TODO(live-verify): Device.capabilities is a ProductInfo with a
        # has_matrix flag in lifx-async 8.0.0; the class-name check above
        # is the confirmed path, this getattr is a belt-and-braces extra.
        capabilities = getattr(device, "capabilities", None)
        if capabilities is not None and getattr(capabilities, "has_matrix", False):
            is_matrix = True

        width = height = None
        if is_matrix:
            try:
                chain = await device.get_device_chain()
                if chain:
                    width = int(chain[0].width)
                    height = int(chain[0].height)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("No tile chain for %s", lan.serial, exc_info=True)

        return LanDevice(
            serial=lan.serial,
            ip=lan.ip,
            name=str(name) if name else lan.name,
            product=int(product) if product is not None else None,
            is_matrix=is_matrix,
            is_ceiling=is_ceiling,
            width=width,
            height=height,
            port=lan.port,
        )
    except Exception:  # noqa: BLE001 - enrichment is optional by design
        _LOGGER.debug("Could not enrich LIFX device %s", lan.serial, exc_info=True)
        return lan
    finally:
        if device is not None:
            try:
                await device.close()
            except Exception:  # noqa: BLE001
                pass


async def discover_targets(
    ha_lights: Sequence[HaLifxLight],
    timeout_s: float = const.LAN_DISCOVERY_TIMEOUT_S,
    include_lan_only: bool = True,
) -> tuple[list[SyncTarget], list[LanDevice]]:
    """Convenience: run LAN discovery and merge with HA lights."""
    lan_devices = await discover_lan(timeout_s)
    return merge_targets(ha_lights, lan_devices, include_lan_only), lan_devices


__all__ = [
    "HaDiscoveryError",
    "HaLifxLight",
    "LanDevice",
    "ResolvedLight",
    "SyncTarget",
    "apply_ha_hosts",
    "discover_lan",
    "discover_targets",
    "fetch_ha_lifx_lights",
    "merge_targets",
    "normalize_serial",
    "parse_ha_config_entries",
    "parse_ha_registries",
    "slugify",
]
