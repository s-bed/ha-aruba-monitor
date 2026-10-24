"""Native metric identity, availability and dynamic registry lifecycle."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import ap, client, inventory
from homeassistant.components.sensor import SensorStateClass
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aruba_monitor.binary_sensor import ArubaBinarySensor
from custom_components.aruba_monitor.const import DOMAIN
from custom_components.aruba_monitor.entity import async_setup_metrics, build_metrics
from custom_components.aruba_monitor.sensor import ArubaSensor
from custom_components.aruba_monitor.store import ssid_id


def make_snapshot(store):
    store.ingest([client(band="5 GHz")], [ap()], inventory())
    return store.snapshot()


def fake_coordinator(metrics):
    listeners = []

    def add_listener(listener):
        listeners.append(listener)
        return lambda: listeners.remove(listener)

    return SimpleNamespace(
        metrics=metrics,
        settings=SimpleNamespace(site_id="home"),
        _closed=False,
        inventory_revision=0,
        async_add_listener=add_listener,
        listeners=listeners,
    )


def test_metrics_preserve_unknown_values_and_report_native_units(store):
    snapshot = make_snapshot(store)
    metrics = build_metrics(snapshot, "home")
    ap_id = ssid_id("SERIAL01")
    assert metrics[f"ap_{ap_id}_radio_0_band"].value is None
    assert metrics[f"ap_{ap_id}_radio_0_channel"].value == "36+"
    assert metrics[f"ap_{ap_id}_radio_0_power_dbm"].unit == "dBm"
    assert metrics[f"ap_{ap_id}_radio_0_utilization_pct"].unit == "%"
    assert metrics[f"ap_{ap_id}_radio_0_noise_floor_dbm"].value == -94
    assert metrics["last_success"].value.tzinfo is not None
    assert metrics["last_success"].device_class == "timestamp"
    assert metrics["collection_health"].platform == "binary_sensor"
    ssid_key = f"ssid_{ssid_id('Old Wi-Fi')}"
    assert metrics[f"{ssid_key}_band_5_clients"].value == 1
    assert metrics[f"{ssid_key}_band_2_4_clients"].value == 0
    assert metrics[f"{ssid_key}_band_6_clients"].value == 0
    assert metrics[f"{ssid_key}_band_unknown_clients"].value == 0


def test_ap_rename_preserves_ids_and_updates_split_labels(store):
    before = make_snapshot(store)
    after = deepcopy(before)
    after["aps"][0]["name"] = "Renamed access point"
    for ssid in after["ssids"]:
        ssid["by_ap"] = {"Renamed access point": ssid["by_ap"].get("Office", 0)}
    original = build_metrics(before, "home")
    renamed = build_metrics(after, "home")
    assert renamed.keys() == original.keys()
    ap_key = f"ap_{ssid_id('SERIAL01')}_clients"
    split_key = f"ssid_{ssid_id('Old Wi-Fi')}_ap_{ssid_id('SERIAL01')}_clients"
    assert renamed[ap_key].device["identifiers"] == original[ap_key].device["identifiers"]
    assert renamed[ap_key].device["name"] == "Renamed access point"
    assert renamed[split_key].name == "Clients on Renamed access point"
    assert renamed[split_key].value == 1
    assert renamed[ap_key].device["via_device"] == (DOMAIN, "aruba_home")


def test_native_counts_still_count_mac_connections_after_identity_linking(store):
    store.ingest([client(), client(mac="aa:bb:cc:00:00:02")], [ap(clients=2)], inventory())
    identities = store.clients()
    store.link(identities[0]["id"], identities[1]["mac"])
    snapshot = store.snapshot()
    assert len(snapshot["clients"]) == 1
    metrics = build_metrics(snapshot, "home")
    assert metrics["cluster_clients"].value == 2
    assert metrics[f"ssid_{ssid_id('Old Wi-Fi')}_clients"].value == 2


def test_client_metrics_keep_per_mac_identity_and_evidence_through_panel_linking(store, time_at):
    first, second = "aa:bb:cc:00:00:01", "aa:bb:cc:00:00:02"
    store.ingest([client(mac=first), client(mac=second, ssid="New Wi-Fi")], [ap()], inventory(), time_at(0))
    store.ingest([client(mac=second, ssid="New Wi-Fi")], [ap()], inventory(), time_at(1))
    before = build_metrics(store.snapshot(), "home")
    identity = next(row for row in store.clients() if row["mac"] == first)
    store.link(identity["id"], second)
    store.label(identity["id"], "Living room device")
    snapshot = store.snapshot()
    linked = build_metrics(snapshot, "home")
    assert len(snapshot["clients"]) == 1
    assert len(snapshot["client_connections"]) == 2
    assert before.keys() == linked.keys()
    for mac, connected in ((first, False), (second, True)):
        key = f"client_{ssid_id(mac)}"
        assert linked[f"{key}_tracker"].value is connected
        assert linked[f"{key}_tracker"].available
        assert linked[f"{key}_tracker"].device["connections"] == {("mac", mac)}
        assert linked[f"{key}_tracker"].device["identifiers"] == {(DOMAIN, f"aruba_home_{key}")}
        assert linked[f"{key}_tracker"].device["name"] == "Living room device"
        assert linked[f"{key}_ssid"].value == ("New Wi-Fi" if connected else "Old Wi-Fi")
        assert linked[f"{key}_ssid"].available is connected
        assert linked[f"{key}_last_seen"].available
        assert not linked[f"{key}_tracker"].enabled_by_default
        assert all(
            not metric.enabled_by_default
            for name, metric in linked.items()
            if name.startswith(f"{key}_") and name != f"{key}_tracker"
        )
    store.unlink(identity["id"], second)
    assert build_metrics(store.snapshot(), "home").keys() == before.keys()


def test_client_unknown_metrics_units_and_failure_availability(store):
    mac = "aa:bb:cc:00:00:01"
    store.ingest([client(mac=mac, signal_db=None, speed_mbps=None, name=None)], [ap()], inventory())
    snapshot = store.snapshot()
    metrics = build_metrics(snapshot, "home")
    key = f"client_{ssid_id(mac)}"
    assert metrics[f"{key}_tracker"].device["name"] == f"Wi-Fi client {mac}"
    assert "manufacturer" not in metrics[f"{key}_tracker"].device
    for field in ("signal_db", "speed_mbps", "band", "channel"):
        assert metrics[f"{key}_{field}"].value is None
    assert metrics[f"{key}_signal_db"].unit == "dB"
    assert metrics[f"{key}_signal_db"].device_class == "signal_strength"
    assert metrics[f"{key}_speed_mbps"].unit == "Mbit/s"
    assert metrics[f"{key}_speed_mbps"].device_class == "data_rate"
    snapshot["status"].update(fresh=False, last_error="Controller unavailable")
    grace = build_metrics(snapshot, "home")
    assert grace[f"{key}_tracker"].available
    assert grace[f"{key}_tracker"].value is True
    snapshot["status"]["tracker_available"] = False
    assert not build_metrics(snapshot, "home")[f"{key}_tracker"].available
    assert grace[f"{key}_signal_db"].available
    snapshot["status"]["stale"] = True
    stale = build_metrics(snapshot, "home")
    assert not stale[f"{key}_signal_db"].available
    assert stale[f"{key}_last_seen"].available


def test_ap_offline_gates_its_statistics_but_keeps_connectivity_visible(store):
    snapshot = make_snapshot(store)
    snapshot["aps"][0]["online"] = False
    metrics = build_metrics(snapshot, "home")
    ap_key = f"ap_{ssid_id('SERIAL01')}"
    assert metrics[f"{ap_key}_online"].available
    assert not metrics[f"{ap_key}_online"].value
    assert not metrics[f"{ap_key}_clients"].available
    assert not metrics[f"{ap_key}_radio_0_power_dbm"].available
    assert metrics["cluster_clients"].available
    assert metrics[f"ssid_{ssid_id('Old Wi-Fi')}_clients"].available


def test_collection_error_grace_and_stale_deadline_have_distinct_availability(store):
    snapshot = make_snapshot(store)
    snapshot["status"].update(fresh=False, last_error="Controller unavailable")
    grace = build_metrics(snapshot, "home")
    assert grace["collection_health"].available
    assert not grace["collection_health"].value
    assert grace["cluster_clients"].available
    snapshot["status"]["stale"] = True
    stale = build_metrics(snapshot, "home")
    assert stale["collection_health"].available
    assert stale["last_success"].available
    assert all(
        not metric.available
        for key, metric in stale.items()
        if key not in {"collection_health", "last_success"} and not key.endswith(("_last_seen", "_tracker"))
    )
    assert stale["cluster_clients"].value == grace["cluster_clients"].value


def test_entity_properties_use_cached_metrics_and_stable_unique_ids(store):
    coordinator = fake_coordinator(build_metrics(make_snapshot(store), "home"))
    sensor = ArubaSensor(coordinator, "cluster_clients")
    health = ArubaBinarySensor(coordinator, "collection_health")
    assert sensor.unique_id == "aruba_home_cluster_clients"
    assert sensor.available
    assert sensor.native_value == 1
    assert sensor.state_class == SensorStateClass.MEASUREMENT
    assert health.is_on
    assert health.device_class == "connectivity"
    assert health.entity_category == EntityCategory.DIAGNOSTIC
    coordinator.metrics["cluster_clients"] = replace(coordinator.metrics["cluster_clients"], value=7)
    assert sensor.native_value == 7
    coordinator.last_update_success = False
    assert sensor.available
    coordinator._closed = True
    assert not sensor.available
    assert not health.available
    coordinator._closed = False
    del coordinator.metrics["cluster_clients"]
    assert not sensor.available
    assert sensor.native_value == 1


async def test_obsolete_radios_removed_only_after_validated_inventory_for_this_entry(hass, store):
    snapshot = make_snapshot(store)
    second_radio = {**snapshot["aps"][0]["radios"][0], "index": 1}
    snapshot["aps"][0]["radios"].append(second_radio)
    coordinator = fake_coordinator(build_metrics(snapshot, "home"))
    entry = MockConfigEntry(domain=DOMAIN, unique_id="home")
    entry.add_to_hass(hass)
    entry.runtime_data = coordinator
    other_entry = MockConfigEntry(domain=DOMAIN, unique_id="elsewhere")
    other_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    ap_key = f"ap_{ssid_id('SERIAL01')}"
    disabled = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"aruba_home_{ap_key}_radio_2_band",
        config_entry=entry,
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    foreign = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"aruba_elsewhere_{ap_key}_radio_5_band",
        config_entry=other_entry,
    )
    added = []

    def add_entities(entities):
        for entity in entities:
            registered = registry.async_get_or_create("sensor", DOMAIN, entity.unique_id, config_entry=entry)
            entity.entity_id = registered.entity_id
            entity.hass = hass
            entity.async_remove = AsyncMock()
            added.append(entity)

    await async_setup_metrics(hass, entry, add_entities, "sensor", ArubaSensor)
    radio_zero = next(entity for entity in added if entity.key == f"{ap_key}_radio_0_band")
    radio_one = next(entity for entity in added if entity.key == f"{ap_key}_radio_1_band")
    assert registry.async_get(disabled.entity_id) is not None
    coordinator.metrics = {
        key: metric for key, metric in coordinator.metrics.items() if "_radio_1_" not in key
    }
    coordinator.listeners[0]()
    await hass.async_block_till_done()
    assert registry.async_get(radio_one.entity_id) is not None
    radio_one.async_remove.assert_not_awaited()
    coordinator.inventory_revision = 1
    coordinator.listeners[0]()
    await hass.async_block_till_done()
    radio_one.async_remove.assert_awaited_once()
    assert registry.async_get(radio_one.entity_id) is None
    assert registry.async_get(disabled.entity_id) is None
    assert registry.async_get(radio_zero.entity_id) is not None
    assert registry.async_get(foreign.entity_id) is not None
    coordinator.metrics = build_metrics(snapshot, "home")
    coordinator.inventory_revision = 2
    coordinator.listeners[0]()
    assert len([entity for entity in added if entity.unique_id == radio_one.unique_id]) == 2
    assert registry.async_get_entity_id("sensor", DOMAIN, radio_one.unique_id) is not None


@pytest.mark.parametrize("kind", ["radio", "ssid", "client"])
async def test_reappearing_metric_waits_for_old_entity_retirement_and_keeps_registry(hass, store, kind):
    import asyncio

    metrics = build_metrics(make_snapshot(store), "home")
    coordinator = fake_coordinator(metrics)
    entry = MockConfigEntry(domain=DOMAIN, unique_id="home")
    entry.add_to_hass(hass)
    entry.runtime_data = coordinator
    registry = er.async_get(hass)
    added = []

    def add_entities(entities):
        for entity in entities:
            registered = registry.async_get_or_create("sensor", DOMAIN, entity.unique_id, config_entry=entry)
            entity.entity_id = registered.entity_id
            entity.hass = hass
            entity.async_remove = AsyncMock()
            added.append(entity)

    await async_setup_metrics(hass, entry, add_entities, "sensor", ArubaSensor)
    key = {
        "radio": f"ap_{ssid_id('SERIAL01')}_radio_0_band",
        "ssid": f"ssid_{ssid_id('Old Wi-Fi')}_clients",
        "client": f"client_{ssid_id('aa:bb:cc:00:00:01')}_ip",
    }[kind]
    old_radio = next(entity for entity in added if entity.key == key)
    registered = registry.async_update_entity(old_radio.entity_id, name="Preserve this user's name")
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_remove():
        started.set()
        await release.wait()

    old_radio.async_remove = slow_remove
    coordinator.metrics = {name: metric for name, metric in metrics.items() if name != key}
    coordinator.inventory_revision = 1
    coordinator.listeners[0]()
    await started.wait()
    coordinator.metrics = metrics
    coordinator.inventory_revision = 2
    coordinator.listeners[0]()
    assert len([entity for entity in added if entity.key == key]) == 1
    release.set()
    await hass.async_block_till_done()
    assert len([entity for entity in added if entity.key == key]) == 2
    replacement = registry.async_get_entity_id("sensor", DOMAIN, old_radio.unique_id)
    assert replacement == old_radio.entity_id
    assert registry.async_get(replacement).id == registered.id
    assert registry.async_get(replacement).name == "Preserve this user's name"


async def test_retained_ssid_metrics_and_devices_retire_only_after_deletion(hass, store, time_at):
    from homeassistant.helpers import device_registry as dr

    store.ingest([client()], [ap()], inventory(), time_at(0))
    original_metrics = build_metrics(store.snapshot(), "home")
    coordinator = fake_coordinator(original_metrics)
    entry = MockConfigEntry(domain=DOMAIN, unique_id="home")
    entry.add_to_hass(hass)
    entry.runtime_data = coordinator
    other_entry = MockConfigEntry(domain="other", unique_id="other")
    other_entry.add_to_hass(hass)
    entities = er.async_get(hass)
    devices = dr.async_get(hass)
    old_prefix = f"aruba_home_ssid_{ssid_id('Old Wi-Fi')}"
    old_device = devices.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, old_prefix)}, name="Old Wi-Fi"
    )
    # Foreign device records must survive even if they expose the same identifier.
    foreign_device = devices.async_get_or_create(
        config_entry_id=other_entry.entry_id, identifiers={(DOMAIN, old_prefix)}, name="Foreign device"
    )
    # Include a disabled entity from a previous run and a device without entities.
    disabled = entities.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{old_prefix}_disabled",
        config_entry=entry,
        device_id=old_device.id,
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    orphan = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"aruba_home_ssid_{ssid_id('Retired Wi-Fi')}")},
    )
    added = []

    def add_entities(new_entities):
        for entity in new_entities:
            device = devices.async_get_or_create(config_entry_id=entry.entry_id, **entity.metric.device)
            registered = entities.async_get_or_create(
                "sensor", DOMAIN, entity.unique_id, config_entry=entry, device_id=device.id
            )
            entity.entity_id = registered.entity_id
            entity.hass = hass
            entity.async_remove = AsyncMock()
            added.append(entity)

    await async_setup_metrics(hass, entry, add_entities, "sensor", ArubaSensor)
    old_entities = [entity for entity in added if entity.unique_id.startswith(old_prefix)]
    # A failure does not advance the successful inventory revision.
    store.failure("Controller unreachable")
    coordinator.metrics = build_metrics(store.snapshot(), "home")
    coordinator.listeners[0]()
    await hass.async_block_till_done()
    assert devices.async_get(old_device.id)
    assert devices.async_get(orphan.id)
    assert entities.async_get(disabled.entity_id)
    assert all(not entity.async_remove.called for entity in old_entities)
    store.ingest([], [ap()], [{"ssid": "New Wi-Fi"}], time_at(1))
    coordinator.metrics = build_metrics(store.snapshot(), "home")
    coordinator.inventory_revision = 1
    coordinator.listeners[0]()
    await hass.async_block_till_done()
    assert all(not entity.async_remove.called for entity in old_entities)
    assert devices.async_get(old_device.id)
    assert not coordinator.metrics[f"ssid_{ssid_id('Old Wi-Fi')}_clients"].available
    store.delete_ssid("Old Wi-Fi")
    coordinator.metrics = build_metrics(store.snapshot(), "home")
    coordinator.inventory_revision = 2
    coordinator.listeners[0]()
    await hass.async_block_till_done()
    assert all(entity.async_remove.await_count == 1 for entity in old_entities)
    assert not entities.async_get(disabled.entity_id)
    assert not devices.async_get(old_device.id)
    assert not devices.async_get(orphan.id)
    assert devices.async_get(foreign_device.id)
    assert not any(
        row.unique_id.startswith(old_prefix)
        for row in er.async_entries_for_config_entry(entities, entry.entry_id)
    )
    assert any(row.key.startswith("client_") for row in added)
    assert store.clients()[0]["sightings"][0]["ssid"] == "Old Wi-Fi"
    # The stable SSID identifier returns when the controller discovers it again.
    store.ingest([], [ap()], inventory(), time_at(2))
    coordinator.metrics = build_metrics(store.snapshot(), "home")
    coordinator.inventory_revision = 3
    coordinator.listeners[0]()
    await hass.async_block_till_done()
    replacement = devices.async_get_device_by_identifier((DOMAIN, old_prefix), entry.entry_id)
    assert replacement
    assert entities.async_get_entity_id("sensor", DOMAIN, f"{old_prefix}_clients")
