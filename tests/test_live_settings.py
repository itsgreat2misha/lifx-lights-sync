"""
Regression tests for the live-apply settings list.

provider.py cannot be imported outside Music Assistant (it imports
music_assistant / aiosendspin), so the _LIVE_SETTING_KEYS declaration
is checked at source level: every playback setting that the bridge can
apply in place must be listed, or changing it in the UI silently
forces a full provider reload instead of applying live. This is the
exact bug that hid brightness_floor and debug_frame_log.
"""

from __future__ import annotations

import ast
from pathlib import Path

from lifx_lights_sync import constants as const
from lifx_lights_sync.settings import PluginSettings

PROVIDER_PY = Path(__file__).resolve().parent.parent / "lifx_lights_sync" / "provider.py"

# Playback settings the bridge applies without a reload: brightness,
# brightness floor and color mode go to the analyzer, latency and send
# rate to the bridge/sender, and the debug frame log is read from the
# provider's refreshed settings object on every render tick.
EXPECTED_LIVE_KEYS = {
    "CONF_BRIGHTNESS",
    "CONF_BRIGHTNESS_FLOOR",
    "CONF_COLOR_MODE",
    "CONF_LATENCY_MS",
    "CONF_SEND_RATE_HZ",
    "CONF_DEBUG_FRAME_LOG",
}


def _live_setting_key_names() -> set[str]:
    """Extract the const.* names listed in _LIVE_SETTING_KEYS."""
    tree = ast.parse(PROVIDER_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if any(
            isinstance(target, ast.Name) and target.id == "_LIVE_SETTING_KEYS"
            for target in node.targets
        ):
            if not isinstance(node.value, ast.Tuple):
                continue
            return {
                elt.attr
                for elt in node.value.elts
                if isinstance(elt, ast.Attribute)
                and isinstance(elt.value, ast.Name)
                and elt.value.id == "const"
            }
    raise AssertionError("_LIVE_SETTING_KEYS assignment not found in provider.py")


class TestLiveSettingKeys:
    """Tests for Live Setting Keys."""

    def test_all_live_applicable_keys_are_listed(self) -> None:
        """All live applicable keys are listed."""
        assert (_live_setting_key_names()) == (EXPECTED_LIVE_KEYS)

    def test_listed_names_exist_in_constants(self) -> None:
        """Listed names exist in constants."""
        for name in _live_setting_key_names():
            assert hasattr(const, name), name

    def test_floor_and_frame_log_settings_round_trip(self) -> None:
        """Floor and frame log settings round trip."""
        settings = PluginSettings.from_mapping(
            {const.CONF_BRIGHTNESS_FLOOR: 25, const.CONF_DEBUG_FRAME_LOG: True}
        )
        assert (settings.brightness_floor_percent) == (25)
        assert (settings.brightness_floor) == (0.25)
        assert settings.debug_frame_log
