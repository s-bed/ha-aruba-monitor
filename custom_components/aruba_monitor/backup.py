"""Quiesce the integration's WAL database during Home Assistant backups."""

from homeassistant.core import HomeAssistant

from .const import DOMAIN


async def async_pre_backup(hass: HomeAssistant) -> None:
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        await entry.runtime_data.async_prepare_backup()


async def async_post_backup(hass: HomeAssistant) -> None:
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        await entry.runtime_data.async_finish_backup()
