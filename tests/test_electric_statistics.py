"""Electric statistics import and rebuild, against the real recorder."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component

from custom_components.southern_company.const import DOMAIN
from custom_components.southern_company.coordinator import SouthernCompanyCoordinator

UTC = timezone.utc
NOW = datetime(2026, 10, 22, 16, tzinfo=UTC)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)


def reading(start, usage=1.0, cost=0.1):
    return SimpleNamespace(
        time=start, date=start.replace(tzinfo=None), usage=usage, cost=cost
    )


@pytest.fixture(autouse=True)
def fixed_time(freezer):
    freezer.move_to(NOW)


@pytest.fixture
def account():
    return SimpleNamespace(
        number="123",
        name="Home",
        service_point_number="1",
        get_hourly_data=AsyncMock(return_value=[]),
        get_daily_data=AsyncMock(return_value=[]),
    )


@pytest.fixture
def coordinator(hass, account):
    class API:
        @property
        async def accounts(self):
            return [account]

        @property
        async def jwt(self):
            return "jwt"

    coordinator = SouthernCompanyCoordinator(hass, API(), "entry")
    hass.data[DOMAIN] = {"entry": coordinator}
    hass.config.components.add(DOMAIN)
    return coordinator


async def seed(coordinator, account, rows):
    """Write (start, state, sum) rows to both series."""
    await coordinator._write_statistics(
        account,
        {
            key: [dict(start=s, state=state, sum=total) for s, state, total in rows]
            for key in ("cost", "usage")
        },
    )


async def usage(coordinator, account):
    rows = (await coordinator._history(account, NOW - 1000 * DAY))["usage"]
    return [
        (datetime.fromtimestamp(r["start"], UTC), r["state"], r["sum"]) for r in rows
    ]


def issue(hass, account):
    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"statistics_entry_{account.number}"
    )


async def test_initial_import_prefers_hourly(coordinator, account):
    start = NOW - 3 * HOUR
    account.get_hourly_data.return_value = [reading(start + i * HOUR) for i in range(4)]
    await coordinator._insert_statistics()
    # The current hour is still incomplete.
    assert [r[2] for r in await usage(coordinator, account)] == [1, 2, 3]
    account.get_daily_data.assert_not_called()


async def test_initial_import_falls_back_to_daily(coordinator, account):
    days = [datetime(2026, 10, d, tzinfo=UTC) for d in (20, 21, 22)]
    account.get_daily_data.return_value = [reading(d, 10) for d in days]
    await coordinator._insert_statistics()
    # Oct 22 (local) is not complete yet.
    assert [r[2] for r in await usage(coordinator, account)] == [10, 20]


async def test_append_continues_sums_and_never_restarts(coordinator, account):
    """Issue #146: a gap before the first portal reading must not reset sums."""
    last = NOW - 10 * DAY
    await seed(coordinator, account, [(last - HOUR, 1, 6699), (last, 1, 6700)])
    account.get_hourly_data.return_value = [
        reading(last + i * HOUR) for i in range(-1, 3)
    ]
    await coordinator._insert_statistics()
    rows = await usage(coordinator, account)
    assert [r[2] for r in rows] == [6699, 6700, 6701, 6702]


async def test_established_daily_stays_daily(coordinator, account):
    last = datetime(2026, 10, 20, tzinfo=UTC)
    await seed(coordinator, account, [(last - DAY, 10, 90), (last, 10, 100)])
    account.get_hourly_data.return_value = [reading(last + HOUR)]
    account.get_daily_data.return_value = [reading(last + DAY, 10)]
    await coordinator._insert_statistics()
    assert [r[2] for r in await usage(coordinator, account)] == [90, 100, 110]
    account.get_hourly_data.assert_not_called()


