"""The Southern Company integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry, ConfigEntryChange
from homeassistant.const import CONF_PASSWORD
from homeassistant.const import CONF_USERNAME
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN
import logging
PLATFORMS = [Platform.SENSOR]

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Southern Company from a config entry."""
    _LOGGER.error("PLease set up with Southern Company instead of Southern Company HACS")
    return False


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok

async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """"""
    entry.domain = "southern_company"
    hass.config_entries.async_update_entry(
        entry,
    )
    hass.config_entries._async_schedule_save()
    hass.config_entries._async_dispatch(ConfigEntryChange.UPDATED, entry)
    return True
