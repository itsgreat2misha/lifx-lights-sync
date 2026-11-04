"""
Tests for the setup-flow credential handling.

The setup flow itself (lifx_lights_sync/setup_flow.py) can only run
inside Music Assistant's flow engine, so it is checked at source
level, like provider.py in test_live_settings.py. The decision logic
it relies on - credential pair validation and the setup-data overlay
in settings.py - is pure and unit-tested directly here.
"""

import ast
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lifx_lights_sync import constants as const
from lifx_lights_sync.settings import (
    PluginSettings,
    resolve_ha_credentials,
    validate_ha_credential_pair,
)

PACKAGE_DIR = Path(__file__).resolve().parent.parent / "lifx_lights_sync"
SETUP_FLOW_PY = PACKAGE_DIR / "setup_flow.py"
PROVIDER_PY = PACKAGE_DIR / "provider.py"
MANIFEST_JSON = PACKAGE_DIR / "manifest.json"


class TestCredentialPairValidation(unittest.TestCase):
    def test_both_empty_is_valid(self) -> None:
        # No direct connection configured: rely on the hass provider / LAN.
        self.assertIsNone(validate_ha_credential_pair("", ""))

    def test_complete_pair_is_valid(self) -> None:
        self.assertIsNone(validate_ha_credential_pair("http://homeassistant.local:8123", "token"))

    def test_token_without_url(self) -> None:
        self.assertEqual(validate_ha_credential_pair("", "token"), "ha_url_required")

    def test_url_without_token(self) -> None:
        self.assertEqual(
            validate_ha_credential_pair("http://homeassistant.local:8123", ""),
            "ha_token_required",
        )


class TestResolveHaCredentials(unittest.TestCase):
    def _base(self) -> PluginSettings:
        return PluginSettings.from_mapping(
            {const.CONF_HA_URL: "http://legacy.local:8123/", const.CONF_HA_TOKEN: "legacytoken"}
        )

    def test_no_setup_values_keeps_config(self) -> None:
        # Setup flow never ran: legacy options-form values are used as-is.
        settings = resolve_ha_credentials(self._base(), None, None)
        self.assertEqual(settings.ha_url, "http://legacy.local:8123")
        self.assertEqual(settings.ha_token, "legacytoken")

    def test_setup_values_win(self) -> None:
        settings = resolve_ha_credentials(self._base(), "http://new.local:8123/", "newtoken")
        self.assertEqual(settings.ha_url, "http://new.local:8123")
        self.assertEqual(settings.ha_token, "newtoken")
        self.assertTrue(settings.ha_configured)

    def test_setup_empty_string_clears(self) -> None:
        # Submitting the flow with empty fields clears the credentials.
        settings = resolve_ha_credentials(self._base(), "", "")
        self.assertEqual(settings.ha_url, "")
        self.assertEqual(settings.ha_token, "")
        self.assertFalse(settings.ha_configured)

    def test_partial_setup_overlay(self) -> None:
        settings = resolve_ha_credentials(self._base(), "http://new.local:8123", None)
        self.assertEqual(settings.ha_url, "http://new.local:8123")
        self.assertEqual(settings.ha_token, "legacytoken")


def _module_source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _top_level_function_names(path: Path) -> set[str]:
    tree = ast.parse(_module_source(path))
    return {
        node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


class TestSetupFlowModule(unittest.TestCase):
    def test_run_setup_exists_and_is_async(self) -> None:
        tree = ast.parse(_module_source(SETUP_FLOW_PY))
        funcs = {node.name: node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)}
        self.assertIn("run_setup", funcs)

    def test_flow_uses_session_form_and_finish(self) -> None:
        source = _module_source(SETUP_FLOW_PY)
        self.assertIn("session.form(", source)
        self.assertIn("session.finish(", source)
        self.assertIn('step_id="user"', source)

    def test_flow_collects_the_credential_keys(self) -> None:
        source = _module_source(SETUP_FLOW_PY)
        self.assertIn("const.CONF_HA_URL", source)
        self.assertIn("const.CONF_HA_TOKEN", source)
        self.assertIn("ConfigEntryType.SECURE_STRING", source)


class TestProviderShape(unittest.TestCase):
    def test_credentials_no_longer_declared_as_options(self) -> None:
        source = _module_source(PROVIDER_PY)
        self.assertNotIn("key=const.CONF_HA_URL", source)
        self.assertNotIn("key=const.CONF_HA_TOKEN", source)

    def test_provider_reads_setup_values_with_fallback(self) -> None:
        source = _module_source(PROVIDER_PY)
        self.assertIn("get_setup_value", source)
        self.assertIn("resolve_ha_credentials", source)

    def test_manifest_stage_is_alpha(self) -> None:
        manifest = json.loads(MANIFEST_JSON.read_text(encoding="utf-8"))
        self.assertEqual(manifest["stage"], "alpha")


if __name__ == "__main__":
    unittest.main()
