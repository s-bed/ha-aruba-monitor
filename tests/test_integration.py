"""Load the packaged integration through the real Home Assistant lifecycle."""

import os
import shutil
import subprocess
import sys
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components import frontend
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.aruba_monitor import parser
from custom_components.aruba_monitor.aruba import ArubaClient, ArubaError
from custom_components.aruba_monitor.bands import resolve_bands
from custom_components.aruba_monitor.const import DOMAIN, FRONTEND_URL, PANEL_URL
from custom_components.aruba_monitor.store import Store, ssid_id

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

SETTINGS = {
    "site_id": "home",
    "aruba_host": "192.0.2.10",
    "aruba_ip": "192.0.2.10",
    "aruba_user": "test-user",
    "aruba_password": "test-password",
}
FIXTURES = Path(__file__).parent / "fixtures" / "instant_8_10_0_16"


@pytest.fixture
def transport():
    """Mock only controller requests; exercise the real parser and runtime."""
    commands = {
        "show version": "Aruba Operating System Software. Version 8.10.0.16",
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
        return {"Status": "Success", "Status-code": 0, "Command output": commands[kwargs["params"]["cmd"]]}

    with patch.object(ArubaClient, "request", new_callable=AsyncMock, side_effect=request) as mocked:
        yield mocked


async def setup_entry(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    entry = MockConfigEntry(domain=DOMAIN, data=SETTINGS, unique_id="home", version=2)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.state is ConfigEntryState.LOADED
    return entry


async def test_full_setup_registers_admin_panel_and_native_entities(hass, tmp_path, transport):
    entry = await setup_entry(hass, tmp_path)
    coordinator = entry.runtime_data
    assert coordinator.snapshot["status"]["fresh"] is True
    assert coordinator.snapshot["status"]["firmware"] == "8.10.0.16"
    assert coordinator.snapshot["clients"]
    assert coordinator._remove_collector_listener is not None
    assert coordinator._remove_stale_timer is not None
    assert (tmp_path / DOMAIN / "aruba.sqlite3").is_file()
    panel = hass.data[frontend.DATA_PANELS][PANEL_URL]
    assert panel.require_admin is True
    # A configuration panel overrides HA's Configure button and hides Options.
    assert panel.config_panel_domain is None
    assert entry.supports_options is True
    options = await hass.config_entries.options.async_init(entry.entry_id)
    assert options["step_id"] == "init"
    assert {"auto_delete_clients", "client_retention_days", "auto_delete_ssids", "ssid_retention_days"} <= {
        key.schema for key in options["data_schema"].schema
    }
    assert panel.config["_panel_custom"]["name"] == "aruba-monitor-panel"
    assert panel.config["_panel_custom"]["embed_iframe"] is False
    assert panel.config["_panel_custom"]["module_url"].startswith(f"{FRONTEND_URL}/panel.js?")
    assert {state.domain for state in hass.states.async_all()} >= {"sensor", "binary_sensor"}
    assert all(state.domain != "mqtt" for state in hass.states.async_all())
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_unload_and_reload_close_old_runtime_without_duplicate_collector(hass, tmp_path, transport):
    entry = await setup_entry(hass, tmp_path)
    original = entry.runtime_data
    before = original.snapshot["clients"]
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert original._closed is True
    assert original._remove_stale_timer is None
    assert original._remove_collector_listener is None
    assert original.client.sid is None
    assert PANEL_URL not in hass.data[frontend.DATA_PANELS]
    assert (tmp_path / DOMAIN / "aruba.sqlite3").is_file()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    current = entry.runtime_data
    assert current is not original
    assert [(c["id"], c["first_seen"], c["macs"]) for c in current.snapshot["clients"]] == [
        (c["id"], c["first_seen"], c["macs"]) for c in before
    ]
    assert current._remove_collector_listener is not None
    assert len([panel for panel in hass.data[frontend.DATA_PANELS] if panel == PANEL_URL]) == 1
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_offline_startup_keeps_history_but_requires_live_collection(hass, tmp_path, transport):
    hass.config.config_dir = str(tmp_path)

    def create_history():
        store = Store(str(tmp_path / DOMAIN / "aruba.sqlite3"))
        store.ingest(
            [{"mac": "aa:bb:cc:00:00:01", "name": "Historical client", "ssid": "Old", "ap": "Office"}],
            [],
            [{"ssid": "Old"}, {"ssid": "New"}],
        )
        store.create_campaign("Retained campaign", "Old", "New")
        store.close()

    await hass.async_add_executor_job(create_history)
    transport.side_effect = ArubaError("Controller connection failed (check address, TLS and reachability)")
    entry = await setup_entry(hass, tmp_path)
    snapshot = entry.runtime_data.snapshot
    assert snapshot["clients"][0]["name"] == "Historical client"
    assert snapshot["clients"][0]["connection"] == "unknown"
    assert snapshot["campaigns"][0]["name"] == "Retained campaign"
    assert snapshot["campaigns"][0]["ready"] is False
    assert snapshot["status"]["fresh"] is False
    assert snapshot["status"]["last_success"] is not None
    assert PANEL_URL in hass.data[frontend.DATA_PANELS]
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_collector_listener_keeps_polling_without_entity_or_panel_listeners(hass, tmp_path, transport):
    """The integration's own listener is sufficient to keep history collection active."""
    with patch.object(hass.config_entries, "async_forward_entry_setups", new_callable=AsyncMock):
        entry = await setup_entry(hass, tmp_path)
    coordinator = entry.runtime_data
    assert len(coordinator._listeners) == 1
    before = coordinator.inventory_revision
    async_fire_time_changed(hass, datetime.now(UTC) + timedelta(seconds=31))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert coordinator.inventory_revision > before
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_packaged_module_and_styles_are_served_locally(hass, hass_client, tmp_path, transport):
    entry = await setup_entry(hass, tmp_path)
    client = await hass_client()
    response = await client.get(f"{FRONTEND_URL}/panel.js")
    assert response.status == 200
    source = await response.text()
    assert 'customElements.define("aruba-monitor-panel"' in source
    assert "hass.callWS" in source or "_hass.callWS" in source
    assert "/api/v1" not in source
    response = await client.get(f"{FRONTEND_URL}/panel.css")
    assert response.status == 200
    assert "--primary-text-color" in await response.text()
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_invalid_database_does_not_reset_existing_file(hass, tmp_path, transport):
    hass.config.config_dir = str(tmp_path)
    database = tmp_path / DOMAIN / "aruba.sqlite3"
    database.parent.mkdir()
    database.write_bytes(b"This is deliberately not a SQLite database")
    entry = MockConfigEntry(domain=DOMAIN, data=SETTINGS, unique_id="home", version=2)
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert database.read_bytes() == b"This is deliberately not a SQLite database"
    assert transport.await_count == 0


def test_component_imports_with_only_its_packaged_directory(tmp_path):
    """The deployable component imports with only its installed directory present."""
    source = Path(__file__).parents[1] / "custom_components" / DOMAIN
    destination = tmp_path / "custom_components" / DOMAIN
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__"))
    check = """
from importlib import import_module
from pathlib import Path
for module in ("", ".aruba", ".coordinator", ".config_flow", ".sensor", ".binary_sensor", ".device_tracker", ".websocket", ".backup"):
    import_module("custom_components.aruba_monitor" + module)
component = Path("custom_components/aruba_monitor")
assert (component / "frontend/panel.js").is_file()
assert (component / "frontend/panel.css").is_file()
"""
    result = subprocess.run(
        [sys.executable, "-c", check],
        cwd=tmp_path,
        env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


async def test_device_metadata_and_parent_links_refresh_with_retained_inventory(hass, tmp_path, transport):
    """Retained AP entities register before collection and still acquire correct parents."""
    clients = parser.clients((FIXTURES / "clients.txt").read_text())
    aps = parser.aps((FIXTURES / "aps.txt").read_text())
    networks = parser.networks((FIXTURES / "networks.txt").read_text())
    resolve_bands(clients, aps)

    def seed_history():
        store = Store(str(tmp_path / DOMAIN / "aruba.sqlite3"))
        store.ingest(clients, aps, networks)
        store.close()

    await hass.async_add_executor_job(seed_history)
    entry = await setup_entry(hass, tmp_path)
    registry = dr.async_get(hass)
    parent = registry.async_get_device_by_identifier((DOMAIN, "aruba_home"), entry.entry_id)
    assert parent is not None
    assert parent.sw_version == "8.10.0.16"
    devices = dr.async_entries_for_config_entry(registry, entry.entry_id)
    children = [device for device in devices if device.id != parent.id]
    assert len(children) == len(aps) + len(networks) + len(clients)
    assert all(device.via_device_id == parent.id for device in children)
    assert all(device.manufacturer == "HPE Aruba Networking" for device in children if not device.connections)
    assert len([device for device in children if device.connections]) == len(clients)

    original = aps[0]
    identifiers = {(DOMAIN, f"aruba_home_ap_{ssid_id(original['serial'])}")}
    device = registry.async_get_device_by_identifier(next(iter(identifiers)), entry.entry_id)
    assert device is not None
    device_id = device.id
    registry.async_update_device(device_id, name_by_user="My kitchen access point")
    changed_aps = deepcopy(aps)
    changed_aps[0].update(name="Renamed kitchen AP", model="335(indoor)")
    changed_clients = [
        {**client, "ap": "Renamed kitchen AP" if client.get("ap") == original["name"] else client.get("ap")}
        for client in clients
    ]
    with patch.object(
        entry.runtime_data.client,
        "collect",
        new_callable=AsyncMock,
        return_value=(changed_clients, changed_aps, networks),
    ):
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done(wait_background_tasks=True)
    updated = registry.async_get_device_by_identifier(next(iter(identifiers)), entry.entry_id)
    assert updated.id == device_id
    assert updated.identifiers == identifiers
    assert updated.name == "Renamed kitchen AP"
    assert updated.name_by_user == "My kitchen access point"
    assert updated.model == "335(indoor)"
    assert updated.serial_number == original["serial"]
    assert updated.via_device_id == parent.id
    assert all(
        device.via_device_id == parent.id
        for device in dr.async_entries_for_config_entry(registry, entry.entry_id)
        if device.id != parent.id
    )
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_ssid_devices_retained_until_explicit_deletion_without_losing_clients(
    hass, tmp_path, transport
):
    from homeassistant.helpers import entity_registry as er

    entry = await setup_entry(hass, tmp_path)
    coordinator = entry.runtime_data
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    old_ssid = coordinator.snapshot["ssids"][0]
    identifier = (DOMAIN, f"aruba_home_ssid_{old_ssid['id']}")
    device = devices.async_get_device_by_identifier(identifier, entry.entry_id)
    assert device
    native_entities = er.async_entries_for_device(entities, device.id, include_disabled_entities=True)
    assert native_entities
    clients = coordinator.snapshot["clients"]
    with patch.object(coordinator.client, "collect", side_effect=ArubaError("Controller unavailable")):
        await coordinator.async_refresh()
    assert devices.async_get(device.id)
    assert all(entities.async_get(entity.entity_id) for entity in native_entities)
    # All tables succeeded, but this SSID no longer exists on the controller.
    with patch.object(coordinator.client, "collect", return_value=([], [], [])):
        await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert all(not ssid["in_inventory"] for ssid in coordinator.snapshot["ssids"])
    assert devices.async_get(device.id)
    assert all(entities.async_get(entity.entity_id) for entity in native_entities)
    assert not coordinator.metrics[f"ssid_{old_ssid['id']}_clients"].available
    await coordinator.async_mutate("delete_ssid", old_ssid["ssid"])
    await hass.async_block_till_done()
    assert old_ssid["id"] not in {ssid["id"] for ssid in coordinator.snapshot["ssids"]}
    assert not devices.async_get(device.id)
    assert all(not entities.async_get(entity.entity_id) for entity in native_entities)
    assert {row["id"] for row in coordinator.snapshot["clients"]} == {row["id"] for row in clients}
    assert all(row["sightings"] for row in coordinator.snapshot["clients"])
    assert await hass.config_entries.async_unload(entry.entry_id)
