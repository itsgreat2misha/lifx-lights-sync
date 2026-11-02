"""Home Assistant discovery via Music Assistant's own ``hass`` provider.

Music Assistant's Home Assistant provider (``music_assistant.providers
.hass.HomeAssistantProvider``) already holds a connected, authenticated
Home Assistant WebSocket client. Other providers borrow it — the same
pattern AI Radio / music_quiz use for HA-backed engines — so in the
common case this plugin needs **no HA credentials of its own**:

- ``hass_provider.get_device_registry()`` -> ``dict[device_id, Device]``
- ``hass_provider.get_area_registry()`` -> ``dict[area_id, Area]``
- ``hass_provider.get_entity_registry()`` ->
  ``Mapping[entity_id, HassRegistryEntity]`` — note this is the
  *abbreviated display* registry: values are NamedTuples with only
  ``platform`` / ``device_id`` / ``area_id``, and the entity_id is the
  mapping key, so the adapter injects it back into each row.
- ``hass_provider.hass.send_command("config_entries/get")`` — the
  generic WebSocket command sender on the provider's public
  ``HomeAssistantClient`` — returns all config entries; LIFX entries
  are filtered client-side (no domain kwarg is assumed) and parsed for
  their ``data.host`` / ``data.serial`` by the same pure parser the
  direct-URL/token path uses.

Everything here is duck-typed (no Music Assistant imports): registry
rows may arrive as mappings (hass_client TypedDicts), NamedTuples, or
plain objects, and are normalized to plain dicts before being handed
to discovery's pure parse functions.

Any failure — provider missing, unavailable, not connected, or a call
raising — is normalized to :class:`HaDiscoveryError` so the caller can
fall back to the next discovery source.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Sequence

from .discovery import (
    HaDiscoveryError,
    HaLifxLight,
    apply_ha_hosts,
    parse_ha_config_entries,
    parse_ha_registries,
)

_LOGGER = logging.getLogger(__name__)

_DEVICE_FIELDS = (
    "id",
    "identifiers",
    "connections",
    "manufacturer",
    "model",
    "name",
    "name_by_user",
    "area_id",
    "via_device_id",
)
_ENTITY_FIELDS = (
    "entity_id",
    "device_id",
    "area_id",
    "platform",
    "unique_id",
    "config_entry_id",
)
_AREA_FIELDS = ("area_id", "id", "name")
_ENTRY_FIELDS = ("domain", "data", "unique_id", "entry_id", "title")


def _row_to_dict(value: Any, fields: Sequence[str]) -> dict[str, Any] | None:
    """Normalize one registry row (mapping / NamedTuple / object) to a dict."""
    if value is None:
        return None
    if isinstance(value, Mapping):
        return dict(value)
    asdict = getattr(value, "_asdict", None)
    if callable(asdict):  # NamedTuple (e.g. HassRegistryEntity)
        return dict(asdict())
    row: dict[str, Any] = {}
    for field_name in fields:
        field_value = getattr(value, field_name, None)
        if field_value is not None:
            row[field_name] = field_value
    return row or None


def _rows_from_keyed(
    payload: Any, fields: Sequence[str], key_field: str | None = None
) -> list[dict[str, Any]]:
    """Normalize a registry payload into a list of plain dict rows.

    Accepts a mapping keyed by id (the hass provider's shape), a plain
    sequence of rows, or a mapping wrapping rows under a plural key.
    When ``key_field`` is given, a mapping key is injected into rows
    that lack that field (entity_id for the entity registry).
    """
    rows: list[dict[str, Any]] = []
    if isinstance(payload, Mapping):
        for key in ("devices", "entities", "areas", "result"):
            wrapped = payload.get(key)
            if isinstance(wrapped, (Mapping, Sequence)) and not isinstance(
                wrapped, (str, bytes)
            ):
                return _rows_from_keyed(wrapped, fields, key_field)
        items: Sequence[tuple[Any, Any]] = list(payload.items())
        for key, value in items:
            row = _row_to_dict(value, fields)
            if row is None:
                continue
            if key_field and not row.get(key_field):
                row[key_field] = key
            rows.append(row)
        return rows
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        for value in payload:
            row = _row_to_dict(value, fields)
            if row is not None:
                rows.append(row)
    return rows


async def fetch_ha_via_hass_provider(hass_provider: Any) -> list[HaLifxLight]:
    """Fetch LIFX lights through Music Assistant's Home Assistant provider.

    :param hass_provider: A loaded ``hass`` provider instance (duck-typed).
    :raises HaDiscoveryError: provider missing/unavailable/not connected,
        or any registry call failing.
    """
    if hass_provider is None:
        raise HaDiscoveryError("Home Assistant provider is not loaded")
    if getattr(hass_provider, "available", True) is False:
        raise HaDiscoveryError("Home Assistant provider is not available")
    hass_client = getattr(hass_provider, "hass", None)
    if hass_client is None:
        raise HaDiscoveryError("Home Assistant provider has no HA client")
    if getattr(hass_client, "connected", True) is False:
        raise HaDiscoveryError("Home Assistant provider is not connected to HA")

    try:
        devices = await hass_provider.get_device_registry()
        entities = await hass_provider.get_entity_registry()
        areas = await hass_provider.get_area_registry()
    except HaDiscoveryError:
        raise
    except Exception as err:  # noqa: BLE001 - normalized for the caller
        raise HaDiscoveryError(
            f"Could not read Home Assistant registries via the hass provider: {err}"
        ) from err

    lights = parse_ha_registries(
        _rows_from_keyed(devices, _DEVICE_FIELDS),
        _rows_from_keyed(entities, _ENTITY_FIELDS, key_field="entity_id"),
        _rows_from_keyed(areas, _AREA_FIELDS),
    )

    # Config entries carry the primary serial -> host mapping. Like the
    # direct path, a failure here is non-fatal: registries + LAN still
    # work, the lights just fall back to LAN-resolved IPs.
    hosts: dict[str, str] = {}
    try:
        entries = await hass_client.send_command("config_entries/get")
        hosts = parse_ha_config_entries(_rows_from_keyed(entries, _ENTRY_FIELDS))
    except Exception:  # noqa: BLE001 - enhancement only, see above
        _LOGGER.warning(
            "config_entries/get via the hass provider failed; "
            "continuing without HA config-entry hosts",
            exc_info=True,
        )
    if hosts:
        lights = apply_ha_hosts(lights, hosts)
    _LOGGER.debug(
        "hass provider discovery: %d LIFX lights, %d with HA hosts",
        len(lights),
        len(hosts),
    )
    return lights


__all__ = ["fetch_ha_via_hass_provider"]
