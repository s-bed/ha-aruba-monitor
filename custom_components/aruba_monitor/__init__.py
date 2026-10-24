"""Native Aruba Instant AOS-8 monitoring and migration campaigns."""

from __future__ import annotations

import logging
from functools import partial
from pathlib import Path

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import config_validation as cv

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
