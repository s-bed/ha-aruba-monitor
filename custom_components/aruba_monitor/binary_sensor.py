"""Aruba collection health, AP connectivity and Wi-Fi client connectivity."""

from homeassistant.components.binary_sensor import BinarySensorEntity

from .entity import ArubaEntity, async_setup_metrics


class ArubaBinarySensor(ArubaEntity, BinarySensorEntity):
    @property
    def is_on(self):
        return self.metric.value

    @property
    def device_class(self):
        return self.metric.device_class


async def async_setup_entry(hass, entry, async_add_entities):
    await async_setup_metrics(hass, entry, async_add_entities, "binary_sensor", ArubaBinarySensor)
