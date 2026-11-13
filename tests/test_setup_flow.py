"""
Tests for the setup-flow credential handling.

The setup flow itself (lifx_lights_sync/setup_flow.py) can only run
inside Music Assistant's flow engine, so it is checked at source
level, like provider.py in test_live_settings.py. The decision logic
it relies on - credential pair validation and the setup-data overlay
in settings.py - is pure and unit-tested directly here.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

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


class TestCredentialPairValidation:
    """Tests for Credential Pair Validation."""

    def test_both_empty_is_valid(self) -> None:
        # No direct connection configured: rely on the hass provider / LAN.
        """Both empty is valid."""
        assert (validate_ha_credential_pair("", "")) is None

    def test_complete_pair_is_valid(self) -> None:
        """Complete pair is valid."""
        assert (validate_ha_credential_pair("http://homeassistant.local:8123", "token")) is None

    def test_token_without_url(self) -> None:
        """Token without url."""
        assert (validate_ha_credential_pair("", "token")) == ("ha_url_required")

    def test_url_without_token(self) -> None:
        """Url without token."""
        assert (validate_ha_credential_pair("http://homeassistant.local:8123", "")) == (
            "ha_token_required"
        )


class TestResolveHaCredentials:
    """Tests for Resolve Ha Credentials."""

    def _base(self) -> PluginSettings:
        """Build the base settings carrying legacy config credentials."""
        return PluginSettings.from_mapping(
            {const.CONF_HA_URL: "http://legacy.local:8123/", const.CONF_HA_TOKEN: "legacytoken"}
        )

    def test_no_setup_values_keeps_config(self) -> None:
        # Setup flow never ran: legacy options-form values are used as-is.
        """No setup values keeps config."""
        settings = resolve_ha_credentials(self._base(), None, None)
        assert (settings.ha_url) == ("http://legacy.local:8123")
        assert (settings.ha_token) == ("legacytoken")

    def test_setup_values_win(self) -> None:
        """Setup values win."""
        settings = resolve_ha_credentials(self._base(), "http://new.local:8123/", "newtoken")
        assert (settings.ha_url) == ("http://new.local:8123")
        assert (settings.ha_token) == ("newtoken")
        assert settings.ha_configured

    def test_setup_empty_string_clears(self) -> None:
        # Submitting the flow with empty fields clears the credentials.
        """Setup empty string clears."""
        settings = resolve_ha_credentials(self._base(), "", "")
        assert (settings.ha_url) == ("")
        assert (settings.ha_token) == ("")
        assert not (settings.ha_configured)

    def test_partial_setup_overlay(self) -> None:
        """Partial setup overlay."""
        settings = resolve_ha_credentials(self._base(), "http://new.local:8123", None)
        assert (settings.ha_url) == ("http://new.local:8123")
        assert (settings.ha_token) == ("legacytoken")


def _module_source(path: Path) -> str:
    """Read a module file and return its source text."""
    return path.read_text(encoding="utf-8")


def _top_level_function_names(path: Path) -> set[str]:
    """Top level function names."""
    tree = ast.parse(_module_source(path))
    return {
        node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


class TestSetupFlowModule:
    """Tests for Setup Flow Module."""

    def test_run_setup_exists_and_is_async(self) -> None:
        """Run setup exists and is async."""
        tree = ast.parse(_module_source(SETUP_FLOW_PY))
        funcs = {node.name: node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)}
        assert ("run_setup") in (funcs)

    def test_flow_uses_session_form_and_finish(self) -> None:
        """Flow uses session form and finish."""
        source = _module_source(SETUP_FLOW_PY)
        assert ("session.form(") in (source)
        assert ("session.finish(") in (source)
        assert ('step_id="user"') in (source)

    def test_flow_collects_the_credential_keys(self) -> None:
        """Flow collects the credential keys."""
        source = _module_source(SETUP_FLOW_PY)
        assert ("const.CONF_HA_URL") in (source)
        assert ("const.CONF_HA_TOKEN") in (source)
        assert ("ConfigEntryType.SECURE_STRING") in (source)


class TestProviderShape:
    """Tests for Provider Shape."""

    def test_credentials_no_longer_declared_as_options(self) -> None:
        """Credentials no longer declared as options."""
        source = _module_source(PROVIDER_PY)
        assert ("key=const.CONF_HA_URL") not in (source)
        assert ("key=const.CONF_HA_TOKEN") not in (source)

    def test_provider_reads_setup_values_with_fallback(self) -> None:
        """Provider reads setup values with fallback."""
        source = _module_source(PROVIDER_PY)
        assert ("get_setup_value") in (source)
        assert ("resolve_ha_credentials") in (source)

    def test_manifest_stage_is_alpha(self) -> None:
        """Manifest stage is alpha."""
        manifest = json.loads(MANIFEST_JSON.read_text(encoding="utf-8"))
        assert (manifest["stage"]) == ("alpha")
