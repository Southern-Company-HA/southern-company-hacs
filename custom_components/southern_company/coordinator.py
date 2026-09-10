"""Coordinator to handle southern Company connections."""

from __future__ import annotations

import asyncio
import datetime
from datetime import timedelta
import logging
from typing import TYPE_CHECKING

import southern_company_api
from southern_company_api.exceptions import SouthernCompanyException

if TYPE_CHECKING:
    from southern_company_api.nicor_parser import NicorGasAPI

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.const import CURRENCY_DOLLAR, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class SouthernCompanyCoordinator(DataUpdateCoordinator):
    """Handle Southern company data and insert statistics."""

    def __init__(
        self,
        hass: HomeAssistant,
        southern_company_connection: southern_company_api.SouthernCompanyAPI,
    ) -> None:
        """Initialize the data handler."""
        super().__init__(
            hass,
            _LOGGER,
            name="Southern Company",
            update_interval=timedelta(minutes=60),
        )
        self._southern_company_connection = southern_company_connection

    @property
    def api(self) -> southern_company_api.SouthernCompanyAPI:
        """Access the api."""
        return self._southern_company_connection

    async def _async_update_data(
        self,
    ) -> dict[str, southern_company_api.account.MonthlyUsage]:
        """Update data via API."""
        try:
            if await self._southern_company_connection.jwt is not None:
                account_month_data: dict[
                    str, southern_company_api.account.MonthlyUsage
                ] = {}
                for account in await self._southern_company_connection.accounts:
                    if not account.service_point_number:
                        _LOGGER.warning(
                            "Skipping account ending in %s: no service point number",
                            account.number[-4:] if account.number else "????",
                        )
                        continue
                    _LOGGER.debug("Updating sensor data for %s", account.number)
                    account_month_data[account.number] = await account.get_month_data(
                        await self._southern_company_connection.jwt
                    )
                # Note: insert statistics can be somewhat slow on first setup.
                await self._insert_statistics()
                return account_month_data
        except SouthernCompanyException as ex:
            raise UpdateFailed("Failed updating jwt token") from ex

        raise UpdateFailed("No jwt token")

    async def _insert_statistics(self) -> None:
        """Insert Southern Company statistics."""
        if await self._southern_company_connection.jwt is None:
            raise UpdateFailed("Jwt is None")
        for account in await self._southern_company_connection.accounts:
            if not account.service_point_number:
                continue
            _LOGGER.debug("Updating Statistics for %s", account.number)
            cost_statistic_id = f"{DOMAIN}:energy_cost_{account.number}"
            usage_statistic_id = f"{DOMAIN}:energy_usage_{account.number}"

            last_stats = await get_instance(self.hass).async_add_executor_job(
                get_last_statistics, self.hass, 1, usage_statistic_id, True, set()
            )
            is_hourly = True
            jwt = await self._southern_company_connection.jwt
            if not last_stats:
                # First time setup: attempt to fetch 23 months of hourly data
                _LOGGER.info(
                    "Updating statistic for the first time. Attempting to fetch hourly data for %s",
                    account.number,
                )
                try:
                    hourly_data = await account.get_hourly_data(
                        datetime.datetime.now() - timedelta(days=700),
                        datetime.datetime.now(),
                        jwt,
                    )
                except Exception as e:
                    _LOGGER.debug(
                        "Failed to get hourly data for account %s: %s. Falling back to daily data.",
                        account.number,
                        e,
                    )
                    hourly_data = []

                if not hourly_data:
                    _LOGGER.info(
                        "Fetching up to 23 months of daily data for account %s",
                        account.number,
                    )
                    is_hourly = False
                    daily_data = await account.get_daily_data(
                        datetime.datetime.now() - timedelta(days=700),
                        datetime.datetime.now(),
                        jwt,
                    )
                else:
                    daily_data = []

                _cost_sum = 0.0
                _usage_sum = 0.0
                last_stats_time = None
            else:
                # Ongoing update: detect established series granularity to prevent mixing hourly and daily rows
                sample_start = datetime.datetime.now() - timedelta(days=35)
                check_stat = await get_instance(self.hass).async_add_executor_job(
                    statistics_during_period,
                    self.hass,
                    sample_start,
                    None,
                    [cost_statistic_id],
                    "hour",
                    None,
                    {"sum"},
                )
                is_hourly = bool(
                    cost_statistic_id in check_stat and check_stat[cost_statistic_id]
                )

                if is_hourly:
                    try:
                        hourly_data = await account.get_hourly_data(
                            datetime.datetime.now() - timedelta(days=31),
                            datetime.datetime.now(),
                            jwt,
                        )
                    except Exception as e:
                        _LOGGER.warning(
                            "Failed to fetch hourly data for established hourly account %s: %s",
                            account.number,
                            e,
                        )
                        continue

                    if not hourly_data:
                        _LOGGER.warning(
                            "No hourly data returned for established hourly account %s",
                            account.number,
                        )
                        continue

                    daily_data = []
                    from_time = hourly_data[0].time
                    period = "hour"
                    start_offset = timedelta(hours=1)
                else:
                    try:
                        daily_data = await account.get_daily_data(
                            datetime.datetime.now() - timedelta(days=31),
                            datetime.datetime.now(),
                            jwt,
                        )
                    except Exception as e:
                        _LOGGER.warning(
                            "Failed to fetch daily data for account %s: %s",
                            account.number,
                            e,
                        )
                        continue

                    if not daily_data:
                        _LOGGER.warning("No daily data returned for account %s", account.number)
                        continue

                    hourly_data = []
                    from_time = daily_data[0].date
                    period = "day"
                    start_offset = timedelta(days=1)

                if from_time and from_time.tzinfo is None:
                    from_time = from_time.replace(tzinfo=datetime.timezone.utc)
                start = from_time - start_offset if from_time else None

                cost_stat = await get_instance(self.hass).async_add_executor_job(
                    statistics_during_period,
                    self.hass,
                    start,
                    None,
                    [cost_statistic_id],
                    period,
                    None,
                    {"sum"},
                )
                usage_stat = await get_instance(self.hass).async_add_executor_job(
                    statistics_during_period,
                    self.hass,
                    start,
                    None,
                    [usage_statistic_id],
                    period,
                    None,
                    {"sum"},
                )

                # Validate BOTH baselines to prevent restarting cumulative usage or cost at zero
                has_cost_baseline = (
                    cost_statistic_id in cost_stat and bool(cost_stat[cost_statistic_id])
                )
                has_usage_baseline = (
                    usage_statistic_id in usage_stat
                    and bool(usage_stat[usage_statistic_id])
                )

                if not has_cost_baseline or not has_usage_baseline:
                    _LOGGER.warning(
                        "Missing baseline statistics for account %s. Rebuilding history...",
                        account.number,
                    )
                    if is_hourly:
                        try:
                            hourly_data = await account.get_hourly_data(
                                datetime.datetime.now() - timedelta(days=700),
                                datetime.datetime.now(),
                                jwt,
                            )
                        except Exception as e:
                            _LOGGER.warning("Failed hourly rebuild: %s", e)
                            continue
                        if not hourly_data:
                            continue
                    else:
                        try:
                            daily_data = await account.get_daily_data(
                                datetime.datetime.now() - timedelta(days=700),
                                datetime.datetime.now(),
                                jwt,
                            )
                        except Exception as e:
                            _LOGGER.warning("Failed daily rebuild: %s", e)
                            continue
                        if not daily_data:
                            continue

                    _cost_sum = 0.0
                    _usage_sum = 0.0
                    last_stats_time = None
                else:
                    _cost_sum = cost_stat[cost_statistic_id][0]["sum"] or 0.0
                    _usage_sum = usage_stat[usage_statistic_id][0]["sum"] or 0.0
                    _raw_last_stats_start = max(
                        cost_stat[cost_statistic_id][0]["start"],
                        usage_stat[usage_statistic_id][0]["start"],
                    )
                    last_stats_time = (
                        _raw_last_stats_start.timestamp()
                        if isinstance(_raw_last_stats_start, datetime.datetime)
                        else float(_raw_last_stats_start)
                    )

            data_points = hourly_data if is_hourly else daily_data
            if not data_points:
                continue

            cost_statistics = []
            usage_statistics = []

            for data in data_points:
                # Southern Company's web portal returns -1 for usage and cost to
                # represent missing/unbilled days. We skip these placeholder values.
                if (
                    data.cost is None
                    or data.usage is None
                    or data.usage == -1
                    or data.cost == -1
                    or isinstance(data.cost, bool)
                    or isinstance(data.usage, bool)
                    or not isinstance(data.cost, (int, float))
                    or not isinstance(data.usage, (int, float))
                ):
                    continue
                from_time = data.time if is_hourly else data.date
                if from_time is None:
                    continue
                # Normalize timezone to UTC and align start time to boundary
                if from_time.tzinfo is None:
                    from_time = from_time.replace(tzinfo=datetime.timezone.utc)
                if is_hourly:
                    from_time = from_time.replace(minute=0, second=0, microsecond=0)
                else:
                    from_time = from_time.replace(hour=0, minute=0, second=0, microsecond=0)

                if (
                    last_stats_time is not None
                    and from_time.timestamp() <= last_stats_time
                ):
                    continue
                _cost_sum += data.cost
                _usage_sum += data.usage

                cost_statistics.append(
                    StatisticData(
                        start=from_time,
                        state=data.cost,
                        sum=_cost_sum,
                    )
                )
                usage_statistics.append(
                    StatisticData(
                        start=from_time,
                        state=data.usage,
                        sum=_usage_sum,
                    )
                )

            cost_metadata_kwargs = {
                "has_mean": False,
                "has_sum": True,
                "name": f"Southern Company {account.name} cost",
                "source": DOMAIN,
                "statistic_id": cost_statistic_id,
                "unit_of_measurement": CURRENCY_DOLLAR,
            }
            usage_metadata_kwargs = {
                "has_mean": False,
                "has_sum": True,
                "name": f"Southern Company {account.name} usage",
                "source": DOMAIN,
                "statistic_id": usage_statistic_id,
                "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
            }

            # Check if mean_type and unit_class are supported for backwards compatibility
            stat_fields = (
                set(StatisticMetaData.__annotations__.keys())
                if hasattr(StatisticMetaData, "__annotations__")
                else set()
            )
            if "mean_type" in stat_fields:
                from homeassistant.components.recorder.models import StatisticMeanType

                cost_metadata_kwargs["mean_type"] = StatisticMeanType.NONE
                usage_metadata_kwargs["mean_type"] = StatisticMeanType.NONE
                # Remove deprecated has_mean to prevent warnings when mean_type is present
                cost_metadata_kwargs.pop("has_mean", None)
                usage_metadata_kwargs.pop("has_mean", None)
            if "unit_class" in stat_fields:
                cost_metadata_kwargs["unit_class"] = None
                usage_metadata_kwargs["unit_class"] = "energy"

            cost_metadata = StatisticMetaData(**cost_metadata_kwargs)
            usage_metadata = StatisticMetaData(**usage_metadata_kwargs)
            async_add_external_statistics(self.hass, cost_metadata, cost_statistics)
            async_add_external_statistics(self.hass, usage_metadata, usage_statistics)


class NicorGasCoordinator(DataUpdateCoordinator):
    """Handle Nicor Gas data updates."""

    def __init__(
        self,
        hass: HomeAssistant,
        nicor_gas_api: NicorGasAPI,
    ) -> None:
        """Initialize the data handler."""
        super().__init__(
            hass,
            _LOGGER,
            name="Nicor Gas",
            update_interval=timedelta(minutes=60),
        )
        self._api = nicor_gas_api

    @property
    def api(self) -> NicorGasAPI:
        """Access the API."""
        return self._api

    async def _async_update_data(self) -> southern_company_api.NicorUsageHistory:
        """Update data via API."""
        try:
            await self._api.connect()
            await asyncio.sleep(10)
            return await self._api.get_usage_history()
        except Exception as ex:
            _LOGGER.exception("Unexpected error fetching Nicor Gas usage history")
            raise UpdateFailed(f"Failed to get Nicor Gas usage history: {ex}") from ex
