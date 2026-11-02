"""LIFX Lights Sync plugin for Music Assistant.

Syncs LIFX lights to music using the Sendspin visualizer stream, modeled
on the built-in Hue Lights Sync plugin (hue_entertainment).

This module is intentionally import-light: it must be importable without
Music Assistant installed (the unit tests rely on that, and Music
Assistant itself imports the package before calling :func:`setup`).
The provider class is therefore imported lazily, in an executor thread,
inside :func:`setup` — the same pattern the Hue plugin uses.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from music_assistant.common.models.provider import Provider
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models.provider import ProviderManifest

# A plugin provider exposes no extra player/music features of its own;
# the LIGHT players it registers come from the Sendspin provider.
SUPPORTED_FEATURES: set = set()


def _import_provider_class() -> type:
    from .provider import LifxLightsSyncProvider

    return LifxLightsSyncProvider


async def setup(
    mass: "MusicAssistant", manifest: "ProviderManifest", config: Any
) -> "Provider":
    """Initialize the provider instance (called by Music Assistant)."""
    loop = asyncio.get_running_loop()
    provider_class = await loop.run_in_executor(None, _import_provider_class)
    return provider_class(mass, manifest, config)


async def get_config_entries(
    mass: "MusicAssistant",
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, Any] | None = None,
) -> tuple:
    """Return the config entries without instantiating the provider."""
    loop = asyncio.get_running_loop()
    provider_class = await loop.run_in_executor(None, _import_provider_class)
    # The Hue plugin delegates to an unbound helper on the provider class;
    # our entries are instance-independent, so build them on a bare
    # instance-free call through the class's static construction path.
    # TODO(live-verify): confirm MA calls the module-level
    # get_config_entries with this signature for plugin providers.
    provider = object.__new__(provider_class)
    return await provider_class.get_config_entries(provider, action, values)


__all__ = ["SUPPORTED_FEATURES", "get_config_entries", "setup"]
