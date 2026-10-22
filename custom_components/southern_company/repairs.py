"""Repairs for Southern Company statistics."""

from __future__ import annotations

from datetime import datetime
import logging

from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import SouthernCompanyCoordinator

_LOGGER = logging.getLogger(__name__)


class RebuildStatisticsFlow(ConfirmRepairFlow):
    """Rebuild statistics from the portal once the user confirms."""

    async def async_step_confirm(self, user_input=None):
        """Rebuild on confirm; the issue is removed when the flow finishes."""
        if user_input is not None:
            coordinator = self.hass.data.get(DOMAIN, {}).get(self.data["entry_id"])
            if not isinstance(coordinator, SouthernCompanyCoordinator):
                return self.async_abort(reason="not_loaded")
            try:
                await coordinator.async_rebuild_statistics(
                    self.data["account"], datetime.fromisoformat(self.data["start"])
                )
            except Exception:
                _LOGGER.exception("Failed to rebuild statistics")
                return self.async_abort(reason="rebuild_failed")
        return await super().async_step_confirm(user_input)


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict | None
) -> RepairsFlow:
    """Create the statistics rebuild flow."""
    return RebuildStatisticsFlow()
