"""Validated controller settings, independent of environment variables."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from ipaddress import ip_address
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry


@dataclass(frozen=True)
class ControllerSettings:
    """Immutable connection settings for one Instant virtual controller."""

    site_id: str = "home"
    aruba_host: str = ""
    aruba_ip: str = ""
    aruba_port: int = 4343
    aruba_user: str = field(default="", repr=False)
    aruba_password: str = field(default="", repr=False)
    aruba_verify_tls: bool = True
    aruba_ca: str = ""
    interval: float = 30
    timeout: float = 15
    auto_delete_clients: bool = False
    client_retention_days: int = 30
    auto_delete_ssids: bool = False
    ssid_retention_days: int = 30

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", self.site_id):
            raise ValueError("Site ID must contain 1–64 letters, digits, hyphens or underscores")
        if not self.aruba_host or any(c in self.aruba_host for c in "/?#@ \t\r\n"):
            raise ValueError("Controller address must be a hostname or IP without a URL scheme or port")
        if ":" in self.aruba_host:
            try:
                ip_address(self.aruba_host)
            except ValueError:
                raise ValueError("Set the controller port separately from its hostname") from None
        if self.aruba_ip:
            try:
                ip_address(self.aruba_ip)
            except ValueError:
                raise ValueError("Instant AP IP address must be an IP address") from None
        if not self.aruba_user or not self.aruba_password:
            raise ValueError("Controller credentials are required")
        if isinstance(self.aruba_port, bool) or not 1 <= self.aruba_port <= 65535:
            raise ValueError("Controller port must be between 1 and 65535")
        if not math.isfinite(self.interval) or self.interval < 5:
            raise ValueError("Polling interval must be at least five seconds")
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("HTTP timeout must be positive")
        if not isinstance(self.aruba_verify_tls, bool):
            raise ValueError("TLS verification must be true or false")
        for enabled in (self.auto_delete_clients, self.auto_delete_ssids):
            if not isinstance(enabled, bool):
                raise ValueError("Automatic cleanup must be true or false")
        for days in (self.client_retention_days, self.ssid_retention_days):
            if isinstance(days, bool) or not isinstance(days, int) or days < 1:
                raise ValueError("Inactivity periods must be positive whole numbers of days")

    @classmethod
    def from_entry(cls, entry: ConfigEntry) -> ControllerSettings:
        """Read stable connection data and adjustable collection/cleanup options."""
        data = dict(entry.data)
        option_keys = (
            "interval",
            "timeout",
            "auto_delete_clients",
            "client_retention_days",
            "auto_delete_ssids",
            "ssid_retention_days",
        )
        data.update({key: entry.options[key] for key in option_keys if key in entry.options})
        return cls(**data)
