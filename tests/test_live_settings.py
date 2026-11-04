"""
Regression tests for the live-apply settings list.

provider.py cannot be imported outside Music Assistant (it imports
music_assistant / aiosendspin), so the _LIVE_SETTING_KEYS declaration
is checked at source level: every playback setting that the bridge can
apply in place must be listed, or changing it in the UI silently
forces a full provider reload instead of applying live. This is the
exact bug that hid brightness_floor and debug_frame_log.
"""

import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
            return {
                elt.attr
                for elt in node.value.elts
                if isinstance(elt, ast.Attribute)
                and isinstance(elt.value, ast.Name)
                and elt.value.id == "const"
            }
    raise AssertionError("_LIVE_SETTING_KEYS assignment not found in provider.py")


class TestLiveSettingKeys(unittest.TestCase):
    def test_all_live_appliable_keys_are_listed(self) -> None:
        self.assertEqual(_live_setting_key_names(), EXPECTED_LIVE_KEYS)

    def test_listed_names_exist_in_constants(self) -> None:
        for name in _live_setting_key_names():
            self.assertTrue(hasattr(const, name), name)

    def test_floor_and_frame_log_settings_round_trip(self) -> None:
        settings = PluginSettings.from_mapping(
            {const.CONF_BRIGHTNESS_FLOOR: 25, const.CONF_DEBUG_FRAME_LOG: True}
        )
        self.assertEqual(settings.brightness_floor_percent, 25)
        self.assertEqual(settings.brightness_floor, 0.25)
        self.assertTrue(settings.debug_frame_log)


if __name__ == "__main__":
    unittest.main()
