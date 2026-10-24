"""Exercise per-MAC entities through Home Assistant's native registries."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from conftest import ap, client, inventory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.aruba_monitor.aruba import ArubaClient, ArubaError
from custom_components.aruba_monitor.const import DOMAIN
from custom_components.aruba_monitor.store import ssid_id

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

MAC = "aa:bb:cc:00:00:01"
SECOND_MAC = "aa:bb:cc:00:00:02"
SENSOR_SUFFIXES = {"ip", "ssid", "ap", "band", "channel", "signal_db", "speed_mbps", "last_seen"}
SETTINGS = {
    "site_id": "home",
    "aruba_host": "192.0.2.10",
    "aruba_user": "test-user",
    "aruba_password": "test-password",
}


@pytest.fixture
def controller():
    """Keep controller observations mutable without contacting a real controller."""
    responses = {"clients": [client(band="5 GHz", channel="36")], "error": None}

    async def collect():
        if responses["error"]:
            raise responses["error"]
        return deepcopy(responses["clients"]), [ap()], inventory()

    with (
        patch.object(
            ArubaClient, "show", new_callable=AsyncMock, return_value="Aruba Instant Version 8.10.0.16"
        ),
        patch.object(ArubaClient, "collect", new_callable=AsyncMock, side_effect=collect),
    ):
        yield responses


async def setup_entry(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    entry = MockConfigEntry(domain=DOMAIN, data=SETTINGS, unique_id="home")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.state is ConfigEntryState.LOADED
    return entry


def registered(hass, mac=MAC, suffix="online"):
    registry = er.async_get(hass)
    platform = "binary_sensor" if suffix == "online" else "sensor"
    entity_id = registry.async_get_entity_id(platform, DOMAIN, f"aruba_home_client_{ssid_id(mac)}_{suffix}")
    assert entity_id is not None
    return registry.async_get(entity_id)


async def refresh(hass, entry):
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_client_connectivity_enabled_and_other_sensors_disabled_by_default(hass, tmp_path, controller):
    entry = await setup_entry(hass, tmp_path)
    online = registered(hass)
    assert online.disabled_by is None
    state = hass.states.get(online.entity_id)
    assert state.state == STATE_ON
    assert state.attributes["device_class"] == "connectivity"
    assert online.original_name == "Wi-Fi connected"
    for suffix in SENSOR_SUFFIXES:
        sensor = registered(hass, suffix=suffix)
        assert sensor.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert sensor.device_id == online.device_id
        assert hass.states.get(sensor.entity_id) is None
    devices = dr.async_get(hass)
    device = devices.async_get(online.device_id)
    assert device.identifiers == {(DOMAIN, f"aruba_home_client_{ssid_id(MAC)}")}
    assert device.connections == {(dr.CONNECTION_NETWORK_MAC, MAC)}
    assert device.name == "Kitchen speaker"
    assert device.manufacturer is None
    assert device.model is None
    parent = devices.async_get_device_by_identifier((DOMAIN, "aruba_home"), entry.entry_id)
    assert device.via_device_id == parent.id
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_auto_cleanup_removes_native_client_entities_and_respects_protection(
    hass, tmp_path, controller, freezer
):
    controller["clients"] = [client(), client(mac=SECOND_MAC)]
    entry = await setup_entry(hass, tmp_path)
    coordinator = entry.runtime_data
    protected = next(c for c in coordinator.snapshot["clients"] if c["macs"] == [MAC])
    second = registered(hass, SECOND_MAC)
    entities = er.async_get(hass)
    second_entities = er.async_entries_for_device(entities, second.device_id, include_disabled_entities=True)
    await coordinator.async_mutate("protect_client", protected["id"], True)
    coordinator.settings = replace(coordinator.settings, auto_delete_clients=True)
    controller["clients"] = []
    freezer.move_to(datetime.now(UTC) + timedelta(days=31))
    await refresh(hass, entry)
    assert [c["macs"] for c in coordinator.snapshot["clients"]] == [[MAC]]
    assert coordinator.snapshot["clients"][0]["protected"]
    assert hass.states.get(registered(hass).entity_id).state == STATE_OFF
    assert all(not entities.async_get(item.entity_id) for item in second_entities)
    assert not dr.async_get(hass).async_get(second.device_id)
    controller["clients"] = [client(mac=SECOND_MAC)]
    await refresh(hass, entry)
    assert hass.states.get(registered(hass, SECOND_MAC).entity_id).state == STATE_ON
    assert len(coordinator.snapshot["clients"]) == 2
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_late_clients_discovered_and_disconnect_keeps_stable_entities(hass, tmp_path, controller):
    entry = await setup_entry(hass, tmp_path)
    first = registered(hass)
    controller["clients"] = [client(mac=SECOND_MAC)]
    await refresh(hass, entry)
    second = registered(hass, SECOND_MAC)
    assert second.device_id != first.device_id  # A shared hostname is not physical identity.
    assert hass.states.get(first.entity_id).state == STATE_OFF
    assert hass.states.get(second.entity_id).state == STATE_ON
    assert registered(hass).device_id == first.device_id
    for suffix in SENSOR_SUFFIXES:
        assert registered(hass, SECOND_MAC, suffix).disabled_by is er.RegistryEntryDisabler.INTEGRATION

    controller["error"] = ArubaError("Controller unavailable")
    await refresh(hass, entry)
    assert hass.states.get(first.entity_id).state == STATE_UNAVAILABLE
    assert hass.states.get(second.entity_id).state == STATE_UNAVAILABLE
    assert registered(hass).entity_id == first.entity_id
    assert registered(hass, SECOND_MAC).entity_id == second.entity_id

    controller["error"] = None
    controller["clients"] = [client()]
    await refresh(hass, entry)
    assert hass.states.get(first.entity_id).state == STATE_ON
    assert hass.states.get(second.entity_id).state == STATE_OFF
    assert registered(hass, SECOND_MAC).device_id == second.device_id
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("foreign_first", [True, False], ids=["existing-device", "late-device"])
async def test_same_mac_matches_native_devices_without_changing_foreign_metadata(
    hass, tmp_path, controller, foreign_first
):
    foreign_entry = MockConfigEntry(domain="test", unique_id="foreign-client")
    foreign_entry.add_to_hass(hass)
    registry = dr.async_get(hass)

    def create_foreign():
        return registry.async_get_or_create(
            config_entry_id=foreign_entry.entry_id,
            identifiers={("test", "speaker-serial")},
            connections={(dr.CONNECTION_NETWORK_MAC, "AA-BB-CC-00-00-01")},
            name="Speaker from native integration",
            manufacturer="Speaker manufacturer",
            model="Speaker model",
            sw_version="2.0",
        )

    if foreign_first:
        foreign = create_foreign()
    entry = await setup_entry(hass, tmp_path)
    if not foreign_first:
        foreign = create_foreign()
    registry.async_update_device(foreign.id, name_by_user="Living room speaker")
    await refresh(hass, entry)

    aruba = registry.async_get(registered(hass).device_id)
    matches = registry.async_get_devices(connections={(dr.CONNECTION_NETWORK_MAC, MAC)})
    assert {device.id for device in matches} == {foreign.id, aruba.id}
    assert aruba.config_entry_id == entry.entry_id
    foreign = registry.async_get(foreign.id)
    assert foreign.config_entry_id == foreign_entry.entry_id
    assert foreign.name == "Speaker from native integration"
    assert foreign.name_by_user == "Living room speaker"
    assert foreign.manufacturer == "Speaker manufacturer"
    assert foreign.model == "Speaker model"
    assert foreign.sw_version == "2.0"
    assert foreign.identifiers == {("test", "speaker-serial")}
    assert aruba.manufacturer is None
    assert aruba.model is None

    controller["clients"].append(client(mac=SECOND_MAC))
    await refresh(hass, entry)
    second = registered(hass, SECOND_MAC)
    assert second.device_id not in {device.id for device in matches}
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_reload_preserves_enabled_sensor_and_requires_new_live_collection(
    hass, tmp_path, controller, freezer
):
    entry = await setup_entry(hass, tmp_path)
    online = registered(hass)
    ip = registered(hass, suffix="ip")
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    registry = er.async_get(hass)
    registry.async_update_entity(ip.entity_id, disabled_by=None)
    controller["error"] = ArubaError("Controller unavailable")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert registered(hass).entity_id == online.entity_id
    assert registered(hass).device_id == online.device_id
    assert registered(hass, suffix="ip").disabled_by is None
    assert hass.states.get(online.entity_id).state == STATE_UNAVAILABLE
    assert hass.states.get(ip.entity_id).state == STATE_UNAVAILABLE
    for suffix in SENSOR_SUFFIXES - {"ip"}:
        assert registered(hass, suffix=suffix).disabled_by is er.RegistryEntryDisabler.INTEGRATION

    controller["error"] = None
    await refresh(hass, entry)
    assert hass.states.get(online.entity_id).state == STATE_ON
    assert hass.states.get(ip.entity_id).state == "192.0.2.24"
    controller["error"] = ArubaError("Controller unavailable")
    await refresh(hass, entry)
    assert hass.states.get(online.entity_id).state == STATE_UNAVAILABLE
    assert hass.states.get(ip.entity_id).state == "192.0.2.24"
    later = datetime.now(UTC) + timedelta(seconds=91)
    freezer.move_to(later)
    async_fire_time_changed(hass, later)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(ip.entity_id).state == STATE_UNAVAILABLE

    controller["error"] = None
    controller["clients"] = []
    await refresh(hass, entry)
    assert hass.states.get(online.entity_id).state == STATE_OFF
    assert hass.states.get(ip.entity_id).state == STATE_UNAVAILABLE
    assert await hass.config_entries.async_unload(entry.entry_id)
