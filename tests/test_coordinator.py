"""Exercise collection lifecycle against Home Assistant's real coordinator."""

import asyncio
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch

import pytest
from conftest import ap, client, inventory
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.aruba_monitor.aruba import ArubaError, AuthError
from custom_components.aruba_monitor.config import ControllerSettings
from custom_components.aruba_monitor.const import DOMAIN
from custom_components.aruba_monitor.coordinator import ArubaCoordinator
from custom_components.aruba_monitor.sensor import ArubaSensor
from custom_components.aruba_monitor.store import Store


@pytest.fixture
async def make_coordinator(hass, tmp_path):
    coordinators = []

    async def create(*, history=False):
        settings = ControllerSettings(
            aruba_host="controller.example", aruba_user="user", aruba_password="secret"
        )
        entry = MockConfigEntry(domain=DOMAIN, unique_id="home", data={})
        entry.add_to_hass(hass)
        path = str(tmp_path / f"history-{len(coordinators)}.sqlite3")

        def open_store():
            if history:
                initial = Store(path)
                initial.ingest([client()], [ap()], inventory())
                initial.create_campaign("Rotation", "Old Wi-Fi", "New Wi-Fi")
                initial.close()
            return Store(path, require_live_collection=True)

        store = await hass.async_add_executor_job(open_store)
        coordinator = ArubaCoordinator(hass, entry, settings, store)
        coordinator.client.show = AsyncMock(return_value="Aruba Instant Version 8.10.0.16")
        coordinator.client.collect = AsyncMock(return_value=([client()], [ap()], inventory()))
        coordinator.client.close = AsyncMock()
        entry.runtime_data = coordinator
        coordinators.append(coordinator)
        return coordinator

    yield create
    for coordinator in coordinators:
        await coordinator.async_shutdown()


async def test_offline_initialization_exposes_history_and_blocks_live_readiness(make_coordinator):
    coordinator = await make_coordinator(history=True)
    coordinator.client.collect.side_effect = ArubaError("Controller unavailable")
    await coordinator.async_initialize()
    assert coordinator.data is coordinator.snapshot
    assert coordinator.snapshot["clients"][0]["name"] == "Kitchen speaker"
    assert coordinator.snapshot["campaigns"][0]["name"] == "Rotation"
    assert not coordinator.snapshot["status"]["live_collection"]
    assert not coordinator.snapshot["status"]["fresh"]
    assert coordinator.snapshot["clients"][0]["connection"] == "unknown"
    coordinator.client.show.assert_not_awaited()
    coordinator.client.collect.assert_not_awaited()
    assert coordinator.metrics["collection_health"].available
    assert not coordinator.metrics["collection_health"].value
    assert not coordinator.metrics["cluster_clients"].available
    await coordinator.async_refresh()
    assert isinstance(coordinator.last_exception, UpdateFailed)
    assert coordinator.snapshot["clients"][0]["name"] == "Kitchen speaker"
    assert coordinator.snapshot["campaigns"][0]["counts"]["pending"] == 1
    assert coordinator.inventory_revision == 0


async def test_first_success_clears_startup_gate_and_failure_keeps_statistics(make_coordinator):
    coordinator = await make_coordinator(history=True)
    await coordinator.async_initialize()
    await coordinator.async_refresh()
    coordinator.client.show.assert_awaited_once_with("show version")
    assert coordinator.inventory_revision == 1
    assert coordinator.snapshot["status"]["live_collection"]
    assert coordinator.snapshot["status"]["firmware_verified"]
    assert coordinator.snapshot["status"]["fresh"]
    assert coordinator.snapshot["status"]["site_id"] == "home"
    sensor = ArubaSensor(coordinator, "cluster_clients")
    assert sensor.available
    assert sensor.native_value == 1
    last_success = coordinator.snapshot["status"]["last_success"]
    coordinator.client.collect.side_effect = ArubaError("Controller unavailable")
    await coordinator.async_refresh()
    assert not coordinator.last_update_success
    assert coordinator.snapshot["status"]["last_success"] == last_success
    assert not coordinator.snapshot["status"]["fresh"]
    assert not coordinator.snapshot["status"]["stale"]
    assert not coordinator.metrics["collection_health"].value
    assert coordinator.metrics["collection_health"].available
    assert coordinator.snapshot["clients"][0]["connection"] == "unknown"
    assert sensor.available
    assert sensor.native_value == 1
    assert coordinator.inventory_revision == 1
    coordinator.client.show.assert_awaited_once()


async def test_stale_deadline_expires_statistics_even_while_waiting_for_reauth(
    hass, make_coordinator, freezer
):
    coordinator = await make_coordinator()
    await coordinator.async_initialize()
    await coordinator.async_refresh()
    sensor = ArubaSensor(coordinator, "cluster_clients")
    coordinator.client.collect.side_effect = AuthError("Rejected")
    with patch("homeassistant.config_entries.ConfigEntry.async_start_reauth_if_available") as reauth:
        await coordinator.async_refresh()
    reauth.assert_called_once_with(hass)
    assert not coordinator.last_update_success
    assert coordinator._unsub_refresh is None
    assert coordinator._remove_stale_timer is not None
    assert sensor.available
    later = datetime.now(UTC) + timedelta(seconds=91)
    freezer.move_to(later)
    async_fire_time_changed(hass, later)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert coordinator.snapshot["status"]["stale"]
    assert not sensor.available
    assert sensor.native_value == 1
    assert coordinator.metrics["collection_health"].available
    assert not coordinator.metrics["collection_health"].value
    assert coordinator.client.collect.await_count == 2


