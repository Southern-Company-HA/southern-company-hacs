"""Historical statistics injection for Nicor Gas."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from typing import Any

import southern_company_api
from southern_company_api.nicor_account import NicorBillingPeriod, NicorDailyUsage, NicorUsageHistory

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.const import CURRENCY_DOLLAR, UnitOfVolume
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STAT_DAILY_GAS = f"{DOMAIN}:nicor_gas_daily_gas"
STAT_DAILY_COST = f"{DOMAIN}:nicor_gas_daily_cost"
STAT_BILLING_GAS = f"{DOMAIN}:nicor_gas_billing_period_gas"
STAT_BILLING_COST = f"{DOMAIN}:nicor_gas_billing_period_cost"


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = str(value).replace("$", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except (ValueError, TypeError):
        return default


def _cost_for_billing_period(
    period: NicorBillingPeriod,
    daily_usage: list[NicorDailyUsage],
) -> float:
    """Sum daily costs whose date falls within the billing period's date range."""
    period_end = period.date.date()
    period_start = (period.date - timedelta(days=period.days_used - 1)).date()
    return sum(
        _safe_float(day.cost)
        for day in daily_usage
        if period_start <= day.date.date() <= period_end
    )


async def async_import_nicor_statistics(
    hass: HomeAssistant,
    data: NicorUsageHistory,
) -> None:
    """Backfill up to 13 months of Nicor Gas statistics into the HA recorder."""
    try:
        get_instance(hass)
    except Exception:
        _LOGGER.warning("Recorder unavailable; skipping Nicor Gas statistics import")
        return

    daily = sorted(data.daily_usage, key=lambda d: d.date)
    billing = sorted(data.billing_periods, key=lambda p: p.date)

    _import_daily_statistics(hass, daily)
    _import_billing_period_statistics(hass, billing, data.daily_usage)

    _LOGGER.info(
        "Imported Nicor Gas statistics: %d daily records, %d billing periods",
        len(daily),
        len(billing),
    )


def _import_daily_statistics(
    hass: HomeAssistant,
    daily: list[NicorDailyUsage],
) -> None:
    if not daily:
        return

    gas_stats: list[StatisticData] = []
    cost_stats: list[StatisticData] = []
    gas_sum = 0.0
    cost_sum = 0.0

    for day in daily:
        # Normalise to midnight UTC so each day gets one statistic slot.
        start = day.date.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
        ccf = day.therms / 1.02
        cost = _safe_float(day.cost)
        gas_sum += ccf
        cost_sum += cost
        gas_stats.append(StatisticData(start=start, state=ccf, sum=gas_sum))
        cost_stats.append(StatisticData(start=start, state=cost, sum=cost_sum))

    async_add_external_statistics(
        hass,
        StatisticMetaData(
            has_mean=False,
            has_sum=True,
            name="Nicor Gas daily gas",
            source=DOMAIN,
            statistic_id=STAT_DAILY_GAS,
            unit_of_measurement=UnitOfVolume.CENTUM_CUBIC_FEET,
        ),
        gas_stats,
    )
    async_add_external_statistics(
        hass,
        StatisticMetaData(
            has_mean=False,
            has_sum=True,
            name="Nicor Gas daily cost",
            source=DOMAIN,
            statistic_id=STAT_DAILY_COST,
            unit_of_measurement=CURRENCY_DOLLAR,
        ),
        cost_stats,
    )


def _import_billing_period_statistics(
    hass: HomeAssistant,
    billing: list[NicorBillingPeriod],
    daily_usage: list[NicorDailyUsage],
) -> None:
    if not billing:
        return

    gas_stats: list[StatisticData] = []
    cost_stats: list[StatisticData] = []
    gas_sum = 0.0
    cost_sum = 0.0

    for period in billing:
        # period.date is naive (midnight local); treat as UTC midnight.
        start = period.date.replace(tzinfo=timezone.utc)
        ccf = _safe_float(period.ccfs)
        cost = _cost_for_billing_period(period, daily_usage)
        gas_sum += ccf
        cost_sum += cost
        gas_stats.append(StatisticData(start=start, state=ccf, sum=gas_sum))
        cost_stats.append(StatisticData(start=start, state=cost, sum=cost_sum))

    async_add_external_statistics(
        hass,
        StatisticMetaData(
            has_mean=False,
            has_sum=True,
            name="Nicor Gas billing period gas",
            source=DOMAIN,
            statistic_id=STAT_BILLING_GAS,
            unit_of_measurement=UnitOfVolume.CENTUM_CUBIC_FEET,
        ),
        gas_stats,
    )
    async_add_external_statistics(
        hass,
        StatisticMetaData(
            has_mean=False,
            has_sum=True,
            name="Nicor Gas billing period cost",
            source=DOMAIN,
            statistic_id=STAT_BILLING_COST,
            unit_of_measurement=CURRENCY_DOLLAR,
        ),
        cost_stats,
    )
