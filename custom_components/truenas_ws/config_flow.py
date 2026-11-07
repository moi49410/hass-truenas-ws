"""Config flow for the TrueNAS SCALE (WebSocket) integration."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_API_KEY, CONF_HOST
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import TrueNASAuthError, TrueNASClient, TrueNASConnectionError, TrueNASError
from .const import (
    CONF_SCAN_INTERVAL,
    CONF_VERIFY_SSL,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
)
from .coordinator import TrueNASConfigEntry

_LOGGER = logging.getLogger(__name__)

PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


async def _validate(
    hass: HomeAssistant, host: str, api_key: str, verify_ssl: bool
) -> dict[str, Any]:
    """Log in and return system information."""
    client = TrueNASClient(
        async_get_clientsession(hass, verify_ssl=verify_ssl), host, api_key, verify_ssl
    )
    try:
        await client.connect()
        info = await client.call("system.info") or {}
        try:
            host_id = await client.call("system.host_id")
        except TrueNASError:
            host_id = None
        info["_unique"] = host_id or info.get("system_serial") or info.get("hostname")
        return info
    finally:
        await client.close()


class TrueNASConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up a TrueNAS system."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for host and API key."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                info = await _validate(
                    self.hass,
                    user_input[CONF_HOST],
                    user_input[CONF_API_KEY],
                    user_input[CONF_VERIFY_SSL],
                )
            except TrueNASAuthError:
                errors["base"] = "invalid_auth"
            except TrueNASConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during TrueNAS setup")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(str(info["_unique"]))
                self._abort_if_unique_id_configured(
                    updates={CONF_HOST: user_input[CONF_HOST]}
                )
                return self.async_create_entry(
                    title=info.get("hostname") or user_input[CONF_HOST],
                    data=user_input,
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST): str,
                vol.Required(CONF_API_KEY): PASSWORD,
                vol.Required(CONF_VERIFY_SSL, default=DEFAULT_VERIFY_SSL): bool,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start re-authentication after the API key was refused."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new API key."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            try:
                await _validate(
                    self.hass,
                    entry.data[CONF_HOST],
                    user_input[CONF_API_KEY],
                    entry.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
                )
            except TrueNASAuthError:
                errors["base"] = "invalid_auth"
            except TrueNASConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during TrueNAS reauth")
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_API_KEY: user_input[CONF_API_KEY]}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_API_KEY): PASSWORD}),
            description_placeholders={"host": entry.data[CONF_HOST]},
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: TrueNASConfigEntry) -> TrueNASOptionsFlow:
        """Return the options flow."""
        return TrueNASOptionsFlow()


class TrueNASOptionsFlow(OptionsFlowWithReload):
    """Polling interval."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=self.config_entry.options.get(
                        CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                    ),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL,
                        max=600,
                        step=5,
                        unit_of_measurement="s",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
