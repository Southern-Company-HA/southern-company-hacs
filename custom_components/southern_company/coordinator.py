"""Coordinator to handle southern Company connections."""

from __future__ import annotations

import asyncio
import datetime
from datetime import timedelta
import logging
from math import isclose, isfinite
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
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class SouthernCompanyCoordinator(DataUpdateCoordinator):
    """Handle Southern company data and insert statistics."""

    def __init__(
        self,
        hass: HomeAssistant,
        southern_company_connection: southern_company_api.SouthernCompanyAPI,
        entry_id: str,
    ) -> None:
        """Initialize the data handler."""
        super().__init__(
            hass,
            _LOGGER,
            name="Southern Company",
            update_interval=timedelta(minutes=60),
        )
        self._southern_company_connection = southern_company_connection
        self._entry_id = entry_id
        self._statistics_lock = asyncio.Lock()
        self._checked_accounts: set[str] = set()

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
        jwt = await self._southern_company_connection.jwt
        if jwt is None:
            raise UpdateFailed("Jwt is None")
        async with self._statistics_lock:
            for account in await self._southern_company_connection.accounts:
                if not account.service_point_number:
                    continue
                try:
                    if account.number not in self._checked_accounts:
                        await self._check_statistics(account)
                        self._checked_accounts.add(account.number)
                    await self._append_statistics(account, jwt)
                except Exception:
                    # Statistics must never take down the sensors or other accounts.
                    _LOGGER.exception(
                        "Failed to update statistics for account %s", account.number
                    )

    async def _append_statistics(self, account, jwt: str) -> None:
        """Append new readings after the last stored row; never restart sums."""
        last = {
            key: await self._last_statistics(stat_id, 2)
            for key, stat_id in _statistic_ids(account).items()
        }
        now = dt_util.utcnow()
        starts = [_ts(row["start"]) for row in last["usage"]]
        if starts:
            # Keep the established resolution; daily rows sit at midnight UTC.
            hourly = not all(ts % 86400 == 0 for ts in starts)
            points = await _fetch(
                account, jwt, hourly, _from_ts(starts[-1]) - timedelta(days=2), now
            )
        else:
            _LOGGER.info("Importing statistics for %s", account.number)
            hourly = True
            points = await _fetch(account, jwt, True, now - BACKFILL, now)
            if not points:
                hourly = False
                points = await _fetch(account, jwt, False, now - BACKFILL, now)

        step = timedelta(hours=1) if hourly else timedelta(days=1)
        sums = {key: rows[-1]["sum"] if rows else 0.0 for key, rows in last.items()}
        expected = _from_ts(starts[-1]) + step if starts else None
        statistics = {"cost": [], "usage": []}
        for start, values in points.items():
            if start >= _incomplete(now, hourly):
                break
            if expected is not None:
                if start < expected:
                    continue
                # Wait for late portal data, but skip intervals that never
                # arrive (DST spring-forward hour, meter outages).
                if start > expected and expected > now - GAP_GRACE:
                    break
            expected = start + step
            for key, value in values.items():
                sums[key] += value
                statistics[key].append(
                    StatisticData(start=start, state=value, sum=sums[key])
                )
        await self._write_statistics(account, statistics)

    async def _check_statistics(self, account) -> None:
        """Raise a Repairs issue if repairable usage history has a reset."""
        window = dt_util.utcnow() - BACKFILL + timedelta(days=1)
        rows = (await self._history(account, window - timedelta(days=1)))["usage"]
        drop = next(
            (
                _from_ts(b["start"])
                for a, b in zip(rows, rows[1:])
                if None not in (a["sum"], b["sum"])
                and b["sum"] < a["sum"] - 1e-6
                # Net-metering exports lower the sum by exactly their state.
                and not isclose(b["sum"], a["sum"] + (b["state"] or 0), abs_tol=1e-6)
                and _from_ts(b["start"]) >= window
            ),
            None,
        )
        issue_id = f"statistics_{self._entry_id}_{account.number}"
        if drop is None:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        _LOGGER.warning(
            "Usage statistics for %s drop at %s; open Settings > System > Repairs",
            account.number,
            drop,
        )
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=True,
            severity=ir.IssueSeverity.ERROR,
            translation_key="statistics_reset",
            translation_placeholders={"account": account.number, "start": str(drop)},
            data={
                "entry_id": self._entry_id,
                "account": account.number,
                "start": drop.isoformat(),
            },
        )

    async def async_rebuild_statistics(self, account_number: str, start) -> None:
        """Rewrite history from start with portal data, keeping hourly detail.

        Stored rows the portal has no reading for are rewritten to add zero,
        so leftovers from an earlier reset no longer cause spikes.
        """
        async with self._statistics_lock:
            jwt = await self._southern_company_connection.jwt
            account = next(
                a
                for a in await self._southern_company_connection.accounts
                if a.number == account_number
            )
            now = dt_util.utcnow()
            fetch_from = start - timedelta(days=1)
            points = await _fetch(account, jwt, True, fetch_from, now)
            hourly = bool(points) and min(points) <= start + timedelta(days=1)
            if not hourly:
                points = await _fetch(account, jwt, False, fetch_from, now)
            if not points or min(points) > start + timedelta(days=1):
                raise HomeAssistantError(f"Portal has no data from {start}")
            points = {
                ts.timestamp(): values
                for ts, values in points.items()
                if start <= ts < _incomplete(now, hourly)
            }
            history = await self._history(account, start - BACKFILL)
            stamps = sorted(
                {_ts(r["start"]) for rows in history.values() for r in rows}
                | points.keys()
            )
            statistics = {}
            for key, rows in history.items():
                before = [r for r in rows if _ts(r["start"]) < start.timestamp()]
                total = before[-1]["sum"] if before else 0.0
                statistics[key] = []
                for ts in stamps:
                    if ts < start.timestamp():
                        continue
                    value = points.get(ts, {}).get(key, 0.0)
                    total += value
                    statistics[key].append(
                        StatisticData(start=_from_ts(ts), state=value, sum=total)
                    )
            await self._write_statistics(account, statistics)

    async def _last_statistics(self, stat_id: str, count: int) -> list[dict]:
        result = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, count, stat_id, True, {"sum"}
        )
        return sorted(result.get(stat_id, []), key=lambda row: _ts(row["start"]))

    async def _history(self, account, start) -> dict[str, list[dict]]:
        ids = _statistic_ids(account)
        result = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            start,
            None,
            set(ids.values()),
            "hour",
            None,
            {"state", "sum"},
        )
        return {
            key: sorted(result.get(stat_id, []), key=lambda row: _ts(row["start"]))
            for key, stat_id in ids.items()
        }

    async def _write_statistics(self, account, statistics: dict) -> None:
        for key, stat_id in _statistic_ids(account).items():
            if statistics[key]:
                async_add_external_statistics(
                    self.hass, _metadata(account, key, stat_id), statistics[key]
                )
        # Let the next read see these rows.
        await get_instance(self.hass).async_block_till_done()


