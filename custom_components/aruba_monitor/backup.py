"""Quiesce the integration's WAL database during Home Assistant backups."""

from homeassistant.core import HomeAssistant
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN

BACKUP_IN_PROGRESS: HassKey[bool] = HassKey(f"{DOMAIN}_backup_in_progress")


async def async_pre_backup(hass: HomeAssistant) -> None:
    # This lifecycle marker must outlive any entry unloaded during the backup.
    was_in_progress = hass.data.get(BACKUP_IN_PROGRESS, False)
    hass.data[BACKUP_IN_PROGRESS] = True
    try:
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            await entry.runtime_data.async_prepare_backup()
    except BaseException:
        # A rejected/cancelled pre-hook may never receive a matching post-hook.
        if not was_in_progress:
            hass.data.pop(BACKUP_IN_PROGRESS, None)
        raise


async def async_post_backup(hass: HomeAssistant) -> None:
    hass.data.pop(BACKUP_IN_PROGRESS, None)
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        await entry.runtime_data.async_finish_backup()
