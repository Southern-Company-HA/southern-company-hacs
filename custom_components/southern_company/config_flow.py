"""Config flow for Southern Company integration."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from southern_company_api.exceptions import (
    CantReachSouthernCompany,
    InvalidLogin,
    NoRequestTokenFound,
    NoScTokenFound,
)
from southern_company_api.parser import SouthernCompanyAPI
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
)

from .const import (
    ACCOUNT_TYPE_NICOR_GAS,
    ACCOUNT_TYPE_SOUTHERN_COMPANY,
    CONF_ACCOUNT_TYPE,
    DOMAIN,
)
from .parser_patch import EmailValidationRequired

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Required(
            CONF_ACCOUNT_TYPE, default=ACCOUNT_TYPE_SOUTHERN_COMPANY
        ): SelectSelector(
            SelectSelectorConfig(
                options=[
                    SelectOptionDict(
                        value=ACCOUNT_TYPE_SOUTHERN_COMPANY, label="Southern Company"
                    ),
                    SelectOptionDict(value=ACCOUNT_TYPE_NICOR_GAS, label="Nicor Gas"),
                ],
            )
        ),
    }
)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Southern Company."""

    VERSION = 1

    _reauth_entry_data: Mapping[str, Any] = {}

    async def _try_authenticate(
        self, user_input: Mapping[str, Any], errors: dict[str, str]
    ) -> None:
        """Attempt authentication, populating errors dict on failure."""
        account_type = user_input.get(CONF_ACCOUNT_TYPE, ACCOUNT_TYPE_SOUTHERN_COMPANY)

        if account_type == ACCOUNT_TYPE_NICOR_GAS:
            from southern_company_api.nicor_parser import NicorGasAPI  # noqa: PLC0415

            api = NicorGasAPI(
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
                aiohttp_client.async_get_clientsession(self.hass),
            )
            try:
                await api.connect()
            except (CantReachSouthernCompany, NoScTokenFound, NoRequestTokenFound):
                errors["base"] = "cannot_connect"
            except InvalidLogin:
                errors["base"] = "invalid_auth"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception during Nicor Gas auth")
                errors["base"] = "unknown"
        else:
            sca = SouthernCompanyAPI(
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
                aiohttp_client.async_get_clientsession(self.hass),
            )
            try:
                await sca.authenticate()
            except EmailValidationRequired:
                errors["base"] = "email_validation_required"
            except (CantReachSouthernCompany, NoScTokenFound, NoRequestTokenFound):
                errors["base"] = "cannot_connect"
            except InvalidLogin:
                errors["base"] = "invalid_auth"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception during Southern Company auth")
                errors["base"] = "unknown"

    async def async_authenticate(
        self, user_input: Mapping[str, Any], errors: dict[str, str]
    ) -> ConfigFlowResult | None:
        """Handle authentication for the initial user flow. Returns entry on success, None on failure."""
        await self._try_authenticate(user_input, errors)
        if errors:
            return None
        account_type = user_input.get(CONF_ACCOUNT_TYPE, ACCOUNT_TYPE_SOUTHERN_COMPANY)
        title = (
            "Nicor Gas"
            if account_type == ACCOUNT_TYPE_NICOR_GAS
            else "Southern Company Hacs"
        )
        return self.async_create_entry(title=title, data=dict(user_input))

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        _LOGGER.debug("Added user step")
        errors: dict[str, str] = {}
        if user_input is not None:
            self._async_abort_entries_match({CONF_USERNAME: user_input[CONF_USERNAME]})
            auth = await self.async_authenticate(user_input, errors)
            if auth is not None:
                _LOGGER.debug("FINISHED AUTH OF USER")
                return auth
        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle reauthentication."""
        _LOGGER.debug("Reauth?")
        self._reauth_entry_data = entry_data
        return await self.async_step_reauth_confirm(None)

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a flow initiated by reauthentication."""
        errors: dict[str, str] = {}
        if user_input is not None:
            await self._try_authenticate(user_input, errors)
            if not errors:
                entry = self._get_reauth_entry()
                self.hass.config_entries.async_update_entry(
                    entry, data={**entry.data, **user_input}
                )
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")
        reauth_data = self._reauth_entry_data or {}
        data_schema = vol.Schema(
            {
                vol.Required(
                    CONF_USERNAME,
                    default=reauth_data.get(CONF_USERNAME, ""),
                ): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Required(
                    CONF_ACCOUNT_TYPE,
                    default=reauth_data.get(
                        CONF_ACCOUNT_TYPE, ACCOUNT_TYPE_SOUTHERN_COMPANY
                    ),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(
                                value=ACCOUNT_TYPE_SOUTHERN_COMPANY,
                                label="Southern Company",
                            ),
                            SelectOptionDict(
                                value=ACCOUNT_TYPE_NICOR_GAS, label="Nicor Gas"
                            ),
                        ],
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=data_schema, errors=errors
        )
