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
DEFAULT_OPTIONS = {
    "interval": 30.0,
    "timeout": 15.0,
    "auto_delete_clients": True,
    "track_new_devices": False,
    "client_retention_days": 30,
    "auto_delete_ssids": False,
    "ssid_retention_days": 30,
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
    assert result["options"] == DEFAULT_OPTIONS
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
    assert entry.options == {**DEFAULT_OPTIONS, "interval": 60, "timeout": 7.5}
    assert entry.data == INPUT
    reload.assert_called_once_with(entry.entry_id)
    settings = ControllerSettings.from_entry(entry)
    assert settings.interval == 60
    assert settings.timeout == 7.5
    assert settings.site_id == "home"
    assert settings.auto_delete_clients is True
    assert settings.auto_delete_ssids is False
    assert settings.client_retention_days == 30
    assert settings.ssid_retention_days == 30


async def test_options_save_independent_cleanup_settings_without_changing_identity(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=INPUT, unique_id="home")
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    options = {
        **DEFAULT_OPTIONS,
        "auto_delete_clients": True,
        "client_retention_days": 45,
        "auto_delete_ssids": False,
        "ssid_retention_days": 7,
    }
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        result = await hass.config_entries.options.async_configure(result["flow_id"], user_input=options)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == options
    assert entry.data == INPUT
    settings = ControllerSettings.from_entry(entry)
    assert settings.auto_delete_clients is True
    assert settings.client_retention_days == 45
    assert settings.auto_delete_ssids is False
    assert settings.ssid_retention_days == 7
    reload.assert_called_once_with(entry.entry_id)


async def test_options_form_preserves_saved_cleanup_values(hass):
    options = {
        **DEFAULT_OPTIONS,
        "auto_delete_clients": True,
        "client_retention_days": 60,
        "auto_delete_ssids": True,
        "ssid_retention_days": 90,
    }
    entry = MockConfigEntry(domain=DOMAIN, data=INPUT, options=options)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    fields = {getattr(key, "schema", key): key for key in result["data_schema"].schema}
    for name, value in options.items():
        assert fields[name].default() == value


@pytest.mark.parametrize("option", ["client_retention_days", "ssid_retention_days"])
@pytest.mark.parametrize("value", [0, -1, 1.5, 30.0, "30", float("nan"), float("inf")])
async def test_options_reject_invalid_cleanup_days_without_saving(hass, option, value):
    entry = MockConfigEntry(domain=DOMAIN, data=INPUT)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={**DEFAULT_OPTIONS, option: value}
        )
    assert entry.options == {}


@pytest.mark.parametrize("option", ["client_retention_days", "ssid_retention_days"])
async def test_options_reject_boolean_cleanup_days_without_saving(hass, option):
    entry = MockConfigEntry(domain=DOMAIN, data=INPUT)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={**DEFAULT_OPTIONS, option: True}
    )
    assert result["errors"] == {"base": "invalid_config"}
    assert entry.options == {}


@pytest.mark.parametrize("option", ["auto_delete_clients", "auto_delete_ssids", "track_new_devices"])
@pytest.mark.parametrize("value", [0, 1, "true", None])
async def test_options_reject_non_boolean_cleanup_switches(hass, option, value):
    entry = MockConfigEntry(domain=DOMAIN, data=INPUT)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={**DEFAULT_OPTIONS, option: value}
        )
    assert entry.options == {}


async def test_options_reject_nan_without_saving(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=INPUT, unique_id="home")
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"interval": float("nan"), "timeout": 5}
    )
    assert result["errors"] == {"base": "invalid_config"}
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


@pytest.mark.parametrize("options", [{}, {"interval": 45, "timeout": 10}])
def test_settings_default_to_client_cleanup_and_disabled_tracking(options):
    settings = ControllerSettings.from_entry(MockConfigEntry(domain=DOMAIN, data=INPUT, options=options))
    assert settings.auto_delete_clients is True
    assert settings.auto_delete_ssids is False
    assert settings.client_retention_days == 30
    assert settings.ssid_retention_days == 30


