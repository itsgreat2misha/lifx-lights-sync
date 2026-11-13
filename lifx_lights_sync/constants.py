"""
Constants for the LIFX Lights Sync plugin.

This module is intentionally free of Music Assistant imports so it can be
imported (and unit-tested) outside a running Music Assistant instance.
"""

from __future__ import annotations

from typing import Final

DOMAIN = "lifx_lights_sync"

# --- Config entry keys (mirror the Hue Lights Sync plugin's naming style) ---
CONF_DISCOVERY_SOURCE = "discovery_source"
CONF_HA_URL = "ha_url"
CONF_HA_TOKEN = "ha_token"  # noqa: S105 - a config key name, not a credential
CONF_BRIGHTNESS = "brightness"
CONF_BRIGHTNESS_FLOOR = "brightness_floor"
CONF_COLOR_MODE = "color_mode"
CONF_LATENCY_MS = "latency_ms"
CONF_SEND_RATE_HZ = "send_rate_hz"
CONF_DEBUG_FRAME_LOG = "debug_frame_log"

# --- Discovery sources ---
SOURCE_AUTO = "auto"  # MA hass provider, then direct HA, then LAN
SOURCE_HASS = "hass"  # Music Assistant's own Home Assistant provider only
SOURCE_HA = "ha"  # Direct Home Assistant URL+token only
SOURCE_LAN = "lan"  # LAN only, no Home Assistant involved
DISCOVERY_SOURCES = (SOURCE_AUTO, SOURCE_HASS, SOURCE_HA, SOURCE_LAN)
DEFAULT_DISCOVERY_SOURCE = SOURCE_AUTO

# --- Defaults ---
DEFAULT_HA_URL = ""
DEFAULT_HA_TOKEN = ""
DEFAULT_BRIGHTNESS = 80  # percent (upper bound: the peaks)
DEFAULT_BRIGHTNESS_FLOOR = 0  # percent (lower bound: rest level; 0 = per-mode default)
DEFAULT_COLOR_MODE = "smooth"
DEFAULT_LATENCY_MS = 0
DEFAULT_SEND_RATE_HZ = 20
DEFAULT_DEBUG_FRAME_LOG = False

# Brightness is configured as a percentage so the UI matches Music
# Assistant conventions; internally the analyzer works with 0.0-1.0.
MIN_BRIGHTNESS = 1
MAX_BRIGHTNESS = 100
MIN_BRIGHTNESS_FLOOR = 0
MAX_BRIGHTNESS_FLOOR = 99
MIN_LATENCY_MS = 0
MAX_LATENCY_MS = 2_000
MIN_SEND_RATE_HZ = 1
MAX_SEND_RATE_HZ = 30

# --- Analyzer modes (presets ported from the Hue Lights Sync analyzer) ---
MODE_SMOOTH = "smooth"
MODE_AMBIENT = "ambient"
MODE_FLASHING = "flashing"
MODE_ENERGETIC = "energetic"
MODE_BASS = "bass"
COLOR_MODES = (MODE_SMOOTH, MODE_AMBIENT, MODE_FLASHING, MODE_ENERGETIC, MODE_BASS)

# --- Sendspin visualizer request (mirrors hue_entertainment @73c3e59) ---
# The Sendspin server computes the spectrum; we only declare what we want.
VISUALIZER_RATE_MAX = 20  # frames per second requested from the server
VISUALIZER_TYPES = ("beat", "peak", "spectrum")
SPECTRUM_N_DISP_BINS = 17
SPECTRUM_SCALE: Final = "mel"
SPECTRUM_F_MIN = 20
SPECTRUM_F_MAX = 20_000
VISUALIZER_BUFFER_CAPACITY = 2048

# Sendspin bridge role identifiers. The canonical values live in
# ``music_assistant.providers.sendspin.bridge_role``; bridge.py imports them
# from there when running inside Music Assistant and only falls back to
# these literals when that module is unavailable.
VISUALIZER_BRIDGE_ROLE_ID = "visualizer@_bridge"
COLOR_BRIDGE_ROLE_ID = "color@_bridge"

# --- Render loop ---
RENDER_FPS = 50  # local render/send tick, independent of visualizer rate
STOP_DEBOUNCE_S = 2.0  # keep streaming briefly across track gaps

# --- LIFX protocol ---
LIFX_UDP_PORT = 56700
DEFAULT_KELVIN = 3500  # kelvin attached to HSBK colors built from RGB
LAN_DISCOVERY_TIMEOUT_S = 6.0
SWEEP_TIMEOUT_S = 5.0

# --- Sync target kinds / ids ---
TARGET_KIND_AREA = "area"
TARGET_KIND_MATRIX = "matrix"
TARGET_KIND_UNGROUPED = "ungrouped"
TARGET_KIND_ALL = "all"
UNGROUPED_TARGET_ID = "ungrouped"
UNGROUPED_TARGET_NAME = "Ungrouped"
LAN_TARGET_ID = "lifx-lights"
LAN_TARGET_NAME = "LIFX Lights"

__all__ = [
    "COLOR_BRIDGE_ROLE_ID",
    "COLOR_MODES",
    "CONF_BRIGHTNESS",
    "CONF_COLOR_MODE",
    "CONF_DEBUG_FRAME_LOG",
    "CONF_DISCOVERY_SOURCE",
    "CONF_HA_TOKEN",
    "CONF_HA_URL",
    "CONF_LATENCY_MS",
    "CONF_SEND_RATE_HZ",
    "DEFAULT_BRIGHTNESS",
    "DEFAULT_COLOR_MODE",
    "DEFAULT_DEBUG_FRAME_LOG",
    "DEFAULT_DISCOVERY_SOURCE",
    "DEFAULT_HA_TOKEN",
    "DEFAULT_HA_URL",
    "DEFAULT_KELVIN",
    "DEFAULT_LATENCY_MS",
    "DEFAULT_SEND_RATE_HZ",
    "DISCOVERY_SOURCES",
    "DOMAIN",
    "LAN_DISCOVERY_TIMEOUT_S",
    "LAN_TARGET_ID",
    "LAN_TARGET_NAME",
    "LIFX_UDP_PORT",
    "MAX_BRIGHTNESS",
    "MAX_LATENCY_MS",
    "MAX_SEND_RATE_HZ",
    "MIN_BRIGHTNESS",
    "MIN_LATENCY_MS",
    "MIN_SEND_RATE_HZ",
    "MODE_AMBIENT",
    "MODE_BASS",
    "MODE_ENERGETIC",
    "MODE_FLASHING",
    "MODE_SMOOTH",
    "RENDER_FPS",
    "SOURCE_AUTO",
    "SOURCE_HA",
    "SOURCE_HASS",
    "SOURCE_LAN",
    "SPECTRUM_F_MAX",
    "SPECTRUM_F_MIN",
    "SPECTRUM_N_DISP_BINS",
    "SPECTRUM_SCALE",
    "STOP_DEBOUNCE_S",
    "SWEEP_TIMEOUT_S",
    "TARGET_KIND_ALL",
    "TARGET_KIND_AREA",
    "TARGET_KIND_MATRIX",
    "TARGET_KIND_UNGROUPED",
    "UNGROUPED_TARGET_ID",
    "UNGROUPED_TARGET_NAME",
    "VISUALIZER_BRIDGE_ROLE_ID",
    "VISUALIZER_BUFFER_CAPACITY",
    "VISUALIZER_RATE_MAX",
    "VISUALIZER_TYPES",
]