BACKFILL = timedelta(days=700)
# Hourly data is published ~48h late; an interval missing for longer never arrives.
GAP_GRACE = timedelta(days=3)
# The API stamps hourly data at a fixed UTC-5; daily totals cover that local day.
PORTAL_OFFSET = timedelta(hours=5)


def _ts(value) -> float:
    return value.timestamp() if isinstance(value, datetime.datetime) else value


def _from_ts(value) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(_ts(value), datetime.timezone.utc)


def _incomplete(now: datetime.datetime, hourly: bool) -> datetime.datetime:
    """Return the start of the portal's current, still incomplete interval."""
    if hourly:
        return now.replace(minute=0, second=0, microsecond=0)
    return datetime.datetime.combine(
        (now - PORTAL_OFFSET).date(), datetime.time(), datetime.timezone.utc
    )


def _valid(value) -> bool:
    """Reject missing values and the portal's -1 placeholder; keep exports."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and isfinite(value)
        and value != -1
    )


async def _fetch(account, jwt, hourly, start, end) -> dict:
    """Return {start: {"cost", "usage"}} sorted by start; {} if unavailable."""
    try:
        get = account.get_hourly_data if hourly else account.get_daily_data
        data = await get(start.replace(tzinfo=None), end.replace(tzinfo=None), jwt)
    except Exception as err:
        _LOGGER.warning("Failed to get statistics for %s: %s", account.number, err)
        return {}
    points = {}
    for item in data:
        stamp = item.time if hourly else item.date
        if stamp is None or not (_valid(item.cost) and _valid(item.usage)):
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=datetime.timezone.utc)
        stamp = stamp.astimezone(datetime.timezone.utc)
        stamp = stamp.replace(minute=0, second=0, microsecond=0)
        if not hourly:
            stamp = stamp.replace(hour=0)
        points[stamp] = {"cost": float(item.cost), "usage": float(item.usage)}
    return dict(sorted(points.items()))


def _statistic_ids(account) -> dict[str, str]:
    return {key: f"{DOMAIN}:energy_{key}_{account.number}" for key in ("cost", "usage")}


def _metadata(account, key: str, stat_id: str) -> StatisticMetaData:
    kwargs = {
        "has_mean": False,
        "has_sum": True,
        "name": f"Southern Company {account.name} {key}",
        "source": DOMAIN,
        "statistic_id": stat_id,
        "unit_of_measurement": CURRENCY_DOLLAR
        if key == "cost"
        else UnitOfEnergy.KILO_WATT_HOUR,
    }
    # Check if mean_type and unit_class are supported for backwards compatibility
    fields = getattr(StatisticMetaData, "__annotations__", {})
    if "mean_type" in fields:
        from homeassistant.components.recorder.models import StatisticMeanType

        kwargs["mean_type"] = StatisticMeanType.NONE
        # Remove deprecated has_mean to prevent warnings when mean_type is present
        kwargs.pop("has_mean")
    if "unit_class" in fields:
        kwargs["unit_class"] = None if key == "cost" else "energy"
    return StatisticMetaData(**kwargs)


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
