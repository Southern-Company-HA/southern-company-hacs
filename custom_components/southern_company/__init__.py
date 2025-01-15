"""The Southern Company integration."""

from __future__ import annotations

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
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import aiohttp_client

from .const import DOMAIN
from .coordinator import SouthernCompanyCoordinator

PLATFORMS = [Platform.SENSOR]
failures: dict[str, float] = {}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Southern Company from a config entry."""
    if entry.entry_id in failures:
        if not time.time() - failures[entry.entry_id] > 600:
            raise ConfigEntryNotReady(
                "Delaying retrying to prevent robot detection. You may need to restart to fix this."
            )
    hass.data.setdefault(DOMAIN, {})
    session = aiohttp_client.async_create_clientsession(hass)
    sca = SouthernCompanyAPI(
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        session,
    )
    try:
        await sca.authenticate()
    except CantReachSouthernCompany as err:
        raise ConfigEntryNotReady("Can not connect to the southern company") from err
    except (NoScTokenFound, NoRequestTokenFound) as err:
        failures[entry.entry_id] = time.time()
        raise ConfigEntryNotReady(
            "Token not found in southern company response. Please double check your credentials or open an issue"
        ) from err
    except InvalidLogin as err:
        raise ConfigEntryAuthFailed("Login incorrect") from err
    if entry.entry_id in failures:
        failures.pop(entry.entry_id)
    coordinator = SouthernCompanyCoordinator(hass, sca)
    await coordinator.async_config_entry_first_refresh()
    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok
