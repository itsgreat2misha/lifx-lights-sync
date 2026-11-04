"""
Setup flow for the LIFX Lights Sync provider.

Follows the pattern of the Home Assistant provider's setup flow in
Music Assistant (``providers/hass/setup_flow.py``): credentials are
collected interactively here and persisted by the flow engine as the
instance's ``setup_data`` (encrypted at rest, never exposed over the
API), instead of living in the provider's options form. The provider
itself reads them back via ``get_setup_value`` (see provider.py).

The values collected are the *optional* direct Home Assistant
connection (URL + long-lived access token), used only when Music
Assistant's own Home Assistant provider is not configured — the
primary discovery path borrows that provider's connection and needs
no credentials at all, and LAN discovery needs none either. Both
fields may therefore be left empty; the flow then finishes with empty
values and the plugin relies on the hass provider / LAN.

This module is only ever loaded by Music Assistant's setup-flow
engine (server 2.10+); on older servers it is simply never imported,
and the provider keeps reading the same keys from its stored config
values, so existing installs are unaffected.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.models.setup_flow import SetupFlowError
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from . import constants as const
from .discovery import fetch_ha_lifx_lights
from .settings import validate_ha_credential_pair

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession


async def run_setup(session: SetupSession) -> None:
    """
    Run the LIFX Lights Sync setup flow.

    Shows one form for the optional direct Home Assistant connection.
    Submitted credentials are validated against the instance (a
    registry read) before they are persisted; a failed attempt
    re-shows the form with an error instead of dead-ending, mirroring
    the hass provider's flow. On reconfigure, the form is prefilled
    from the previously collected setup data; leaving the token field
    empty keeps the previously stored token.

    :param session: The setup session driving the flow.
    """
    prefill_url = str(session.context.setup_data.get(const.CONF_HA_URL) or "")
    # Only setup_data is a safe token prefill: it holds the decrypted
    # values from a previous run. (Legacy config values are NOT read
    # here on purpose - the provider resolves those itself through
    # get_setup_value's config fallback, which decrypts them properly.)
    prefill_token = str(session.context.setup_data.get(const.CONF_HA_TOKEN) or "")
    errors: dict[str, str] | None = None
    while True:
        values = await session.form(
            [
                ConfigEntry(
                    key=const.CONF_HA_URL,
                    type=ConfigEntryType.STRING,
                    required=False,
                    value=prefill_url,
                ),
                ConfigEntry(
                    key=const.CONF_HA_TOKEN,
                    type=ConfigEntryType.SECURE_STRING,
                    required=False,
                    advanced=True,
                ),
            ],
            step_id="user",
            errors=errors,
            last_step=True,
        )
        ha_url = str(values.get(const.CONF_HA_URL) or "").strip().rstrip("/")
        ha_token = str(values.get(const.CONF_HA_TOKEN) or "").strip() or prefill_token
        # re-prefill with what was just submitted, so a failed attempt
        # below doesn't revert the form back to the original values
        prefill_url = ha_url

        pair_error = validate_ha_credential_pair(ha_url, ha_token)
        if pair_error is not None:
            error_key = (
                const.CONF_HA_URL if pair_error == "ha_url_required" else const.CONF_HA_TOKEN
            )
            errors = {error_key: pair_error}
            continue

        if ha_url and ha_token:
            try:
                await fetch_ha_lifx_lights(ha_url, ha_token)
            except Exception:
                errors = {"base": "ha_connection_failed"}
                continue

        try:
            await session.finish({const.CONF_HA_URL: ha_url, const.CONF_HA_TOKEN: ha_token})
            return
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}


__all__ = ["run_setup"]
