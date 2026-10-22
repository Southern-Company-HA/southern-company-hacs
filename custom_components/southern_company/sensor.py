"""Support for Southern Company sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import datetime
from typing import Any

import southern_company_api

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_USERNAME,
    CURRENCY_DOLLAR,
    UnitOfEnergy,
    UnitOfTemperature,
    UnitOfTime,
    UnitOfVolume,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import NicorGasCoordinator, SouthernCompanyCoordinator


@dataclass(frozen=True)
class SouthernCompanyEntityDescriptionMixin:
    """Mixin for required keys."""

    value_fn: Callable[[southern_company_api.account.MonthlyUsage], str | float]


@dataclass(frozen=True)
class SouthernCompanyEntityDescription(
    SensorEntityDescription, SouthernCompanyEntityDescriptionMixin
):
    """Describes Southern Company sensor entity."""


SENSORS: tuple[SouthernCompanyEntityDescription, ...] = (
    SouthernCompanyEntityDescription(
        key="dollars_to_date",
        name="Monthly cost",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        value_fn=lambda data: data.dollars_to_date,
        native_unit_of_measurement=CURRENCY_DOLLAR,
    ),
    SouthernCompanyEntityDescription(
        key="total_kwh_used",
        name="Monthly consumption",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data: data.total_kwh_used,
    ),
    SouthernCompanyEntityDescription(
        key="average_daily_cost",
        name="Average daily cost",
        device_class=SensorDeviceClass.MONETARY,
        value_fn=lambda data: data.average_daily_cost,
        native_unit_of_measurement=CURRENCY_DOLLAR,
    ),
    SouthernCompanyEntityDescription(
        key="average_daily_usage",
        name="Average daily usage",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        value_fn=lambda data: data.average_daily_usage,
    ),
    SouthernCompanyEntityDescription(
        key="projected_usage_high",
        name="Higher projected monthly usage",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda data: data.projected_usage_high,
    ),
    SouthernCompanyEntityDescription(
        key="projected_usage_low",
        name="Lower projected monthly usage",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda data: data.projected_usage_low,
    ),
    SouthernCompanyEntityDescription(
        key="projected_bill_amount_low",
        name="Lower projected monthly cost",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda data: data.projected_bill_amount_low,
        native_unit_of_measurement=CURRENCY_DOLLAR,
    ),
    SouthernCompanyEntityDescription(
        key="projected_bill_amount_high",
        name="Higher projected monthly cost",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda data: data.projected_bill_amount_high,
        native_unit_of_measurement=CURRENCY_DOLLAR,
    ),
)


# ---------------------------------------------------------------------------
# Nicor Gas sensors
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class NicorGasEntityDescriptionMixin:
    """Mixin for required keys."""

    value_fn: Callable[
        [southern_company_api.NicorUsageHistory],
        StateType | datetime.date,
    ]
    attr_fn: (
        Callable[
            [southern_company_api.NicorUsageHistory],
            dict[str, Any] | None,
        ]
        | None
    ) = None
    last_reset_fn: (
        Callable[
            [southern_company_api.NicorUsageHistory],
            datetime.datetime | None,
        ]
        | None
    ) = None
    statistic_id: str | None = None


@dataclass(frozen=True, kw_only=True)
class NicorGasEntityDescription(
    SensorEntityDescription, NicorGasEntityDescriptionMixin
):
    """Describes Nicor Gas sensor entity."""


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Convert value to float, stripping currency symbols; return default on failure."""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = str(value).replace("$", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except (ValueError, TypeError):
        return default


def _billing_period_ccfs(
    data: southern_company_api.NicorUsageHistory,
) -> float | None:
    if not data.billing_periods:
        return None
    return max(data.billing_periods, key=lambda p: p.date).ccfs


def _billing_period_therms_attrs(
    data: southern_company_api.NicorUsageHistory,
) -> dict[str, float] | None:
    if not data.billing_periods:
        return None
    return {"therms": max(data.billing_periods, key=lambda p: p.date).therms}


def _current_billing_period_cost(
    data: southern_company_api.NicorUsageHistory,
) -> float | None:
    if not data.daily_usage:
        return None
    current_period = max(data.daily_usage, key=lambda d: d.date).billing_period
    return sum(
        _safe_float(d.cost)
        for d in data.daily_usage
        if d.billing_period == current_period
    )


def _most_recent_daily_ccfs(
    data: southern_company_api.NicorUsageHistory,
) -> float | None:
    if not data.daily_usage:
        return None
    # Per-day CCf is not in the API; approximated from therms (1 CCf ≈ 1.02 therms)
    return max(data.daily_usage, key=lambda d: d.date).therms / 1.02


def _most_recent_daily_therms_attrs(
    data: southern_company_api.NicorUsageHistory,
) -> dict[str, float] | None:
    if not data.daily_usage:
        return None
    return {"therms": max(data.daily_usage, key=lambda d: d.date).therms}


def _most_recent_daily_cost(
    data: southern_company_api.NicorUsageHistory,
) -> float | None:
    if not data.daily_usage:
        return None
    return _safe_float(max(data.daily_usage, key=lambda d: d.date).cost)


def _next_meter_read_date(
    data: southern_company_api.NicorUsageHistory,
) -> datetime.date | None:
    if not data.meter_info:
        return None
    return data.meter_info.next_read_date.date()


def _most_recent_daily_avg_temp(
    data: southern_company_api.NicorUsageHistory,
) -> float | None:
    if not data.daily_usage:
        return None
    return max(data.daily_usage, key=lambda d: d.date).avg_temp


def _most_recent_daily_meter_read(
    data: southern_company_api.NicorUsageHistory,
) -> float | None:
    if not data.daily_usage:
        return None
    return max(data.daily_usage, key=lambda d: d.date).meter_read


def _most_recent_daily_read_type(
    data: southern_company_api.NicorUsageHistory,
) -> str | None:
    if not data.daily_usage:
        return None
    return max(data.daily_usage, key=lambda d: d.date).read_type


def _current_billing_period_days(
    data: southern_company_api.NicorUsageHistory,
) -> int | None:
    if not data.billing_periods:
        return None
    return max(data.billing_periods, key=lambda p: p.date).days_used


def _prev_billing_period_therms(
    data: southern_company_api.NicorUsageHistory,
) -> float | None:
    if len(data.billing_periods) < 2:
        return None
    sorted_periods = sorted(data.billing_periods, key=lambda p: p.date, reverse=True)
    return sorted_periods[1].therms


def _billing_period_last_reset(
    data: southern_company_api.NicorUsageHistory,
) -> datetime.datetime | None:
    if not data.billing_periods:
        return None
    latest = max(data.billing_periods, key=lambda p: p.date)
    start_date = latest.date - datetime.timedelta(days=latest.days_used - 1)
    return datetime.datetime(
        start_date.year,
        start_date.month,
        start_date.day,
        tzinfo=datetime.timezone.utc,
    )


def _projected_bill_amount(
    data: southern_company_api.NicorUsageHistory,
) -> float | None:
    if not data.projected_bill:
        return None
    return _safe_float(data.projected_bill.high_amount)


NICOR_SENSORS: tuple[NicorGasEntityDescription, ...] = (
    NicorGasEntityDescription(
        key="billing_period_gas",
        name="Billing period gas",
        device_class=SensorDeviceClass.GAS,
        native_unit_of_measurement=UnitOfVolume.CENTUM_CUBIC_FEET,
        state_class=SensorStateClass.TOTAL,
        value_fn=_billing_period_ccfs,
        attr_fn=_billing_period_therms_attrs,
        last_reset_fn=_billing_period_last_reset,
    ),
    NicorGasEntityDescription(
        key="billing_period_cost",
        name="Billing period cost",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        native_unit_of_measurement=CURRENCY_DOLLAR,
        value_fn=_current_billing_period_cost,
    ),
    NicorGasEntityDescription(
        key="projected_bill",
        name="Projected bill",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=CURRENCY_DOLLAR,
        value_fn=_projected_bill_amount,
        last_reset_fn=_billing_period_last_reset,
    ),
    NicorGasEntityDescription(
        key="daily_gas",
        name="Daily gas",
        device_class=SensorDeviceClass.GAS,
        native_unit_of_measurement=UnitOfVolume.CENTUM_CUBIC_FEET,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_most_recent_daily_ccfs,
        attr_fn=_most_recent_daily_therms_attrs,
        statistic_id=f"{DOMAIN}:nicor_gas_daily_gas",
    ),
    NicorGasEntityDescription(
        key="daily_cost",
        name="Daily cost",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        native_unit_of_measurement=CURRENCY_DOLLAR,
        value_fn=_most_recent_daily_cost,
        statistic_id=f"{DOMAIN}:nicor_gas_daily_cost",
    ),
    NicorGasEntityDescription(
        key="next_meter_read_date",
        name="Next meter read date",
        device_class=SensorDeviceClass.DATE,
        value_fn=_next_meter_read_date,
    ),
    NicorGasEntityDescription(
        key="daily_avg_temp",
        name="Daily average temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_most_recent_daily_avg_temp,
    ),
    NicorGasEntityDescription(
        key="daily_meter_reading",
        name="Daily meter reading",
        native_unit_of_measurement=UnitOfVolume.CUBIC_FEET,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=_most_recent_daily_meter_read,
    ),
    NicorGasEntityDescription(
        key="daily_read_type",
        name="Daily read type",
        value_fn=_most_recent_daily_read_type,
    ),
    NicorGasEntityDescription(
        key="billing_period_days",
        name="Days in billing period",
        native_unit_of_measurement=UnitOfTime.DAYS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_current_billing_period_days,
    ),
    NicorGasEntityDescription(
        key="billing_period_ccf",
        name="Billing period CCf",
        device_class=SensorDeviceClass.GAS,
        native_unit_of_measurement=UnitOfVolume.CENTUM_CUBIC_FEET,
        state_class=SensorStateClass.TOTAL,
        value_fn=_billing_period_ccfs,
        last_reset_fn=_billing_period_last_reset,
    ),
    NicorGasEntityDescription(
        key="prev_billing_period_therms",
        name="Previous billing period therms",
        native_unit_of_measurement="thm",
        state_class=SensorStateClass.TOTAL,
        value_fn=_prev_billing_period_therms,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the Southern Company sensor."""

    coordinator = hass.data[DOMAIN][entry.entry_id]

    if isinstance(coordinator, NicorGasCoordinator):
        data = coordinator.data
        if data.meter_info:
            meter_id = data.meter_info.meter_number
        else:
            meter_id = entry.data[CONF_USERNAME]
        device = DeviceInfo(
            identifiers={(DOMAIN, f"nicor_gas_{meter_id}")},
            name="Nicor Gas",
            manufacturer="Nicor Gas",
        )
        async_add_entities(
            [
                NicorGasSensor(coordinator, sensor, meter_id, device)
                for sensor in NICOR_SENSORS
            ]
        )
        return

    southern_company_coordinator: SouthernCompanyCoordinator = coordinator
    southern_company_connection = southern_company_coordinator.api
    entities: list[SouthernCompanySensor] = []
    for account in await southern_company_connection.accounts:
        device = DeviceInfo(
            identifiers={(DOMAIN, account.number)},
            name=f"Account {account.number}",
            manufacturer="Southern Company",
        )

        # entities.append(SouthernCompanySensor(account, coordinator, sensor, device))
        entities.extend(
            [
                SouthernCompanySensor(
                    account, southern_company_coordinator, sensor, device
                )
                for sensor in SENSORS
            ]
        )

    async_add_entities(entities)


class SouthernCompanySensor(
    SensorEntity, CoordinatorEntity[SouthernCompanyCoordinator]
):
    """Representation of a Southern company sensor."""

    def __init__(
        self,
        account: southern_company_api.Account,
        coordinator: SouthernCompanyCoordinator,
        description: SouthernCompanyEntityDescription,
        device: DeviceInfo,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description: SouthernCompanyEntityDescription = description
        self._account = account
        self._attr_unique_id = f"{self._account.number}_{description.key}"
        self._attr_device_info = device
        self._sensor_data = None

    @property
    def native_value(self) -> StateType:
        """Return the state."""
        if self.coordinator.data is not None:
            return self.entity_description.value_fn(
                self.coordinator.data[self._account.number]
            )
        return None


class NicorGasSensor(SensorEntity, CoordinatorEntity[NicorGasCoordinator]):
    """Representation of a Nicor Gas sensor."""

    def __init__(
        self,
        coordinator: NicorGasCoordinator,
        description: NicorGasEntityDescription,
        meter_id: str,
        device: DeviceInfo,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description: NicorGasEntityDescription = description
        self._attr_unique_id = f"nicor_gas_{meter_id}_{description.key}"
        self._attr_device_info = device
        if description.statistic_id is not None:
            self._attr_statistic_id = description.statistic_id

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return additional state attributes."""
        if (
            self.coordinator.data is not None
            and self.entity_description.attr_fn is not None
        ):
            return self.entity_description.attr_fn(self.coordinator.data)
        return None

    @property
    def last_reset(self) -> datetime.datetime | None:
        """Return the time when the sensor was last reset."""
        if (
            self.coordinator.data is not None
            and self.entity_description.last_reset_fn is not None
        ):
            return self.entity_description.last_reset_fn(self.coordinator.data)
        return None

    @property
    def native_value(self) -> StateType | datetime.date:
        """Return the state."""
        if self.coordinator.data is not None:
            return self.entity_description.value_fn(self.coordinator.data)
        return None
