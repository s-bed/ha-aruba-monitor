"""Aruba collector, AP, radio and SSID sensors."""

from homeassistant.components.sensor import SensorEntity, SensorStateClass

from .entity import ArubaEntity, async_setup_metrics


class ArubaSensor(ArubaEntity, SensorEntity):
    @property
    def native_value(self):
        return self.metric.value

    @property
    def native_unit_of_measurement(self):
        return self.metric.unit

    @property
    def device_class(self):
        return self.metric.device_class

    @property
    def state_class(self):
        return SensorStateClass.MEASUREMENT if self.metric.measurement else None


async def async_setup_entry(hass, entry, async_add_entities):
    await async_setup_metrics(hass, entry, async_add_entities, "sensor", ArubaSensor)
