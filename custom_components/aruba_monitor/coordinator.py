"""One collector and a cached, transactionally consistent view for HA."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .aruba import ArubaClient, ArubaError, AuthError
from .config import ControllerSettings
from .const import DOMAIN
from .entity import build_metrics
from .parser import ParseError
from .store import Store, ssid_id

_LOGGER = logging.getLogger(__name__)


class ArubaCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Collect continuously, including with no enabled entities or open panels.

    Entity availability deliberately uses snapshot status instead of the default
    CoordinatorEntity policy: a transient failed poll must retain statistics.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, settings: ControllerSettings, store: Store):
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(seconds=settings.interval),
            always_update=True,
        )
        self.settings = settings
        self.store = store
        self.client = ArubaClient(hass, settings)
        self.snapshot: dict[str, Any] = {}
        self.metrics = {}
        self.inventory_revision = 0
        self._firmware_read = False
        self._collection_lock = asyncio.Lock()
        self._database_lock = asyncio.Lock()
        self._paused = False
        self._closed = False
        self._consecutive_failures = 0
        self._auth_failed = False
        self._remove_stale_timer = None
        self._remove_collector_listener = None

    async def async_initialize(self):
        """Publish history first; network outages must not prevent setup."""
        await self.async_snapshot()
        self.data = self.snapshot
        self._remove_collector_listener = self.async_add_listener(self._keep_collecting)

    @callback
    def _keep_collecting(self):
        """Keep one listener for the lifetime of the entry."""

    async def async_snapshot(self):
        """Read SQLite in the executor, never in an entity property."""
        async with self._database_lock:
            if self._closed:
                raise HomeAssistantError("Aruba Monitor is unloading")
            snapshot = await self.hass.async_add_executor_job(self.store.snapshot)
            # Unload can begin while this executor job finishes.
            if self._closed:
                return self.snapshot
        snapshot["status"]["site_id"] = self.settings.site_id
        snapshot["status"]["backup_in_progress"] = self._paused
        snapshot["status"]["track_new_devices"] = self.settings.track_new_devices
        snapshot["status"]["consecutive_failures"] = self._consecutive_failures
        snapshot["status"]["restored_presence"] = not snapshot["status"]["live_collection"]
        snapshot["status"]["tracker_available"] = (
            bool(snapshot["status"]["last_success"])
            and self._consecutive_failures < 3
            and not self._auth_failed
            and not self._closed
        )
        self.snapshot = snapshot
        self.metrics = build_metrics(snapshot, self.settings.site_id)
        self._schedule_stale_update()
        return snapshot

    @callback
    def _schedule_stale_update(self):
        if self._remove_stale_timer:
            self._remove_stale_timer()
            self._remove_stale_timer = None
        status = self.snapshot["status"]
        if self._closed or status["stale"] or status["age_seconds"] is None:
            return
        remaining = max(0.01, 3 * self.settings.interval - status["age_seconds"])

        @callback
        def expired(_now):
            self._remove_stale_timer = None
            self.config_entry.async_create_background_task(
                self.hass, self.async_refresh_status(), "Aruba stale availability"
            )

        self._remove_stale_timer = async_call_later(self.hass, remaining, expired)

    async def async_refresh_status(self):
        """Notify entities at the stale deadline without another controller poll."""
        if not self._closed:
            await self.async_snapshot()
            self.async_update_listeners()

    async def _failure(self, message, *, authentication=False):
        """Record one failed complete attempt; internal retries do not count."""
        async with self._database_lock:
            if self._closed or self._paused:
                return False
            self._consecutive_failures += 1
            self._auth_failed = self._auth_failed or authentication
            await self.hass.async_add_executor_job(self.store.failure, message)
        if not self._closed:
            await self.async_refresh_status()
        return True

    @callback
    def _enabled_tracker_macs(self) -> set[str]:
        """Read registry choices on the event loop before executor cleanup."""
        registry = er.async_get(self.hass)
        enabled_ids = {
            entity.unique_id
            for entity in er.async_entries_for_config_entry(registry, self.config_entry.entry_id)
            if entity.domain == "device_tracker" and entity.platform == DOMAIN and entity.disabled_by is None
        }
        return {
            client["mac"]
            for client in self.snapshot.get("client_connections", [])
            if f"aruba_{self.settings.site_id}_client_{ssid_id(dr.format_mac(client['mac']))}_tracker"
            in enabled_ids
        }

    def _retained_update(self):
        """A skipped attempt must not turn an established outage into recovery."""
        if not self.last_update_success:
            raise UpdateFailed(self.snapshot["status"]["last_error"] or "Controller unavailable")
        return self.snapshot

    async def _async_update_data(self):
        if self._paused or self._closed:
            return self._retained_update()
        try:
            async with self._collection_lock:
                if self._closed:
                    return self._retained_update()
                firmware = None
                if not self._firmware_read:
                    version = await self.client.show("show version")
                    match = re.search(r"\b8\.\d+\.\d+\.\d+\b", version)
                    firmware = match[0] if match else "unrecognized"
                clients, aps, networks = await self.client.collect()
                async with self._database_lock:
                    if self._closed or self._paused:
                        return self._retained_update()
                    if firmware:
                        await self.hass.async_add_executor_job(self.store.set_meta, "firmware", firmware)
                        self._firmware_read = True
                    await self.hass.async_add_executor_job(self.store.ingest, clients, aps, networks)
                    if self.settings.auto_delete_clients or self.settings.auto_delete_ssids:
                        await self.hass.async_add_executor_job(
                            self.store.cleanup,
                            self.settings.client_retention_days
                            if self.settings.auto_delete_clients
                            else None,
                            self.settings.ssid_retention_days if self.settings.auto_delete_ssids else None,
                            None,
                            self._enabled_tracker_macs(),
                        )
                    self._consecutive_failures = 0
                    self._auth_failed = False
                    self.inventory_revision += 1
            if self._closed:
                return self._retained_update()
            return await self.async_snapshot()
        except AuthError:
            message = "Controller authentication failed; reauthenticate Aruba Monitor"
            if not await self._failure(message, authentication=True):
                return self._retained_update()
            raise ConfigEntryAuthFailed(message) from None
        except (ArubaError, ParseError) as exc:
            if not await self._failure(str(exc)) or (
                self._consecutive_failures < 3 and not self._auth_failed
            ):
                return self._retained_update()
            raise UpdateFailed(str(exc)) from None
        except asyncio.CancelledError, HomeAssistantError:
            raise
        except Exception:
            # Exceptions can contain sid URLs or controller data; never log them.
            message = "Unexpected collection failure; check controller and database permissions"
            if not await self._failure(message) or (self._consecutive_failures < 3 and not self._auth_failed):
                return self._retained_update()
            raise UpdateFailed(message) from None

    async def async_mutate(self, operation: str, *args):
        """Serialize writes and publish recalculated results without polling."""
        async with self._database_lock:
            if self._paused or self._closed:
                raise HomeAssistantError("Aruba Monitor is temporarily unavailable during backup or unload")
            result = await self.hass.async_add_executor_job(getattr(self.store, operation), *args)
            if operation in {"delete_client", "delete_ssid"}:
                self.inventory_revision += 1
        await self.async_refresh_status()
        return result

    async def async_prepare_backup(self):
        """Drain writes and make the main SQLite file independently consistent."""
        async with self._database_lock:
            self._paused = True
            try:
                await self.hass.async_add_executor_job(self.store.checkpoint)
            except Exception:
                self._paused = False
                raise HomeAssistantError("Could not prepare the Aruba Monitor database for backup") from None
        await self.async_refresh_status()

    async def async_finish_backup(self):
        self._paused = False
        if not self._closed:
            await self.async_request_refresh()

    async def async_shutdown(self):
        """Close once after in-flight collection and database operations finish."""
        if self._closed:
            return
        self._closed = True
        if self._remove_stale_timer:
            self._remove_stale_timer()
            self._remove_stale_timer = None
        if self._remove_collector_listener:
            self._remove_collector_listener()
            self._remove_collector_listener = None
        await super().async_shutdown()
        try:
            async with self._collection_lock:
                await self.client.close()
        finally:
            async with self._database_lock:
                await self.hass.async_add_executor_job(self.store.close)