@pytest.mark.parametrize("option", ["client_retention_days", "ssid_retention_days"])
@pytest.mark.parametrize("value", [True, False, 0, -1, 1.5, 30.0, "30", None, float("nan"), float("inf")])
def test_settings_require_positive_integer_cleanup_days(option, value):
    with pytest.raises(ValueError, match="positive whole numbers"):
        ControllerSettings(**{**INPUT, option: value})


@pytest.mark.parametrize("option", ["auto_delete_clients", "auto_delete_ssids", "track_new_devices"])
@pytest.mark.parametrize("value", [0, 1, "true", None])
def test_settings_require_boolean_cleanup_switches(option, value):
    with pytest.raises(ValueError, match="true or false"):
        ControllerSettings(**{**INPUT, option: value})


@pytest.mark.parametrize("interval", [10, 30, 300])
async def test_poll_interval_boundaries_are_accepted(hass, interval):
    entry = MockConfigEntry(domain=DOMAIN, data=INPUT, version=2)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with patch.object(hass.config_entries, "async_schedule_reload"):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={**DEFAULT_OPTIONS, "interval": interval}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert ControllerSettings.from_entry(entry).interval == interval


@pytest.mark.parametrize("interval", [True, False, 9, 301, float("inf"), float("nan")])
def test_settings_reject_invalid_poll_intervals(interval):
    with pytest.raises(ValueError, match="between 10 and 300"):
        ControllerSettings(**{**INPUT, "interval": interval})


@pytest.mark.parametrize("interval", [True, False, 9, 301, float("inf")])
async def test_options_reject_out_of_range_or_boolean_poll_intervals(hass, interval):
    entry = MockConfigEntry(domain=DOMAIN, data=INPUT, version=2)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={**DEFAULT_OPTIONS, "interval": interval}
        )
    assert entry.options == {}


@pytest.mark.parametrize(
    ("interval", "expected"), [(5, 10), (9, 10), (10, 10), (45, 45), (300, 300), (900, 300)]
)
async def test_upgrade_changes_defaults_once_and_normalizes_interval(hass, interval, expected):
    from custom_components.aruba_monitor import async_migrate_entry

    options = {
        "interval": interval,
        "timeout": 8,
        "auto_delete_clients": False,
        "client_retention_days": 90,
        "auto_delete_ssids": True,
        "ssid_retention_days": 45,
    }
    entry = MockConfigEntry(domain=DOMAIN, data=INPUT, options=options, version=1)
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)
    assert entry.version == 2
    assert entry.options == {
        **options,
        "interval": expected,
        "auto_delete_clients": True,
        "track_new_devices": False,
    }
    assert entry.data == INPUT
    saved = {**entry.options, "auto_delete_clients": False, "track_new_devices": True}
    hass.config_entries.async_update_entry(entry, options=saved)
    assert await async_migrate_entry(hass, entry)
    assert entry.options == saved


async def test_upgrade_uses_legacy_interval_from_data_and_preserves_cleanup_setting(hass):
    from custom_components.aruba_monitor import async_migrate_entry

    entry = MockConfigEntry(domain=DOMAIN, data={**INPUT, "interval": 8}, version=1)
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)
    settings = ControllerSettings.from_entry(entry)
    assert settings.interval == 10
    assert settings.auto_delete_clients is True
    assert settings.auto_delete_ssids is False
    assert settings.track_new_devices is False


@pytest.mark.parametrize(("version", "minor_version"), [(3, 1), (2, 2)])
async def test_future_config_versions_are_never_downgraded(hass, version, minor_version):
    from custom_components.aruba_monitor import async_migrate_entry

    entry = MockConfigEntry(domain=DOMAIN, data=INPUT, version=version, minor_version=minor_version)
    entry.add_to_hass(hass)
    assert not await async_migrate_entry(hass, entry)
    assert entry.options == {}
    assert entry.version == version
    assert entry.minor_version == minor_version


async def test_options_schema_serializes_with_poll_interval_selector(hass):
    import json

    import probatio
    from homeassistant.helpers import config_validation as cv

    entry = MockConfigEntry(domain=DOMAIN, data=INPUT, version=2)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    fields = probatio.to_field_list(result["data_schema"], custom_serializer=cv.custom_serializer)
    json.dumps(fields)
    interval = next(field for field in fields if field["name"] == "interval")
    assert interval["selector"]["number"] == {"min": 10, "max": 300, "step": 1, "mode": "box"}
