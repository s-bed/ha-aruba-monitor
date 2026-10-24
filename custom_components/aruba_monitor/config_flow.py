"""Home Assistant setup, credential renewal and connection configuration."""

from __future__ import annotations

from typing import Any

import probatio
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .aruba import ArubaClient, ArubaError, AuthError, UnsupportedCommand
from .config import ControllerSettings
from .const import DOMAIN
from .parser import ParseError

_OPTION_DEFAULTS = {
    "interval": 30.0,
    "timeout": 15.0,
    "auto_delete_clients": False,
    "client_retention_days": 30,
    "auto_delete_ssids": False,
    "ssid_retention_days": 30,
}


def _connection_schema(defaults: dict[str, Any], *, initial: bool = False) -> probatio.Schema:
    fields: dict[Any, Any] = {}
    if initial:
        fields[probatio.Required("site_id", default=defaults.get("site_id", "home"))] = str
    fields.update(
        {
            probatio.Required("aruba_host", default=defaults.get("aruba_host", "")): str,
            probatio.Required("aruba_port", default=defaults.get("aruba_port", 4343)): probatio.All(
                probatio.Coerce(int), probatio.Range(min=1, max=65535)
            ),
            probatio.Optional("aruba_ip", default=defaults.get("aruba_ip", "")): str,
            probatio.Required("aruba_user", default=defaults.get("aruba_user", "")): str,
            (probatio.Required if initial else probatio.Optional)("aruba_password"): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
            probatio.Required("aruba_verify_tls", default=defaults.get("aruba_verify_tls", True)): bool,
            probatio.Optional("aruba_ca", default=defaults.get("aruba_ca", "")): str,
        }
    )
    return probatio.Schema(fields)


async def async_validate_connection(hass, data: dict[str, Any]) -> None:
    """Check authentication and every required table without persisting observations."""
    settings = ControllerSettings(**data)
    client = ArubaClient(hass, settings)
    try:
        await client.show("show version")
        await client.collect()
    finally:
        await client.close()


async def _connection_errors(hass, data: dict[str, Any]) -> dict[str, str]:
    try:
        await async_validate_connection(hass, data)
    except AuthError:
        return {"base": "invalid_auth"}
    except UnsupportedCommand:
        return {"base": "unsupported_controller"}
    except ParseError:
        return {"base": "invalid_response"}
    except ArubaError:
        return {"base": "cannot_connect"}
    except TypeError, ValueError:
        return {"base": "invalid_config"}
    except Exception:
        # An unexpected exception can contain full request URLs or credentials.
        return {"base": "unknown"}
    return {}


class ArubaMonitorConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Set up exactly one monitor; the site ID remains stable for its lifetime."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> ArubaMonitorOptionsFlow:
        return ArubaMonitorOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        errors = {}
        if user_input is not None:
            errors = await _connection_errors(self.hass, user_input)
            if not errors:
                await self.async_set_unique_id(user_input["site_id"])
                self._abort_if_unique_id_configured()
                # Another setup flow may have completed during controller validation.
                if self._async_current_entries():
                    return self.async_abort(reason="single_instance_allowed")
                return self.async_create_entry(
                    title=f"Aruba {user_input['site_id']}",
                    data=user_input,
                    options=dict(_OPTION_DEFAULTS),
                )
        return self.async_show_form(
            step_id="user", data_schema=_connection_schema(user_input or {}, initial=True), errors=errors
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None):
        entry = self._get_reauth_entry()
        errors = {}
        if user_input is not None:
            data = {**entry.data, **user_input}
            effective = {**data, **entry.options}
            errors = await _connection_errors(self.hass, effective)
            if not errors:
                return self.async_update_reload_and_abort(entry, data_updates=user_input)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=probatio.Schema(
                {
                    probatio.Required("aruba_user", default=entry.data["aruba_user"]): str,
                    probatio.Required("aruba_password"): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None):
        entry = self._get_reconfigure_entry()
        errors = {}
        if user_input is not None:
            updates = dict(user_input)
            if not updates.get("aruba_password"):
                updates.pop("aruba_password", None)
            data = {**entry.data, **updates}
            errors = await _connection_errors(self.hass, {**data, **entry.options})
            if not errors:
                return self.async_update_reload_and_abort(entry, data_updates=updates)
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_connection_schema({**entry.data, **(user_input or {})}),
            errors=errors,
        )


class ArubaMonitorOptionsFlow(config_entries.OptionsFlowWithReload):
    """Adjust polling and cleanup independently from stable identity data."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        errors = {}
        if user_input is not None:
            try:
                ControllerSettings(**{**self.config_entry.data, **user_input})
            except TypeError, ValueError:
                errors = {"base": "invalid_config"}
            else:
                return self.async_create_entry(title="", data=user_input)
        defaults = {**_OPTION_DEFAULTS, **self.config_entry.options, **(user_input or {})}
        return self.async_show_form(
            step_id="init",
            data_schema=probatio.Schema(
                {
                    probatio.Required("interval", default=defaults["interval"]): probatio.All(
                        probatio.Coerce(float), probatio.Range(min=5)
                    ),
                    probatio.Required("timeout", default=defaults["timeout"]): probatio.All(
                        probatio.Coerce(float), probatio.Range(min=0, min_included=False)
                    ),
                    probatio.Required("auto_delete_clients", default=defaults["auto_delete_clients"]): bool,
                    probatio.Required(
                        "client_retention_days", default=defaults["client_retention_days"]
                    ): probatio.All(int, probatio.Range(min=1)),
                    probatio.Required("auto_delete_ssids", default=defaults["auto_delete_ssids"]): bool,
                    probatio.Required(
                        "ssid_retention_days", default=defaults["ssid_retention_days"]
                    ): probatio.All(int, probatio.Range(min=1)),
                }
            ),
            errors=errors,
        )
