"""Configuration flows exercise the real Home Assistant flow manager."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aruba_monitor.aruba import ArubaError, AuthError, UnsupportedCommand
from custom_components.aruba_monitor.config import ControllerSettings
from custom_components.aruba_monitor.config_flow import async_validate_connection
from custom_components.aruba_monitor.const import DOMAIN
from custom_components.aruba_monitor.parser import ParseError

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

INPUT = {
    "site_id": "home",
    "aruba_host": "192.0.2.10",
    "aruba_ip": "192.0.2.10",
    "aruba_port": 4343,
    "aruba_user": "test-user",
    "aruba_password": "very-secret-password",
    "aruba_verify_tls": True,
    "aruba_ca": "",
}


@pytest.fixture
def validate_connection():
    with patch(
        "custom_components.aruba_monitor.config_flow.async_validate_connection", new_callable=AsyncMock
    ) as validate:
        yield validate


@pytest.fixture
def prevent_setup(hass):
    with patch.object(hass.config_entries, "async_setup", return_value=True):
        yield


async def test_user_flow_authenticates_and_validates_tables(hass, validate_connection, prevent_setup):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input=INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Aruba home"
    assert result["data"] == INPUT
    assert result["options"] == {"interval": 30.0, "timeout": 15.0}
    assert result["result"].unique_id == "home"
    validate_connection.assert_awaited_once_with(hass, INPUT)
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (AuthError("password secret-session"), "invalid_auth"),
        (UnsupportedCommand("unsupported"), "unsupported_controller"),
        (ParseError("private data"), "invalid_response"),
        (ArubaError("cannot connect"), "cannot_connect"),
        (ValueError("bad settings"), "invalid_config"),
        (RuntimeError("very-secret-password secret-session"), "unknown"),
    ],
)
async def test_setup_errors_are_actionable_and_safe(hass, validate_connection, error, code, caplog):
    validate_connection.side_effect = error
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}, data=INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": code}
    assert "very-secret-password" not in caplog.text
    assert "secret-session" not in caplog.text


async def test_only_one_instance_can_be_configured(hass, validate_connection):
    MockConfigEntry(domain=DOMAIN, data=INPUT, unique_id="home").add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"
    validate_connection.assert_not_awaited()


async def test_another_completed_flow_during_validation_cannot_create_a_second_entry(
    hass, validate_connection
):
    async def another_setup(_hass, _data):
        MockConfigEntry(domain=DOMAIN, data={**INPUT, "site_id": "other"}, unique_id="other").add_to_hass(
            hass
        )

    validate_connection.side_effect = another_setup
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}, data=INPUT
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_reauthentication_updates_credentials_and_reloads(hass, validate_connection):
    entry = MockConfigEntry(
        domain=DOMAIN, data=INPUT, unique_id="home", options={"interval": 45, "timeout": 10}
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )
    assert result["step_id"] == "reauth_confirm"
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"aruba_user": "new-user", "aruba_password": "new-password"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["aruba_password"] == "new-password"
    assert entry.data["site_id"] == "home"
    assert entry.unique_id == "home"
    assert validate_connection.call_args.args[1]["timeout"] == 10
    reload.assert_called_once_with(entry.entry_id)


async def test_reauthentication_keeps_old_password_on_validation_failure(hass, validate_connection):
    entry = MockConfigEntry(domain=DOMAIN, data=INPUT, unique_id="home")
    entry.add_to_hass(hass)
    validate_connection.side_effect = AuthError("invalid")
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"aruba_user": "test-user", "aruba_password": "wrong-password"}
    )
    assert result["errors"] == {"base": "invalid_auth"}
    assert entry.data["aruba_password"] == "very-secret-password"


async def test_reconfigure_preserves_identity_and_blank_password(hass, validate_connection):
    entry = MockConfigEntry(domain=DOMAIN, data=INPUT, unique_id="home")
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
    )
    assert result["step_id"] == "reconfigure"
    fields = {getattr(key, "schema", key) for key in result["data_schema"].schema}
    assert "site_id" not in fields
    updates = {key: value for key, value in INPUT.items() if key != "site_id"}
    updates.update(aruba_host="192.0.2.11", aruba_password="")
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input=updates)
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["aruba_host"] == "192.0.2.11"
    assert entry.data["aruba_password"] == INPUT["aruba_password"]
    assert entry.data["site_id"] == "home"
    assert entry.unique_id == "home"
    reload.assert_called_once_with(entry.entry_id)


async def test_options_change_timing_without_changing_identity(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=INPUT, unique_id="home")
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={"interval": 60, "timeout": 7.5}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {"interval": 60, "timeout": 7.5}
    assert entry.data == INPUT
    reload.assert_called_once_with(entry.entry_id)
    settings = ControllerSettings.from_entry(entry)
    assert settings.interval == 60
    assert settings.timeout == 7.5
    assert settings.site_id == "home"


async def test_options_reject_nan_without_saving(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=INPUT, unique_id="home")
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={"interval": float("nan"), "timeout": 5}
        )
    assert entry.options == {}


async def test_options_reject_infinite_timeout_without_saving(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=INPUT, unique_id="home")
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"interval": 30, "timeout": float("inf")}
    )
    assert result["errors"] == {"base": "invalid_config"}
    assert entry.options == {}


async def test_validation_checks_version_and_full_snapshot_and_logs_out(hass):
    with patch("custom_components.aruba_monitor.config_flow.ArubaClient", autospec=True) as client_type:
        client = client_type.return_value
        await async_validate_connection(hass, INPUT)
    client.show.assert_awaited_once_with("show version")
    client.collect.assert_awaited_once()
    client.close.assert_awaited_once()


async def test_validation_logs_out_after_parse_failure(hass):
    with patch("custom_components.aruba_monitor.config_flow.ArubaClient", autospec=True) as client_type:
        client = client_type.return_value
        client.collect.side_effect = ParseError("Malformed table")
        with pytest.raises(ParseError):
            await async_validate_connection(hass, INPUT)
    client.close.assert_awaited_once()


def test_options_cannot_override_site_or_connection_settings():
    entry = MockConfigEntry(
        domain=DOMAIN, data=INPUT, options={"site_id": "different", "aruba_host": "other"}
    )
    settings = ControllerSettings.from_entry(entry)
    assert settings.site_id == "home"
    assert settings.aruba_host == "192.0.2.10"
