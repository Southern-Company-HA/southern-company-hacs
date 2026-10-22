"""Tests for the reconfigure step."""

from unittest.mock import patch

import pytest
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.southern_company.config_flow import ConfigFlow

DATA = {
    "username": "user@example.com",
    "password": "old",
    "account_type": "southern_company",
}


def fake_auth(error: str | None = None):
    async def _try_authenticate(self, user_input, errors):
        if error:
            errors["base"] = error

    return _try_authenticate


@pytest.fixture(autouse=True)
def no_setup():
    with patch(
        "custom_components.southern_company.async_setup_entry", return_value=True
    ):
        yield


@pytest.fixture
def entry(hass):
    entry = MockConfigEntry(
        domain="southern_company", data=dict(DATA), title="Southern Company Hacs"
    )
    entry.add_to_hass(hass)
    return entry


async def test_reconfigure_shows_prefilled_form(hass, entry):
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    username_key = next(k for k in result["data_schema"].schema if k == "username")
    assert username_key.description["suggested_value"] == "user@example.com"


async def test_reconfigure_updates_credentials(hass, entry):
    result = await entry.start_reconfigure_flow(hass)
    with patch.object(ConfigFlow, "_try_authenticate", fake_auth()):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "username": "user@example.com",
                "password": "new",
                "account_type": "southern_company",
            },
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["password"] == "new"


@pytest.mark.parametrize(
    "error", ["invalid_auth", "cannot_connect", "email_validation_required", "unknown"]
)
async def test_reconfigure_errors_reshow_form(hass, entry, error):
    result = await entry.start_reconfigure_flow(hass)
    with patch.object(ConfigFlow, "_try_authenticate", fake_auth(error)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**DATA, "password": "bad"}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}
    assert entry.data["password"] == "old"


async def test_reconfigure_rejects_username_of_another_entry(hass, entry):
    MockConfigEntry(
        domain="southern_company", data={**DATA, "username": "other@example.com"}
    ).add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    with patch.object(ConfigFlow, "_try_authenticate", fake_auth()):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**DATA, "username": "other@example.com"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data["username"] == "user@example.com"