async def test_collector_listener_keeps_polling_without_enabled_entities(hass, make_coordinator, freezer):
    coordinator = await make_coordinator()
    await coordinator.async_initialize()
    assert len(coordinator._listeners) == 1
    remove_entity_listener = coordinator.async_add_listener(Mock())
    assert len(coordinator._listeners) == 2
    remove_entity_listener()
    assert len(coordinator._listeners) == 1
    later = datetime.now(UTC) + timedelta(seconds=31)
    freezer.move_to(later)
    async_fire_time_changed(hass, later)
    await hass.async_block_till_done(wait_background_tasks=True)
    coordinator.client.collect.assert_awaited_once()
    assert coordinator.snapshot["status"]["live_collection"]
    assert coordinator._unsub_refresh is not None


async def test_backup_waits_for_database_write_then_blocks_writes_and_resumes(hass, make_coordinator):
    coordinator = await make_coordinator(history=True)
    await coordinator.async_initialize()
    device_id = coordinator.snapshot["clients"][0]["id"]
    started, release = asyncio.Event(), threading.Event()
    original_label = coordinator.store.label

    def slow_label(*args):
        hass.loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        original_label(*args)

    with (
        patch.object(coordinator.store, "label", slow_label),
        patch.object(coordinator.store, "checkpoint", wraps=coordinator.store.checkpoint) as checkpoint,
    ):
        mutation = asyncio.create_task(coordinator.async_mutate("label", device_id, "Saved before backup"))
        await asyncio.wait_for(started.wait(), 2)
        backup = asyncio.create_task(coordinator.async_prepare_backup())
        await asyncio.sleep(0)
        assert not backup.done()
        checkpoint.assert_not_called()
        release.set()
        await mutation
        await backup
    checkpoint.assert_called_once()
    assert coordinator.snapshot["status"]["backup_in_progress"]
    assert coordinator.snapshot["clients"][0]["label"] == "Saved before backup"
    with pytest.raises(HomeAssistantError, match="backup"):
        await coordinator.async_mutate("label", device_id, "Do not save")
    await coordinator.async_refresh()
    coordinator.client.collect.assert_not_awaited()
    await coordinator.async_finish_backup()
    await hass.async_block_till_done()
    assert not coordinator.snapshot["status"]["backup_in_progress"]
    coordinator.client.collect.assert_awaited_once()
    assert coordinator.snapshot["status"]["fresh"]


async def test_backup_discards_inflight_network_result_until_resumed(hass, make_coordinator):
    coordinator = await make_coordinator(history=True)
    await coordinator.async_initialize()
    last_success = coordinator.snapshot["status"]["last_success"]
    started, release = asyncio.Event(), asyncio.Event()

    async def collect():
        started.set()
        await release.wait()
        return [client(mac="aa:bb:cc:00:00:99")], [ap()], inventory()

    coordinator.client.collect.side_effect = collect
    refresh = asyncio.create_task(coordinator.async_refresh())
    await asyncio.wait_for(started.wait(), 2)
    await coordinator.async_prepare_backup()
    release.set()
    await refresh
    assert coordinator.snapshot["status"]["last_success"] == last_success
    assert len(coordinator.snapshot["clients"]) == 1
    assert coordinator.inventory_revision == 0
    coordinator.client.collect.side_effect = None
    await coordinator.async_finish_backup()
    await hass.async_block_till_done()
    assert coordinator.inventory_revision == 1


async def test_failed_backup_unpauses_and_sanitizes_error(make_coordinator):
    coordinator = await make_coordinator()
    await coordinator.async_initialize()
    with patch.object(coordinator.store, "checkpoint", side_effect=sqlite3.OperationalError("private path")):
        with pytest.raises(HomeAssistantError, match="Could not prepare") as error:
            await coordinator.async_prepare_backup()
    assert "private path" not in str(error.value)
    assert not coordinator._paused
    await coordinator.async_refresh()
    assert coordinator.snapshot["status"]["fresh"]


async def test_shutdown_drains_collection_cancels_timers_and_closes_once(make_coordinator):
    coordinator = await make_coordinator()
    await coordinator.async_initialize()
    await coordinator.async_refresh()
    started, release = asyncio.Event(), asyncio.Event()

    async def collect():
        started.set()
        await release.wait()
        return [], [], []

    coordinator.client.collect.side_effect = collect
    refreshing = asyncio.create_task(coordinator.async_refresh())
    await asyncio.wait_for(started.wait(), 2)
    shutdown = asyncio.create_task(coordinator.async_shutdown())
    await asyncio.sleep(0)
    assert coordinator._closed
    assert coordinator._remove_stale_timer is None
    assert coordinator._remove_collector_listener is None
    assert not coordinator._listeners
    assert not shutdown.done()
    coordinator.client.close.assert_not_awaited()
    release.set()
    await refreshing
    await shutdown
    await coordinator.async_shutdown()
    coordinator.client.close.assert_awaited_once()
    assert coordinator._unsub_refresh is None
    assert coordinator.inventory_revision == 1
    with pytest.raises(HomeAssistantError, match="unloading"):
        await coordinator.async_snapshot()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        coordinator.store.db.execute("SELECT 1")
