"""
LIFX Lights Sync Plugin for Music Assistant.

Syncs LIFX lights to music using the Sendspin visualization pipeline —
each sync target (a Home Assistant area, or a single matrix/ceiling
light) appears as a virtual LIGHT player in Music Assistant. Playing
music to the player makes the lights react to the music in real time.

Modeled on the built-in Hue Lights Sync plugin (hue_entertainment);
this module follows its exact shape for the installed Music Assistant
version.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.provider import ProviderManifest

SUPPORTED_FEATURES: set[ProviderFeature] = set()


async def setup(
    mass: MusicAssistant,
    manifest: ProviderManifest,
    config: ProviderConfig,
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    from .provider import LifxLightsSyncProvider  # noqa: PLC0415

    return LifxLightsSyncProvider(mass, manifest, config, SUPPORTED_FEATURES)
