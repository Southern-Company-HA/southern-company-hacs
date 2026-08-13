"""The Southern Company integration."""

from __future__ import annotations

import logging
import time

from southern_company_api.exceptions import (
    CantReachSouthernCompany,
    InvalidLogin,
    NoRequestTokenFound,
    NoScTokenFound,
)
from southern_company_api.parser import SouthernCompanyAPI

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import aiohttp_client

from . import parser_patch
from .const import (
    ACCOUNT_TYPE_NICOR_GAS,
    CONF_ACCOUNT_TYPE,
    DOMAIN,
    EMAIL_VALIDATION_URL,
)
from .coordinator import NicorGasCoordinator, SouthernCompanyCoordinator
from .statistics import async_import_nicor_statistics

_LOGGER = logging.getLogger(__name__)

parser_patch.apply()

PLATFORMS = [Platform.SENSOR]
failures: dict[str, float] = {}


async def _async_import_nicor_statistics_safe(
    hass: HomeAssistant, data: object
) -> None:
    """Import Nicor Gas statistics, logging (not raising) on failure."""
    try:
        await async_import_nicor_statistics(hass, data)  # type: ignore[arg-type]
    except Exception as err:
        _LOGGER.warning("Failed to import Nicor Gas statistics: %s", err)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Southern Company from a config entry."""
    if entry.entry_id in failures:
        if not time.time() - failures[entry.entry_id] > 600:
            raise ConfigEntryNotReady(
                "Delaying retrying to prevent robot detection. You may need to restart to fix this."
            )
    hass.data.setdefault(DOMAIN, {})
    session = aiohttp_client.async_create_clientsession(hass)

    account_type = entry.data.get(CONF_ACCOUNT_TYPE, "southern_company")

    if account_type == ACCOUNT_TYPE_NICOR_GAS:
        from southern_company_api.nicor_parser import NicorGasAPI  # noqa: PLC0415

        api: NicorGasAPI = NicorGasAPI(
            entry.data[CONF_USERNAME],
            entry.data[CONF_PASSWORD],
            session,
        )
        try:
            await api.connect()
        except CantReachSouthernCompany as err:
            raise ConfigEntryNotReady("Can not connect to Nicor Gas") from err
        except (NoScTokenFound, NoRequestTokenFound) as err:
            failures[entry.entry_id] = time.time()
            raise ConfigEntryNotReady(
                "Token not found in Nicor Gas response. Please double check your credentials or open an issue"
            ) from err
        except InvalidLogin as err:
            raise ConfigEntryAuthFailed("Login incorrect") from err
        coordinator: NicorGasCoordinator | SouthernCompanyCoordinator = (
            NicorGasCoordinator(hass, api)
        )
    else:
        sca = SouthernCompanyAPI(
            entry.data[CONF_USERNAME],
            entry.data[CONF_PASSWORD],
            session,
        )
        try:
            await sca.authenticate()
        except CantReachSouthernCompany as err:
            raise ConfigEntryNotReady(
                "Can not connect to the southern company"
            ) from err
        except (NoScTokenFound, NoRequestTokenFound) as err:
            failures[entry.entry_id] = time.time()
            raise ConfigEntryNotReady(
                "Token not found in southern company response. Please double check your credentials or open an issue"
            ) from err
        except InvalidLogin as err:
            _LOGGER.warning(
                "Southern Company auth failed with InvalidLogin: %s. "
                "This may be caused by bot detection (Imperva) on your "
                "network. Try visiting southernco.com from a browser on "
                "the same network as HA, then retry.",
                err,
            )
            raise ConfigEntryAuthFailed(
                f"Login failed. If you have not validated your email with Southern Company, "
                f"visit {EMAIL_VALIDATION_URL} to do so."
            ) from err
        coordinator = SouthernCompanyCoordinator(hass, sca)

    if entry.entry_id in failures:
        failures.pop(entry.entry_id)

    await coordinator.async_config_entry_first_refresh()
    hass.data[DOMAIN][entry.entry_id] = coordinator

    if account_type == ACCOUNT_TYPE_NICOR_GAS:
        # Import on first setup (backfill), then keep the external long-term
        # statistics current on every subsequent coordinator refresh -- a
        # one-time import left the Energy Dashboard stuck on stale data
        # after the initial backfill.
        await _async_import_nicor_statistics_safe(hass, coordinator.data)

        def _on_nicor_update() -> None:
            if coordinator.data is not None:
                hass.async_create_task(
                    _async_import_nicor_statistics_safe(hass, coordinator.data)
                )

        entry.async_on_unload(coordinator.async_add_listener(_on_nicor_update))

    if account_type == ACCOUNT_TYPE_NICOR_GAS and not hass.services.has_service(
        DOMAIN, "reset_nicor_statistics"
    ):

        async def _handle_reset_nicor_statistics(call: ServiceCall) -> None:
            for entry_id, coord in hass.data.get(DOMAIN, {}).items():
                if not isinstance(coord, NicorGasCoordinator):
                    continue
                await coord.async_refresh()
                if coord.data is None:
                    _LOGGER.warning(
                        "No Nicor Gas data available for entry %s after refresh",
                        entry_id,
                    )
                    continue
                await _async_import_nicor_statistics_safe(hass, coord.data)
                _LOGGER.info("Nicor Gas statistics reimported for entry %s", entry_id)

        hass.services.async_register(
            DOMAIN, "reset_nicor_statistics", _handle_reset_nicor_statistics
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

    remaining_nicor = any(
        isinstance(c, NicorGasCoordinator) for c in hass.data.get(DOMAIN, {}).values()
    )
    if not remaining_nicor and hass.services.has_service(
        DOMAIN, "reset_nicor_statistics"
    ):
        hass.services.async_remove(DOMAIN, "reset_nicor_statistics")

    return unload_ok
