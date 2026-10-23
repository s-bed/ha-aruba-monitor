"""Native metrics with explicit collection and AP availability."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .store import ssid_id


@dataclass(frozen=True)
class Metric:
    key: str
    platform: str
    name: str
    value: Any
    device: DeviceInfo
    available: bool
    unit: str | None = None
    device_class: str | None = None
    measurement: bool = False
    diagnostic: bool = False


def build_metrics(snapshot: dict, site: str) -> dict[str, Metric]:
    """Build native collector, AP, radio and SSID metrics."""
    status = snapshot["status"]
    available = not status["stale"]
    parent_id = f"aruba_{site}"
    cluster = DeviceInfo(
        identifiers={(DOMAIN, parent_id)},
        name=f"Aruba {site}",
        manufacturer="HPE Aruba Networking",
        model="Instant monitor",
        sw_version=status.get("firmware"),
    )
    metrics = {}

    def add(
        key,
        name,
        value,
        device=cluster,
        *,
        platform="sensor",
        online=available,
        unit=None,
        device_class=None,
        measurement=False,
        diagnostic=False,
    ):
        metrics[key] = Metric(
            key,
            platform,
            name,
            value,
            device,
            online,
            unit,
            device_class,
            measurement,
            diagnostic,
        )

    add(
        "collection_health",
        "Collection healthy",
        status["fresh"],
        platform="binary_sensor",
        online=True,
        device_class="connectivity",
        diagnostic=True,
    )
    last_success = status.get("last_success")
    add(
        "last_success",
        "Last successful collection",
        datetime.fromisoformat(last_success) if last_success else None,
        online=True,
        device_class="timestamp",
        diagnostic=True,
    )
    add("cluster_clients", "Connected clients", snapshot["total_clients"], measurement=True)
    for ap in snapshot["aps"]:
        ap_id = ssid_id(ap["serial"])
        device = DeviceInfo(
            identifiers={(DOMAIN, f"{parent_id}_ap_{ap_id}")},
            name=ap["name"],
            manufacturer="HPE Aruba Networking",
            model=ap.get("model") or "Instant AP",
            serial_number=ap["serial"],
            via_device=(DOMAIN, parent_id),
        )
        online = available and ap["online"]
        add(
            f"ap_{ap_id}_online",
            "Connected",
            ap["online"],
            device,
            platform="binary_sensor",
            device_class="connectivity",
        )
        add(f"ap_{ap_id}_clients", "Clients", ap.get("clients"), device, online=online, measurement=True)
        for radio in ap["radios"]:
            for field, label, unit in (
                ("band", "Band", None),
                ("channel", "Channel", None),
                ("power_dbm", "Power", "dBm"),
                ("utilization_pct", "Utilization", "%"),
                ("noise_floor_dbm", "Noise floor", "dBm"),
            ):
                add(
                    f"ap_{ap_id}_radio_{radio['index']}_{field}",
                    f"Radio {radio['index']} {label}",
                    radio.get(field),
                    device,
                    online=online,
                    unit=unit,
                    measurement=bool(unit),
                )
    for ssid in snapshot["ssids"]:
        sid = ssid["id"]
        device = DeviceInfo(
            identifiers={(DOMAIN, f"{parent_id}_ssid_{sid}")},
            name=ssid["ssid"],
            manufacturer="HPE Aruba Networking",
            model="Instant SSID",
            via_device=(DOMAIN, parent_id),
        )
        add(f"ssid_{sid}_clients", "Clients", ssid["clients"], device, measurement=True)
        for ap in snapshot["aps"]:
            ap_id = ssid_id(ap["serial"])
            add(
                f"ssid_{sid}_ap_{ap_id}_clients",
                f"Clients on {ap['name']}",
                ssid["by_ap"].get(ap["name"], 0),
                device,
                measurement=True,
            )
        for band, label in (("2.4 GHz", "2_4"), ("5 GHz", "5"), ("6 GHz", "6"), ("unknown", "unknown")):
            add(
                f"ssid_{sid}_band_{label}_clients",
                f"Clients · {band}",
                ssid["by_band"].get(band, 0),
                device,
                measurement=True,
            )
    return metrics


class ArubaEntity(CoordinatorEntity):
    """Read values only from the coordinator's cache."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, key):
        super().__init__(coordinator)
        self.key = key
        self._attr_unique_id = f"aruba_{coordinator.settings.site_id}_{key}"
        self._initial = coordinator.metrics[key]
        self._attr_entity_category = EntityCategory.DIAGNOSTIC if self._initial.diagnostic else None

    @property
    def metric(self):
        return self.coordinator.metrics.get(self.key, self._initial)

    @property
    def name(self):
        return self.metric.name

    @property
    def device_info(self):
        info = dict(self.metric.device)
        if parent := info.pop("via_device", None):
            registry = dr.async_get(self.hass or self.coordinator.hass)
            if device := registry.async_get_device_by_identifier(
                parent, self.coordinator.config_entry.entry_id
            ):
                info["via_device_id"] = device.id
        return info

    @callback
    def _handle_coordinator_update(self):
        # HA does not refresh registered device names just by reading device_info.
        registry = dr.async_get(self.hass)
        info = self.metric.device
        entry_id = self.coordinator.config_entry.entry_id
        if device := registry.async_get_device_by_identifier(next(iter(info["identifiers"])), entry_id):
            updates = {
                key: info[key]
                for key in ("name", "model", "manufacturer", "sw_version", "serial_number")
                if key in info and getattr(device, key) != info[key]
            }
            if parent := info.get("via_device"):
                if parent_device := registry.async_get_device_by_identifier(parent, entry_id):
                    if device.via_device_id != parent_device.id:
                        updates["via_device_id"] = parent_device.id
            if updates:
                registry.async_update_device(device.id, **updates)
        super()._handle_coordinator_update()

    @property
    def available(self):
        return self.key in self.coordinator.metrics and self.metric.available and not self.coordinator._closed


