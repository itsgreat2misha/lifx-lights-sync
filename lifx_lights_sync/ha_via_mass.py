"""
Home Assistant discovery via Music Assistant's own ``hass`` provider.

When Music Assistant already runs its Home Assistant provider, this
plugin reuses that provider through its public, docstringed registry
accessors — the supported cross-provider pattern in-tree: the official
``sendspin`` provider likewise soft-optionally reaches the hass
provider via ``mass.get_provider("hass")`` and calls its public
methods. In the common case this plugin therefore needs **no HA
credentials of its own**:

- ``hass_provider.get_device_registry()`` -> ``dict[device_id, Device]``
- ``hass_provider.get_area_registry()`` -> ``dict[area_id, Area]``
- ``hass_provider.get_entity_registry()`` ->
  ``Mapping[entity_id, HassRegistryEntity]`` — note this is the
  *abbreviated display* registry: values are NamedTuples with only
  ``platform`` / ``device_id`` / ``area_id``, and the entity_id is the
  mapping key, so the adapter injects it back into each row.

This path deliberately goes no further: it does not issue raw
WebSocket commands on the provider's client, because the hass provider
exposes no public wrapper for HA config-entry data — and HA's stored
per-entry ``host`` would not survive that payload anyway. Lights found
here therefore carry no host; their IPs come from LAN discovery and
the unicast sweep at merge time (see ``discovery``). Only the direct
URL + token path (``discovery.fetch_ha_lifx_lights``), which talks to
HA with the user's own credentials, parses config entries
opportunistically.

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
from collections.abc import Mapping, Sequence
from typing import Any

from .discovery import (
    HaDiscoveryError,
    HaLifxLight,
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
    """
    Normalize a registry payload into a list of plain dict rows.

    Accepts a mapping keyed by id (the hass provider's shape), a plain
    sequence of rows, or a mapping wrapping rows under a plural key.
    When ``key_field`` is given, a mapping key is injected into rows
    that lack that field (entity_id for the entity registry).
    """
    rows: list[dict[str, Any]] = []
    if isinstance(payload, Mapping):
        for key in ("devices", "entities", "areas", "result"):
            wrapped = payload.get(key)
            if isinstance(wrapped, (Mapping, Sequence)) and not isinstance(wrapped, (str, bytes)):
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
    """
    Fetch LIFX lights through Music Assistant's Home Assistant provider.

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
    except Exception as err:
        raise HaDiscoveryError(
            f"Could not read Home Assistant registries via the hass provider: {err}"
        ) from err

    lights = parse_ha_registries(
        _rows_from_keyed(devices, _DEVICE_FIELDS),
        _rows_from_keyed(entities, _ENTITY_FIELDS, key_field="entity_id"),
        _rows_from_keyed(areas, _AREA_FIELDS),
    )
    _LOGGER.debug("hass provider discovery: %d LIFX lights", len(lights))
    return lights


__all__ = ["fetch_ha_via_hass_provider"]
