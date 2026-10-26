"""Exercise real HA WebSocket authentication and isolated campaign writes."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components import websocket_api
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aruba_monitor.aruba import ArubaClient, ArubaError
from custom_components.aruba_monitor.const import DOMAIN
from custom_components.aruba_monitor.store import ssid_id
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
            version=2,
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


def client_device(hass, monitor, mac):
    """Find the actual per-MAC native HA record, independently of panel grouping."""
    return dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, f"aruba_home_client_{ssid_id(mac)}"), monitor.entry_id
    )


@pytest.mark.parametrize("foreign_first", [True, False], ids=["existing-match", "later-match"])
async def test_client_devices_read_native_and_normalized_mac_links_from_cache(
    hass, hass_ws_client, monitor, foreign_first
):
    client = await hass_ws_client(hass)
    registry = dr.async_get(hass)
    retained = next(c for c in monitor.runtime_data.snapshot["clients"] if c["macs"] == ["aa:bb:cc:00:00:01"])
    native = client_device(hass, monitor, retained["macs"][0])
    foreign_entry = MockConfigEntry(domain="test", data={"password": "private-device-setting"})
    foreign_entry.add_to_hass(hass)
    if not foreign_first:
        initial = await successful(client, "client_devices", device_id=retained["id"])
        assert initial["linked_devices"] == []
    foreign = registry.async_get_or_create(
        config_entry_id=foreign_entry.entry_id,
        identifiers={("test", "speaker-serial")},
        connections={(dr.CONNECTION_NETWORK_MAC, "AA-BB-CC-00-00-01")},
        name="Original device name",
        manufacturer="Manufacturer",
        model="Model",
    )
    foreign = registry.async_update_device(foreign.id, name_by_user="<Kitchen speaker>")
    with (
        patch.object(monitor.runtime_data.client, "collect", new_callable=AsyncMock) as collect,
        patch.object(monitor.runtime_data, "async_snapshot", new_callable=AsyncMock) as snapshot,
    ):
        result = await successful(client, "client_devices", device_id=retained["id"])
    assert result == {
        "devices": [{"id": native.id, "name": native.name, "macs": retained["macs"], "integration": DOMAIN}],
        "linked_devices": [
            {"id": foreign.id, "name": "<Kitchen speaker>", "macs": retained["macs"], "integration": "test"}
        ],
        "device_options": [
            {"id": foreign.id, "name": "<Kitchen speaker>", "macs": retained["macs"], "integration": "test"}
        ],
    }
    collect.assert_not_awaited()
    snapshot.assert_not_awaited()
    assert "private-device-setting" not in str(result)
    assert registry.async_get(foreign.id) is foreign
    registry.async_remove_device(foreign.id)
    assert (await successful(client, "client_devices", device_id=retained["id"]))["linked_devices"] == []


async def test_client_devices_grouped_macs_keep_native_records_and_deduplicate_shared_links(
    hass, hass_ws_client, monitor
):
    client = await hass_ws_client(hass)
    clients = monitor.runtime_data.snapshot["clients"]
    original = next(c for c in clients if c["macs"] == ["aa:bb:cc:00:00:01"])
    second = next(c for c in clients if c["macs"] == ["aa:bb:cc:00:00:02"])
    native_ids = {client_device(hass, monitor, value).id for value in [*original["macs"], *second["macs"]]}
    await successful(client, "client_link", device_id=original["id"], mac=second["macs"][0])
    foreign_entry = MockConfigEntry(domain="test")
    foreign_entry.add_to_hass(hass)
    registry = dr.async_get(hass)
    foreign = registry.async_get_or_create(
        config_entry_id=foreign_entry.entry_id,
        identifiers={("test", "shared-device")},
        connections={
            (dr.CONNECTION_NETWORK_MAC, "AA:BB:CC:00:00:01"),
            (dr.CONNECTION_NETWORK_MAC, "aa-bb-cc-00-00-02"),
            (dr.CONNECTION_NETWORK_MAC, "aa:bb:cc:00:00:99"),
        },
        name="One physical device",
    )
    unrelated = registry.async_get_or_create(
        config_entry_id=foreign_entry.entry_id,
        identifiers={("test", "same-hostname")},
        connections={(dr.CONNECTION_NETWORK_MAC, "aa:bb:cc:00:00:98")},
        name=original["name"],
    )
    result = await successful(client, "client_devices", device_id=original["id"])
    assert {device["id"] for device in result["devices"]} == native_ids
    assert result["linked_devices"] == [
        {
            "id": foreign.id,
            "name": "One physical device",
            "macs": sorted([*original["macs"], *second["macs"]]),
            "integration": "test",
        }
    ]
    assert unrelated.id not in {device["id"] for device in result["linked_devices"]}
    assert {device["id"] for device in result["device_options"]} == {foreign.id, unrelated.id}


async def test_client_devices_missing_native_record_still_resolves_exact_foreign_mac(
    hass, hass_ws_client, monitor
):
    client = await hass_ws_client(hass)
    retained = monitor.runtime_data.snapshot["clients"][0]
    registry = dr.async_get(hass)
    registry.async_remove_device(client_device(hass, monitor, retained["macs"][0]).id)
    assert await successful(client, "client_devices", device_id=retained["id"]) == {
        "devices": [],
        "linked_devices": [],
        "device_options": [],
    }
    foreign_entry = MockConfigEntry(domain="test")
    foreign_entry.add_to_hass(hass)
    foreign = registry.async_get_or_create(
        config_entry_id=foreign_entry.entry_id,
        identifiers={("test", "unnamed-device")},
        connections={(dr.CONNECTION_NETWORK_MAC, retained["macs"][0])},
    )
    foreign = registry.async_update_device(foreign.id, name=None)
    result = await successful(client, "client_devices", device_id=retained["id"])
    assert result == {
        "devices": [],
        "linked_devices": [
            {"id": foreign.id, "name": retained["macs"][0], "macs": retained["macs"], "integration": "test"}
        ],
        "device_options": [
            {"id": foreign.id, "name": retained["macs"][0], "macs": retained["macs"], "integration": "test"}
        ],
    }
    response = await command(client, "client_devices", device_id="missing")
    assert response["success"] is False
    assert response["error"] == {"code": "invalid_operation", "message": "Unknown client"}


async def test_manual_client_device_link_adds_grouped_macs_without_changing_other_metadata(
    hass, hass_ws_client, monitor
):
    client = await hass_ws_client(hass)
    coordinator = monitor.runtime_data
    retained, second = coordinator.snapshot["clients"][:2]
    await successful(client, "client_link", device_id=retained["id"], mac=second["macs"][0])
    grouped = next(c for c in coordinator.snapshot["clients"] if c["id"] == retained["id"])
    # Retained client MACs are canonical in storage; tolerate uppercase in cached inputs.
    grouped["macs"] = [value.upper() for value in grouped["macs"]]
    connections = {(dr.CONNECTION_NETWORK_MAC, value.lower()) for value in grouped["macs"]}
    registry = dr.async_get(hass)
    foreign_entry = MockConfigEntry(domain="test", data={"password": "private-setting"})
    foreign_entry.add_to_hass(hass)
    target = registry.async_get_or_create(
        config_entry_id=foreign_entry.entry_id,
        identifiers={("test", "speaker-serial")},
        connections={("bluetooth", "01:02:03:04:05:06")},
        name="Speaker",
        manufacturer="Manufacturer",
        model="Model",
        serial_number="serial-123",
        sw_version="1.0",
    )
    target = registry.async_update_device(target.id, name_by_user="Kitchen speaker")
    original = target.dict_repr
    native = [client_device(hass, monitor, value.lower()) for value in grouped["macs"]]
    with patch.object(coordinator.client, "collect", new_callable=AsyncMock) as collect:
        result = await successful(
            client, "client_device_link", device_id=retained["id"], target_device_id=target.id
        )
    updated = registry.async_get(target.id)
    assert updated.connections == target.connections | connections
    assert {
        key: value for key, value in updated.dict_repr.items() if key not in {"connections", "modified_at"}
    } == {key: value for key, value in original.items() if key not in {"connections", "modified_at"}}
    assert result["linked_devices"] == [
        {
            "id": target.id,
            "name": "Kitchen speaker",
            "macs": sorted(value.lower() for value in grouped["macs"]),
            "integration": "test",
        }
    ]
    assert result["device_options"] == result["linked_devices"]
    for connection in connections:
        assert target.id in {device.id for device in registry.async_get_devices(connections={connection})}
    assert all(registry.async_get(device.id) is device for device in native)
    assert "private-setting" not in str(result)
    collect.assert_not_awaited()
    with patch.object(registry, "async_update_device", wraps=registry.async_update_device) as update:
        assert (
            await successful(
                client, "client_device_link", device_id=retained["id"], target_device_id=target.id
            )
            == result
        )
    update.assert_not_called()


@pytest.mark.parametrize("invalid", ["unknown-client", "unknown-target", "aruba-target", "child-target"])
async def test_manual_client_device_link_rejects_invalid_selections(hass, hass_ws_client, monitor, invalid):
    client = await hass_ws_client(hass)
    retained = monitor.runtime_data.snapshot["clients"][0]
    registry = dr.async_get(hass)
    foreign_entry = MockConfigEntry(domain="test")
    foreign_entry.add_to_hass(hass)
    target = registry.async_get_or_create(
        config_entry_id=foreign_entry.entry_id,
        identifiers={("test", "speaker-serial")},
        name="Speaker",
    )
    child = registry.async_get_or_create_child(
        config_entry_id=foreign_entry.entry_id,
        identifiers={("test", "child")},
        parent_device_id=target.id,
        name="Child device",
    )
    choices = await successful(client, "client_devices", device_id=retained["id"])
    assert [device["id"] for device in choices["device_options"]] == [target.id]
    device_id = "missing" if invalid == "unknown-client" else retained["id"]
    target_id = {
        "unknown-client": target.id,
        "unknown-target": "missing",
        "aruba-target": client_device(hass, monitor, retained["macs"][0]).id,
        "child-target": child.id,
    }[invalid]
    with patch.object(registry, "async_update_device", wraps=registry.async_update_device) as update:
        result = await command(client, "client_device_link", device_id=device_id, target_device_id=target_id)
    assert result["success"] is False
    assert result["error"]["code"] == "invalid_operation"
    update.assert_not_called()
    assert registry.async_get(target.id) is target


async def test_manual_client_device_link_rejects_same_owner_mac_collision_without_merging(
    hass, hass_ws_client, monitor
):
    client = await hass_ws_client(hass)
    retained = monitor.runtime_data.snapshot["clients"][0]
    registry = dr.async_get(hass)
    foreign_entry = MockConfigEntry(domain="test")
    foreign_entry.add_to_hass(hass)
    target = registry.async_get_or_create(
        config_entry_id=foreign_entry.entry_id, identifiers={("test", "target")}, name="Target"
    )
    existing = registry.async_get_or_create(
        config_entry_id=foreign_entry.entry_id,
        identifiers={("test", "existing")},
        connections={(dr.CONNECTION_NETWORK_MAC, retained["macs"][0].upper())},
        name="Existing",
    )
    with patch.object(registry, "async_update_device", wraps=registry.async_update_device) as update:
        result = await command(
            client, "client_device_link", device_id=retained["id"], target_device_id=target.id
        )
    assert result["success"] is False
    assert result["error"] == {
        "code": "invalid_operation",
        "message": "Another device in that integration already uses an observed MAC address",
    }
    update.assert_not_called()
    assert registry.async_get(target.id) is target
    assert registry.async_get(existing.id) is existing


@pytest.mark.parametrize("flag", ["_paused", "_closed"])
async def test_manual_client_device_link_blocks_writes_during_backup_or_unload(
    hass, hass_ws_client, monitor, flag
):
    client = await hass_ws_client(hass)
    coordinator = monitor.runtime_data
    retained = coordinator.snapshot["clients"][0]
    registry = dr.async_get(hass)
    setattr(coordinator, flag, True)
    try:
        with patch.object(registry, "async_update_device", wraps=registry.async_update_device) as update:
            result = await command(
                client, "client_device_link", device_id=retained["id"], target_device_id="any-device"
            )
        assert result["success"] is False
        assert result["error"]["code"] == "unavailable"
        update.assert_not_called()
    finally:
        setattr(coordinator, flag, False)


@pytest.mark.parametrize("invalid_macs", [[], ["invalid"], [None]])
async def test_manual_client_device_link_validates_cached_macs_before_mutation(
    hass, hass_ws_client, monitor, invalid_macs
):
    client = await hass_ws_client(hass)
    retained = monitor.runtime_data.snapshot["clients"][0]
    retained["macs"] = invalid_macs
    registry = dr.async_get(hass)
    with patch.object(registry, "async_update_device", wraps=registry.async_update_device) as update:
        result = await command(
            client, "client_device_link", device_id=retained["id"], target_device_id="any-device"
        )
    assert result["success"] is False
    assert result["error"]["code"] == "invalid_operation"
    update.assert_not_called()


async def test_panel_hidden_from_nonadmin_frontend_listing(
    hass, hass_ws_client, hass_read_only_access_token, monitor
):
    admin = await hass_ws_client(hass)
    await admin.send_json_auto_id({"type": "get_panels"})
    admin_panels = await admin.receive_json()
    assert "aruba-monitor" in admin_panels["result"]
    assert admin_panels["result"]["aruba-monitor"]["config_panel_domain"] is None
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


async def test_campaign_deletion_rejects_outage_without_losing_saved_history(hass, hass_ws_client, monitor):
    client = await hass_ws_client(hass)
    campaign = await successful(
        client, "campaign_create", name="Preserve on outage", source="Old Wi-Fi", target="New Wi-Fi"
    )
    coordinator = monitor.runtime_data
    with patch.object(coordinator.client, "collect", side_effect=ArubaError("Synthetic controller outage")):
        await coordinator.async_refresh()
    before = await successful(client, "snapshot")
    assert not before["status"]["fresh"]
    result = await command(client, "campaign_delete", campaign_id=campaign["id"])
    assert result["success"] is False
    assert result["error"]["code"] == "invalid_operation"
    assert "fresh successful collection" in result["error"]["message"]
    after = await successful(client, "snapshot")
    assert after["campaigns"] == before["campaigns"]
    await coordinator.async_refresh()
    assert coordinator.snapshot["status"]["fresh"]
    await successful(client, "campaign_delete", campaign_id=campaign["id"])
    assert (await successful(client, "snapshot"))["campaigns"] == []


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


async def test_client_protection_and_manual_deletion_preserve_foreign_devices(hass, hass_ws_client, monitor):
    from homeassistant.helpers import entity_registry as er

    connection = await hass_ws_client(hass)
    coordinator = monitor.runtime_data
    retained = coordinator.snapshot["clients"][0]
    device = client_device(hass, monitor, retained["macs"][0])
    entities = er.async_get(hass)
    native_entities = er.async_entries_for_device(entities, device.id, include_disabled_entities=True)
    registry = dr.async_get(hass)
    foreign_entry = MockConfigEntry(domain="test", unique_id="retained-foreign")
    foreign_entry.add_to_hass(hass)
    foreign = registry.async_get_or_create(
        config_entry_id=foreign_entry.entry_id,
        identifiers={("test", "foreign")},
        connections={(dr.CONNECTION_NETWORK_MAC, retained["macs"][0])},
        name="Foreign client",
        manufacturer="Preserved manufacturer",
    )
    before = coordinator.inventory_revision
    with patch.object(coordinator.client, "collect", new_callable=AsyncMock) as collect:
        await successful(connection, "client_protect", device_id=retained["id"], protected=True)
        assert next(c for c in coordinator.snapshot["clients"] if c["id"] == retained["id"])["protected"]
        rejected = await command(connection, "client_delete", device_id=retained["id"])
        assert not rejected["success"]
        assert rejected["error"]["code"] == "invalid_operation"
        assert coordinator.inventory_revision == before
        collect.assert_not_awaited()
    with patch.object(coordinator.client, "collect", return_value=([], [], [])):
        await coordinator.async_refresh()
    with patch.object(coordinator.client, "collect", new_callable=AsyncMock) as collect:
        await successful(connection, "client_delete", device_id=retained["id"])
        collect.assert_not_awaited()
    await hass.async_block_till_done()
    assert retained["id"] not in {c["id"] for c in coordinator.snapshot["clients"]}
    assert not registry.async_get(device.id)
    assert all(not entities.async_get(item.entity_id) for item in native_entities)
    assert registry.async_get(foreign.id) is foreign


@pytest.mark.parametrize("completed", [False, True])
async def test_manual_client_deletion_removes_saved_campaign_membership(
    hass, hass_ws_client, monitor, completed
):
    connection = await hass_ws_client(hass)
    coordinator = monitor.runtime_data
    campaign = await successful(
        connection, "campaign_create", name="Rotation", source="Old Wi-Fi", target="New Wi-Fi"
    )
    deleted = campaign["clients"][0]
    await successful(
        connection,
        "campaign_exclude",
        campaign_id=campaign["id"],
        device_id=deleted["id"],
        excluded=True,
        note="Retired client",
    )
    observations = [{**row, "ssid": "New Wi-Fi"} for row in coordinator.snapshot["clients"]]
    at = datetime.now(UTC) + timedelta(seconds=1)
    await hass.async_add_executor_job(coordinator.store.ingest, observations, [], [], at.isoformat())
    await coordinator.async_refresh_status()
    if completed:
        await successful(connection, "campaign_complete", campaign_id=campaign["id"])
    await hass.async_add_executor_job(
        coordinator.store.ingest, [], [], [], (at + timedelta(seconds=1)).isoformat()
    )
    await coordinator.async_refresh_status()
    before = coordinator.inventory_revision

    with patch.object(coordinator.client, "collect", new_callable=AsyncMock) as collect:
        assert await successful(connection, "client_delete", device_id=deleted["id"]) == {"ok": True}
        snapshot = await successful(connection, "snapshot")
        collect.assert_not_awaited()

    assert coordinator.inventory_revision == before + 1
    assert deleted["id"] not in {row["id"] for row in snapshot["clients"]}
    saved = snapshot["campaigns"][0]
    assert saved["id"] == campaign["id"]
    assert bool(saved["completed_at"]) is completed
    assert saved["total"] == 1
    assert saved["counts"]["excluded"] == 0
    assert saved["counts"]["confirmed"] == 1
    assert saved["progress_pct"] == 100
    assert deleted["id"] not in {row["id"] for row in saved["clients"]}


async def test_ssid_deletion_requires_absence_and_preserves_history(hass, hass_ws_client, monitor):
    connection = await hass_ws_client(hass)
    coordinator = monitor.runtime_data
    before = coordinator.snapshot["clients"]
    rejected = await command(connection, "ssid_delete", ssid="Old Wi-Fi")
    assert not rejected["success"]
    assert rejected["error"]["code"] == "invalid_operation"
    with patch.object(coordinator.client, "collect", return_value=([], [], [])):
        await coordinator.async_refresh()
    await successful(connection, "ssid_delete", ssid="Old Wi-Fi")
    assert "Old Wi-Fi" not in {s["ssid"] for s in coordinator.snapshot["ssids"]}
    assert [c["sightings"] for c in coordinator.snapshot["clients"]] == [c["sightings"] for c in before]


@pytest.mark.parametrize("flag", ["_paused", "_closed"])
@pytest.mark.parametrize(
    ("name", "fields"),
    [
        ("client_delete", {"device_id": "any"}),
        ("client_protect", {"device_id": "any", "protected": True}),
        ("ssid_delete", {"ssid": "Old Wi-Fi"}),
    ],
)
async def test_cleanup_commands_blocked_during_backup_and_unload(
    hass, hass_ws_client, monitor, flag, name, fields
):
    connection = await hass_ws_client(hass)
    coordinator = monitor.runtime_data
    setattr(coordinator, flag, True)
    try:
        result = await command(connection, name, **fields)
        assert not result["success"]
        assert result["error"]["code"] == "unavailable"
    finally:
        setattr(coordinator, flag, False)
