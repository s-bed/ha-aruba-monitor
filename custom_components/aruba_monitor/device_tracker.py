"""Per-MAC native Wi-Fi association presence."""

from homeassistant.components.device_tracker import BaseScannerEntity, SourceType

from .entity import ArubaEntity, async_setup_metrics


class ArubaDeviceTracker(ArubaEntity, BaseScannerEntity):
    """Expose retained association without scanner MAC auto-enable behavior."""

    _attr_source_type = SourceType.ROUTER

    @property
    def is_connected(self):
        return self.metric.value

    @property
    def extra_state_attributes(self):
        return {"restored_presence": self.coordinator.snapshot["status"].get("restored_presence", False)}


async def async_setup_entry(hass, entry, async_add_entities):
    await async_setup_metrics(hass, entry, async_add_entities, "device_tracker", ArubaDeviceTracker)
