"""Native Aruba Instant AOS-8 monitoring and migration campaigns."""

from __future__ import annotations

import logging
import math
import re
from functools import partial
from pathlib import Path

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from .config import ControllerSettings
from .const import (
    DATABASE_DIRECTORY,
    DATABASE_FILENAME,
    DOMAIN,
    FRONTEND_URL,
    PANEL_ELEMENT,
    PANEL_URL,
    VERSION,
)
from .coordinator import ArubaCoordinator
from .store import Store
from .websocket import async_register_commands

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.DEVICE_TRACKER]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register safe static assets and commands once per HA process."""
    await hass.http.async_register_static_paths(
        [StaticPathConfig(FRONTEND_URL, str(Path(__file__).parent / "frontend"), cache_headers=False)]
    )
    async_register_commands(hass)
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Upgrade cleanup/tracking defaults once without resetting later user choices."""
    if entry.version > 2 or (entry.version == 2 and entry.minor_version > 1):
        return False
    if entry.version == 2:
        return True
    if entry.version != 1:
        return False
    options = dict(entry.options)
    interval = options.get("interval", entry.data.get("interval", 30))
    if (
        isinstance(interval, bool)
        or not isinstance(interval, (int, float))
        or not math.isfinite(interval)
        or interval < 5
    ):
        _LOGGER.error("Cannot upgrade Aruba Monitor: invalid saved polling interval")
        return False
    options.update(
        interval=min(300, max(10, interval)),
        auto_delete_clients=True,
        track_new_devices=False,
    )
    hass.config_entries.async_update_entry(entry, options=options, version=2, minor_version=1)
    return True


def _remove_legacy_client_connectivity(hass: HomeAssistant, entry: ConfigEntry, site: str) -> None:
    """Remove only replaced connectivity entities belonging to this integration entry."""
    registry = er.async_get(hass)
    legacy_id = re.compile(rf"aruba_{re.escape(site)}_client_[0-9a-f]{{24}}_online")
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (
            entity.platform == DOMAIN
            and entity.domain == Platform.BINARY_SENSOR
            and legacy_id.fullmatch(entity.unique_id)
        ):
            registry.async_remove(entity.entity_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    settings = ControllerSettings.from_entry(entry)
    try:
        store = await hass.async_add_executor_job(
            partial(
                Store,
                hass.config.path(DATABASE_DIRECTORY, DATABASE_FILENAME),
                settings.interval,
                require_live_collection=True,
            )
        )
    except Exception:
        _LOGGER.error("Cannot open Aruba Monitor database; verify schema, integrity and permissions")
        raise ConfigEntryError(
            "Cannot open Aruba Monitor database; existing data has been preserved"
        ) from None
    coordinator = ArubaCoordinator(hass, entry, settings, store)
    entry.runtime_data = coordinator
    platforms_loaded = False
    try:
        await coordinator.async_initialize()
        _remove_legacy_client_connectivity(hass, entry, settings.site_id)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        platforms_loaded = True
        await panel_custom.async_register_panel(
            hass,
            frontend_url_path=PANEL_URL,
            webcomponent_name=PANEL_ELEMENT,
            sidebar_title="Aruba Monitor",
            sidebar_icon="mdi:wifi",
            require_admin=True,
            module_url=f"{FRONTEND_URL}/panel.js?v={VERSION}",
        )
    except Exception:
        if platforms_loaded:
            await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        await coordinator.async_shutdown()
        raise ConfigEntryError(
            "Aruba Monitor could not initialize; check database and configuration"
        ) from None
    # Existing entries and history remain accessible even if this refresh fails.
    entry.async_create_background_task(hass, coordinator.async_refresh(), "Initial Aruba collection")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    frontend.async_remove_panel(hass, PANEL_URL)
    await entry.runtime_data.async_shutdown()
    return True