async def test_placeholders_skipped_and_exports_kept(coordinator, account):
    last = NOW - 10 * DAY
    await seed(coordinator, account, [(last - HOUR, 1, 9), (last, 1, 10)])
    account.get_hourly_data.return_value = [
        reading(last + HOUR, -1),  # portal placeholder: never published
        reading(last + 2 * HOUR, -0.5),  # net-metering export
        reading(last + 3 * HOUR, 2),
    ]
    await coordinator._insert_statistics()
    assert [r[2] for r in await usage(coordinator, account)] == [9, 10, 9.5, 11.5]
    # An export is not mistaken for a reset.
    coordinator._checked_accounts.clear()
    await coordinator._insert_statistics()
    assert issue(coordinator.hass, account) is None


async def test_recent_gap_waits_for_portal(coordinator, account):
    last = NOW - DAY
    await seed(coordinator, account, [(last - HOUR, 1, 9), (last, 1, 10)])
    account.get_hourly_data.return_value = [reading(last + 2 * HOUR)]
    await coordinator._insert_statistics()
    assert len(await usage(coordinator, account)) == 2


async def test_statistics_errors_do_not_fail_update(coordinator, account):
    await seed(coordinator, account, [(NOW - DAY - HOUR, 1, 9), (NOW - DAY, 1, 10)])
    account.get_hourly_data.side_effect = RuntimeError("portal down")
    await coordinator._insert_statistics()
    account.get_daily_data.assert_not_called()
    coordinator._history = AsyncMock(side_effect=RuntimeError("recorder"))
    coordinator._checked_accounts.clear()
    await coordinator._insert_statistics()


async def test_reset_outside_portal_window_is_ignored(coordinator, account):
    old = NOW - 800 * DAY
    await seed(coordinator, account, [(old, 1, 7000), (old + HOUR, 1, 1)])
    await coordinator._insert_statistics()
    assert issue(coordinator.hass, account) is None


async def test_issue_146_detect_and_rebuild_keeping_hourly(hass, coordinator, account):
    """Old 01:00 rows interleaved with a reset at 00:00 cause +/- spikes."""
    start = datetime(2026, 10, 1, tzinfo=UTC)
    rows = [(start - HOUR, 1, 6600)]
    for day in range(3):
        midnight = start + day * DAY
        rows += [(midnight, 24, 24 * (day + 1)), (midnight + HOUR, 24, 6624 + day)]
    await seed(coordinator, account, rows)
    hourly = [reading(start + i * HOUR) for i in range(-2, 24 * 21)]
    account.get_hourly_data.return_value = hourly
    await coordinator._insert_statistics()
    found = issue(hass, account)
    assert found.is_fixable
    assert found.data["start"] == start.isoformat()

    assert await async_setup_component(hass, "repairs", {})
    flow = repairs_flow_manager(hass)
    result = await flow.async_init(DOMAIN, context={"issue_id": found.issue_id})
    assert result["step_id"] == "confirm"
    result = await flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert issue(hass, account) is None

    rebuilt = await usage(coordinator, account)
    assert rebuilt[0][2] == 6600
    assert all(b[2] - a[2] == 1 for a, b in zip(rebuilt, rebuilt[1:]))
    # The next refresh appends hourly data after the rebuilt tail.
    account.get_hourly_data.return_value = hourly + [reading(rebuilt[-1][0] + HOUR, 5)]
    await coordinator._insert_statistics()
    assert (await usage(coordinator, account))[-1][2] == rebuilt[-1][2] + 5


async def test_rebuild_failure_aborts_and_keeps_issue(hass, coordinator, account):
    start = datetime(2026, 10, 1, tzinfo=UTC)
    await seed(coordinator, account, [(start - HOUR, 1, 6600), (start, 1, 1)])
    await coordinator._insert_statistics()
    before = await usage(coordinator, account)
    assert await async_setup_component(hass, "repairs", {})
    flow = repairs_flow_manager(hass)
    result = await flow.async_init(
        DOMAIN, context={"issue_id": issue(hass, account).issue_id}
    )
    result = await flow.async_configure(result["flow_id"], {})
    assert result["reason"] == "rebuild_failed"
    assert issue(hass, account) is not None
    assert await usage(coordinator, account) == before
    await get_instance(hass).async_block_till_done()
