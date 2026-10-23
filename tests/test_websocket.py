"""Exercise real HA WebSocket authentication and isolated campaign writes."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components import websocket_api
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aruba_monitor.aruba import ArubaClient
from custom_components.aruba_monitor.const import DOMAIN
from custom_components.aruba_monitor.websocket import COMMAND_FIELDS, async_register_commands

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
async def monitor(hass, tmp_path):
    """Load the full integration; mock controller transport, not its lifecycle."""
    hass.config.config_dir = str(tmp_path)
    output = {
        "show version": "Aruba Instant Version 8.10.0.16",
        "show clients": (FIXTURES / "clients.txt").read_text(),
        "show aps": (FIXTURES / "aps.txt").read_text(),
        "show network": (FIXTURES / "networks.txt").read_text(),
    }

    async def request(method, path, **kwargs):
        if path == "/rest/login":
            return {"Status": "Success", "sid": "synthetic-test-session"}
        if path == "/rest/logout":
            return {"Status": "Success"}
        assert method == "GET"
        return {"Status": "Success", "Status-code": 0, "Command output": output[kwargs["params"]["cmd"]]}

    with patch.object(ArubaClient, "request", new_callable=AsyncMock, side_effect=request):
        entry = MockConfigEntry(
            domain=DOMAIN,
            unique_id="home",
            data={
                "site_id": "home",
                "aruba_host": "192.0.2.10",
                "aruba_ip": "192.0.2.10",
                "aruba_user": "test-user",
                "aruba_password": "test-password",
            },
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        yield entry
        if entry.runtime_data and not entry.runtime_data._closed:
            assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)


async def command(client, name, /, **fields):
    await client.send_json_auto_id({"type": f"aruba_monitor/{name}", **fields})
    result = await client.receive_json()
    assert result["type"] == "result"
    return result


async def successful(client, name, /, **fields):
    result = await command(client, name, **fields)
    assert result["success"], result
    return result["result"]


@pytest.mark.parametrize("name", COMMAND_FIELDS)
async def test_nonadmins_cannot_read_or_write_any_command(
    hass,
    hass_ws_client,
    hass_read_only_access_token,
    monitor,
    name,
):
    client = await hass_ws_client(hass, access_token=hass_read_only_access_token)
    payload = {key: False if kind is bool else "synthetic" for key, kind in COMMAND_FIELDS[name].items()}
    result = await command(client, name, **payload)
    assert result["success"] is False
    assert result["error"]["code"] == "unauthorized"


async def test_admin_snapshot_uses_cache_without_collecting(hass, hass_ws_client, monitor):
    client = await hass_ws_client(hass)
    coordinator = monitor.runtime_data
    with patch.object(coordinator.client, "collect", new_callable=AsyncMock) as collect:
        snapshot = await successful(client, "snapshot")
    assert set(snapshot) == {
        "status",
        "clients",
        "client_connections",
        "campaigns",
        "aps",
        "ssids",
        "total_clients",
    }
    assert snapshot["status"]["site_id"] == "home"
    assert "mqtt" not in snapshot["status"]
    assert snapshot["total_clients"] == 3
    assert snapshot["status"]["fresh"] is True
    collect.assert_not_awaited()


async def test_panel_hidden_from_nonadmin_frontend_listing(
    hass, hass_ws_client, hass_read_only_access_token, monitor
):
    admin = await hass_ws_client(hass)
    await admin.send_json_auto_id({"type": "get_panels"})
    admin_panels = await admin.receive_json()
    assert "aruba-monitor" in admin_panels["result"]
    user = await hass_ws_client(hass, access_token=hass_read_only_access_token)
    await user.send_json_auto_id({"type": "get_panels"})
    panels = await user.receive_json()
    assert "aruba-monitor" not in panels["result"]


async def test_campaign_identity_and_exclusion_roundtrip(hass, hass_ws_client, monitor):
    client = await hass_ws_client(hass)
    snapshot = await successful(client, "snapshot")
    original = next(c for c in snapshot["clients"] if c["macs"] == ["aa:bb:cc:00:00:01"])
    second = next(c for c in snapshot["clients"] if c["macs"] == ["aa:bb:cc:00:00:02"])
    campaign = await successful(
        client, "campaign_create", name="  Rotation  ", source="Old Wi-Fi", target="New Wi-Fi"
    )
    assert campaign["name"] == "Rotation"
    assert campaign["total"] == 2
    assert campaign["counts"]["pending"] == 2
    assert await successful(client, "client_label", device_id=original["id"], label=" Kitchen speaker ") == {
        "ok": True
    }
    assert await successful(
        client, "client_link", device_id=original["id"], mac=second["macs"][0].upper()
    ) == {"ok": True}
    linked = await successful(client, "snapshot")
    combined = next(c for c in linked["clients"] if c["id"] == original["id"])
    assert combined["label"] == "Kitchen speaker"
    assert set(combined["macs"]) == {"aa:bb:cc:00:00:01", "aa:bb:cc:00:00:02"}
    unlinked = await successful(client, "client_unlink", device_id=original["id"], mac=second["macs"][0])
    assert unlinked["device_id"] != original["id"]
    await successful(
        client,
        "campaign_exclude",
        campaign_id=campaign["id"],
        device_id=original["id"],
        excluded=True,
        note="  Retired hardware  ",
    )
    snapshot = await successful(client, "snapshot")
    excluded = next(c for c in snapshot["campaigns"][0]["clients"] if c["id"] == original["id"])
    assert excluded["migration_state"] == "excluded"
    assert excluded["exclusion_note"] == "Retired hardware"
    await successful(
        client, "campaign_exclude", campaign_id=campaign["id"], device_id=original["id"], excluded=False
    )
    snapshot = await successful(client, "snapshot")
    assert snapshot["campaigns"][0]["counts"]["pending"] == 2
    await successful(client, "campaign_delete", campaign_id=campaign["id"])
    snapshot = await successful(client, "snapshot")
    assert snapshot["campaigns"] == []
    assert len(snapshot["clients"]) == 3
    assert next(c for c in snapshot["clients"] if c["id"] == original["id"])["label"] == "Kitchen speaker"


async def test_completion_requires_new_away_evidence_and_freezes_exclusions(hass, hass_ws_client, monitor):
    client = await hass_ws_client(hass)
    campaign = await successful(
        client, "campaign_create", name="Rotation", source="Old Wi-Fi", target="New Wi-Fi"
    )
    pending = await command(client, "campaign_complete", campaign_id=campaign["id"])
    assert pending["success"] is False
    assert pending["error"]["code"] == "invalid_operation"
    coordinator = monitor.runtime_data
    snapshot = coordinator.snapshot
    observations = [{**c, "ssid": "New Wi-Fi"} for c in snapshot["clients"]]
    at = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
    await hass.async_add_executor_job(
        coordinator.store.ingest, observations, snapshot["aps"], [{"ssid": "New Wi-Fi"}], at
    )
    await coordinator.async_refresh_status()
    assert await successful(client, "campaign_complete", campaign_id=campaign["id"]) == {"ok": True}
    snapshot = await successful(client, "snapshot")
    assert snapshot["campaigns"][0]["completed_at"]
    result = await command(
        client,
        "campaign_exclude",
        campaign_id=campaign["id"],
        device_id=campaign["clients"][0]["id"],
        excluded=True,
        note="Example",
    )
    assert result["success"] is False
    assert "read-only" in result["error"]["message"]
    await successful(client, "campaign_delete", campaign_id=campaign["id"])


@pytest.mark.parametrize(
    ("name", "fields"),
    [
        ("campaign_create", {"name": " ", "source": "Old", "target": "New"}),
        ("campaign_create", {"name": "Rotation", "source": "Same", "target": "Same"}),
        ("campaign_create", {"name": "Rotation", "source": "é" * 17, "target": "New"}),
        ("campaign_create", {"name": "Rotation", "source": "Old\nSSID", "target": "New"}),
        (
            "campaign_exclude",
            {"campaign_id": "missing", "device_id": "missing", "excluded": True, "note": " "},
        ),
        ("client_link", {"device_id": "missing", "mac": "bad-mac"}),
        ("client_label", {"device_id": "missing", "label": "Example"}),
        ("campaign_delete", {"campaign_id": "missing"}),
    ],
)
async def test_invalid_mutations_return_domain_validation_errors(hass, hass_ws_client, monitor, name, fields):
    client = await hass_ws_client(hass)
    response = await command(client, name, **fields)
    assert response["success"] is False
    assert response["error"]["code"] == "invalid_operation"
    assert response["error"]["message"]
    assert monitor.runtime_data.snapshot["campaigns"] == []


async def test_request_schema_rejects_missing_fields_and_wrong_types(hass, hass_ws_client, monitor):
    client = await hass_ws_client(hass)
    for fields in ({}, {"name": 42, "source": "Old", "target": "New"}):
        response = await command(client, "campaign_create", **fields)
        assert response["success"] is False
        assert response["error"]["code"] == "invalid_format"


async def test_internal_errors_are_sanitized_and_backup_blocks_writes(hass, hass_ws_client, monitor, caplog):
    client = await hass_ws_client(hass)
    coordinator = monitor.runtime_data
    device_id = coordinator.snapshot["clients"][0]["id"]
    with patch.object(coordinator.store, "label", side_effect=RuntimeError("private-session-id-do-not-log")):
        response = await command(client, "client_label", device_id=device_id, label="Example")
    assert response["success"] is False
    assert response["error"]["code"] == "operation_failed"
    assert "private-session" not in str(response)
    assert "private-session" not in caplog.text
    await coordinator.async_prepare_backup()
    try:
        response = await command(client, "client_label", device_id=device_id, label="Example")
        assert response["success"] is False
        assert response["error"]["code"] == "unavailable"
        assert (await successful(client, "snapshot"))["status"]["backup_in_progress"] is True
    finally:
        await coordinator.async_finish_backup()


async def test_commands_without_loaded_entry_report_unavailable(hass, hass_ws_client):
    assert await async_setup_component(hass, websocket_api.DOMAIN, {})
    async_register_commands(hass)
    client = await hass_ws_client(hass)
    response = await command(client, "snapshot")
    assert response["success"] is False
    assert response["error"]["code"] == "unavailable"
