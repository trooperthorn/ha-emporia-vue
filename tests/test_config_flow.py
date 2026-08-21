"""Tests for the Emporia Vue config flow.

Mocks pyemvue.PyEmVue directly (the actual network boundary) so these run
without hitting Emporia's real API. `hub.vue` is a fresh PyEmVue() instance
per VueHub, so patching the class covers every instance the flow creates.
"""

from unittest.mock import MagicMock, patch

import pytest
import requests

from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.emporia_vue.const import DOMAIN


def _mock_customer(gid: str = "42", email: str = "test@example.com") -> MagicMock:
    customer = MagicMock()
    customer.customer_gid = gid
    customer.email = email
    return customer


@pytest.fixture
def mock_pyemvue():
    """Patch pyemvue.PyEmVue with a MagicMock, successful login by default."""
    with patch("pyemvue.PyEmVue") as mock_cls:
        instance = mock_cls.return_value
        instance.login.return_value = True
        instance.customer = _mock_customer()
        instance.auth = MagicMock(tokens=None)
        yield instance


async def test_email_password_success(hass: HomeAssistant, mock_pyemvue) -> None:
    """A valid email/password creates a config entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_EMAIL: "test@example.com", CONF_PASSWORD: "hunter2"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "email_password"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_EMAIL: "test@example.com",
            CONF_PASSWORD: "hunter2",
            "enable_1m": True,
            "enable_1d": True,
            "enable_1mon": True,
            "solar_invert": True,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "test@example.com (42)"
    assert result["data"]["customer_gid"] == "42"


async def test_email_password_invalid_auth(hass: HomeAssistant, mock_pyemvue) -> None:
    """A rejected login shows an invalid_auth error instead of creating an entry."""
    mock_pyemvue.login.return_value = False

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_EMAIL: "test@example.com", CONF_PASSWORD: "wrong"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_EMAIL: "test@example.com",
            CONF_PASSWORD: "wrong",
            "enable_1m": True,
            "enable_1d": True,
            "enable_1mon": True,
            "solar_invert": True,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_email_password_cannot_connect(hass: HomeAssistant, mock_pyemvue) -> None:
    """A network failure during login shows cannot_connect, not invalid_auth."""
    mock_pyemvue.login.side_effect = requests.exceptions.ConnectionError()

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_EMAIL: "test@example.com", CONF_PASSWORD: "hunter2"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_EMAIL: "test@example.com",
            CONF_PASSWORD: "hunter2",
            "enable_1m": True,
            "enable_1d": True,
            "enable_1mon": True,
            "solar_invert": True,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_duplicate_account_aborts(hass: HomeAssistant, mock_pyemvue) -> None:
    """A second config entry for the same Emporia account aborts."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_EMAIL: "test@example.com", CONF_PASSWORD: "hunter2"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_EMAIL: "test@example.com",
            CONF_PASSWORD: "hunter2",
            "enable_1m": True,
            "enable_1d": True,
            "enable_1mon": True,
            "solar_invert": True,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_EMAIL: "test@example.com", CONF_PASSWORD: "hunter2"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_EMAIL: "test@example.com",
            CONF_PASSWORD: "hunter2",
            "enable_1m": True,
            "enable_1d": True,
            "enable_1mon": True,
            "solar_invert": True,
        },
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
