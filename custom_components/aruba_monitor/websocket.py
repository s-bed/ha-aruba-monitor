"""Administrator-only commands for the bundled panel."""

from __future__ import annotations

import logging

import probatio as vol
from homeassistant.components import websocket_api
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .parser import MAC_RE
from .store import ssid_id

_LOGGER = logging.getLogger(__name__)


def text(value, *, limit, trim=False, required=False):
    if not isinstance(value, str):
        raise ValueError("Expected text")
    if trim:
        value = value.strip()
    if len(value) > limit or (required and not value):
        raise ValueError(f"Text must contain {'1' if required else '0'}–{limit} characters")
    return value


def ssid(value):
    value = text(value, limit=32, required=True)
    if len(value.encode()) > 32 or any(ord(c) < 32 for c in value):
        raise ValueError("SSID must be at most 32 UTF-8 bytes with no control characters")
    return value


def mac(value):
    if not MAC_RE.fullmatch(value):
        raise ValueError("Use a colon-separated MAC address")
    return value.lower()


def coordinator_for(hass):
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        raise HomeAssistantError("Aruba Monitor is not loaded")
    return entries[0].runtime_data


def client_devices(hass, coordinator, device_id):
    """Resolve retained client MACs against HA's cached, per-entry device records."""
    client = next((item for item in coordinator.snapshot["clients"] if item["id"] == device_id), None)
    if client is None:
        raise ValueError("Unknown client")
    macs = {dr.format_mac(value) for value in client["macs"]}
    registry = dr.async_get(hass)
    devices = {}
    linked_devices = {}
    for value in sorted(macs):
        identifier = (DOMAIN, f"aruba_{coordinator.settings.site_id}_client_{ssid_id(value)}")
        if device := registry.async_get_device_by_identifier(identifier, coordinator.config_entry.entry_id):
            devices[device.id] = device
        for device in registry.async_get_devices(connections={(dr.CONNECTION_NETWORK_MAC, value)}):
            linked_devices[device.id] = device

    def describe(device):
        matching_macs = sorted(
            dr.format_mac(value)
            for kind, value in device.connections
            if kind == dr.CONNECTION_NETWORK_MAC and dr.format_mac(value) in macs
        )
        entry = hass.config_entries.async_get_entry(device.config_entry_id)
        return {
            "id": device.id,
            "name": device.name_by_user or device.name or (matching_macs or sorted(macs))[0],
            "macs": matching_macs,
            "integration": entry.domain if entry else "",
        }

    return {
        "devices": [describe(device) for device in devices.values()],
        "linked_devices": [describe(device) for key, device in linked_devices.items() if key not in devices],
    }


async def dispatch(hass, command, msg):
    """Keep mutation validation independent of the WebSocket envelope."""
    coordinator = coordinator_for(hass)
    if command == "snapshot":
        return await coordinator.async_snapshot()
    if command == "client_devices":
        return client_devices(hass, coordinator, msg["device_id"])
    if command == "campaign_create":
        name = text(msg["name"], limit=100, trim=True, required=True)
        source, target = ssid(msg["source"]), ssid(msg["target"])
        campaign_id = await coordinator.async_mutate("create_campaign", name, source, target)
        return next(c for c in coordinator.snapshot["campaigns"] if c["id"] == campaign_id)
    if command == "campaign_complete":
        await coordinator.async_mutate("complete", msg["campaign_id"])
    elif command == "campaign_delete":
        await coordinator.async_mutate("delete_campaign", msg["campaign_id"])
    elif command == "campaign_exclude":
        note = text(msg.get("note", ""), limit=500, trim=True)
        if msg["excluded"] and not note:
            raise ValueError("An exclusion note is required")
        await coordinator.async_mutate("exclude", msg["campaign_id"], msg["device_id"], msg["excluded"], note)
    elif command == "client_label":
        label = text(msg["label"], limit=100, trim=True)
        await coordinator.async_mutate("label", msg["device_id"], label)
    elif command == "client_link":
        await coordinator.async_mutate("link", msg["device_id"], mac(msg["mac"]))
    elif command == "client_unlink":
        device_id = await coordinator.async_mutate("unlink", msg["device_id"], mac(msg["mac"]))
        return {"device_id": device_id}
    else:
        raise ValueError("Unknown Aruba Monitor command")
    return {"ok": True}


COMMAND_FIELDS = {
    "snapshot": {},
    "client_devices": {"device_id": str},
    "campaign_create": {"name": str, "source": str, "target": str},
    "campaign_complete": {"campaign_id": str},
    "campaign_delete": {"campaign_id": str},
    "campaign_exclude": {"campaign_id": str, "device_id": str, "excluded": bool},
    "client_label": {"device_id": str, "label": str},
    "client_link": {"device_id": str, "mac": str},
    "client_unlink": {"device_id": str, "mac": str},
}


def make_handler(command, fields):
    schema = {vol.Required("type"): f"{DOMAIN}/{command}"}
    schema.update({vol.Required(key): value for key, value in fields.items()})
    if command == "campaign_exclude":
        schema[vol.Optional("note", default="")] = str

    @websocket_api.require_admin
    @websocket_api.websocket_command(schema)
    @websocket_api.async_response
    async def handle(hass, connection, msg):
        try:
            result = await dispatch(hass, command, msg)
        except ValueError as exc:
            # Only domain validation errors contain user-visible detail.
            connection.send_error(msg["id"], "invalid_operation", str(exc))
        except HomeAssistantError as exc:
            connection.send_error(msg["id"], "unavailable", str(exc))
        except Exception:
            _LOGGER.error("Aruba Monitor command failed; no database or controller details logged")
            connection.send_error(
                msg["id"], "operation_failed", "Aruba Monitor could not complete this operation"
            )
        else:
            connection.send_result(msg["id"], result)

    return handle


def async_register_commands(hass):
    for command, fields in COMMAND_FIELDS.items():
        websocket_api.async_register_command(hass, make_handler(command, fields))