async def async_setup_metrics(hass, entry, async_add_entities, platform, entity_type):
    """Discover metrics dynamically and retire radios after validated inventories."""
    coordinator = entry.runtime_data
    entities = {}
    retiring = set()
    revision = coordinator.inventory_revision

    async def remove(key, entity):
        try:
            if entity.hass is not None:
                await entity.async_remove()
            # A new inventory may reintroduce the radio while removal awaits HA.
            # Defer its replacement until this finishes and preserve its registry ID.
            if key not in coordinator.metrics:
                registry = er.async_get(hass)
                entity_id = registry.async_get_entity_id(platform, DOMAIN, entity.unique_id)
                if entity_id and registry.async_get(entity_id).config_entry_id == entry.entry_id:
                    registry.async_remove(entity_id)
        finally:
            retiring.discard(key)
            if not coordinator._closed:
                update()

    @callback
    def update():
        nonlocal revision
        desired = {key for key, metric in coordinator.metrics.items() if metric.platform == platform}
        added = []
        for key in sorted(desired - entities.keys() - retiring):
            entity = entity_type(coordinator, key)
            entities[key] = entity
            added.append(entity)
        if added:
            async_add_entities(added)
        if revision != coordinator.inventory_revision:
            # Entry tasks may complete eagerly and call update() before returning.
            revision = coordinator.inventory_revision
            for key in list(entities.keys() - desired):
                if "_radio_" in key:
                    entity = entities.pop(key)
                    retiring.add(key)
                    entry.async_create_task(hass, remove(key, entity), "Remove obsolete Aruba radio")
            # Also retire disabled entities left in the registry from an older run.
            registry = er.async_get(hass)
            valid_ids = {f"aruba_{coordinator.settings.site_id}_{key}" for key in desired}
            for registered in er.async_entries_for_config_entry(registry, entry.entry_id):
                if (
                    registered.domain == platform
                    and registered.platform == DOMAIN
                    and "_radio_" in registered.unique_id
                    and registered.unique_id not in valid_ids
                    and registered.unique_id
                    not in {f"aruba_{coordinator.settings.site_id}_{key}" for key in retiring}
                    and registered.unique_id not in {entity.unique_id for entity in entities.values()}
                ):
                    registry.async_remove(registered.entity_id)

    entry.async_on_unload(coordinator.async_add_listener(update))
    update()
