"""Asynchronous, read-only Aruba Instant AOS-8 controller transport."""

from __future__ import annotations

import asyncio
import socket
import ssl
from functools import partial
from typing import TYPE_CHECKING

import aiohttp
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import parser
from .bands import resolve_bands
from .config import ControllerSettings

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


class ArubaError(RuntimeError):
    """Safe errors without raw controller responses, session IDs or request URLs."""


class AuthError(ArubaError):
    """The supplied credentials or renewed session were rejected."""


class UnsupportedCommand(ArubaError):
    """The controller explicitly does not support a required command."""


class ArubaClient:
    """Own the Instant session ID while borrowing HA's HTTP connection pool."""

    def __init__(
        self,
        hass: HomeAssistant,
        settings: ControllerSettings,
        *,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self.hass = hass
        self.settings = settings
        self.sid: str | None = None
        self.iap_ip = settings.aruba_ip
        self.http = session
        self._ssl: ssl.SSLContext | bool = settings.aruba_verify_tls
        self._ssl_ready = False
        host = settings.aruba_host
        url_host = f"[{host}]" if ":" in host else host
        self._base_url = f"https://{url_host}:{settings.aruba_port}"

    async def _prepare_http(self) -> None:
        if not self._ssl_ready:
            if self.settings.aruba_verify_tls and self.settings.aruba_ca:
                try:
                    self._ssl = await self.hass.async_add_executor_job(
                        partial(ssl.create_default_context, cafile=self.settings.aruba_ca)
                    )
                except OSError, ssl.SSLError:
                    raise ArubaError("Cannot load the configured controller CA certificate") from None
            self._ssl_ready = True
        if self.http is None:
            self.http = async_get_clientsession(self.hass)

    async def request(self, method: str, path: str, **kwargs) -> dict:
        """Request a bounded JSON envelope, sanitizing every transport failure."""
        await self._prepare_http()
        try:
            async with self.http.request(
                method,
                self._base_url + path,
                ssl=self._ssl,
                timeout=aiohttp.ClientTimeout(total=self.settings.timeout),
                allow_redirects=False,
                **kwargs,
            ) as response:
                if response.status in (401, 403):
                    raise AuthError("Controller rejected authentication")
                if response.status != 200:
                    raise ArubaError(f"Controller returned HTTP {response.status}")
                try:
                    payload = await response.json(content_type=None)
                except ValueError, UnicodeError:
                    raise ArubaError("Controller returned invalid JSON") from None
        except aiohttp.ClientError, OSError, TimeoutError:
            raise ArubaError("Controller connection failed (check address, TLS and reachability)") from None
        if not isinstance(payload, dict):
            raise ArubaError("Controller returned an invalid response envelope")
        if str(payload.get("Status-code")) == "1":
            raise AuthError("Controller session expired")
        return payload

    async def login(self) -> None:
        self.sid = None
        payload = await self.request(
            "POST",
            "/rest/login",
            json={"user": self.settings.aruba_user, "passwd": self.settings.aruba_password},
        )
        if (
            payload.get("Status") != "Success"
            or not isinstance(payload.get("sid"), str)
            or not payload["sid"]
        ):
            raise AuthError("Controller login failed")
        self.sid = payload["sid"]

    async def show(self, command: str) -> str:
        if not self.iap_ip:
            try:
                addresses = await asyncio.get_running_loop().getaddrinfo(
                    self.settings.aruba_host, None, family=socket.AF_INET, type=socket.SOCK_STREAM
                )
                self.iap_ip = addresses[0][4][0]
            except OSError, IndexError:
                raise ArubaError("Cannot resolve controller; configure the Instant AP IP address") from None
        for attempt in range(2):
            if not self.sid:
                await self.login()
            try:
                payload = await self.request(
                    "GET",
                    "/rest/show-cmd",
                    params={"iap_ip_addr": self.iap_ip, "cmd": command, "sid": self.sid},
                )
            except AuthError:
                self.sid = None
                if attempt == 0:
                    continue
                raise
            if payload.get("Status") != "Success" or str(payload.get("Status-code", 0)) != "0":
                if str(payload.get("Status-code")) == "6":
                    raise UnsupportedCommand(f"Unsupported command: {command}")
                raise ArubaError(f"Controller rejected {command}")
            output = payload.get("Command output")
            if not isinstance(output, str):
                raise ArubaError(f"Missing command output for {command}")
            return output
        raise AuthError("Controller authentication retry exhausted")

    async def collect(self) -> tuple[list[dict], list[dict], list[dict]]:
        """Retry one malformed snapshot from scratch before returning any tables."""
        for attempt in range(2):
            snapshots = []
            try:
                for command, parse in (
                    ("show clients", parser.clients),
                    ("show aps", parser.aps),
                    ("show network", parser.networks),
                ):
                    output = await self.show(command)
                    try:
                        snapshots.append(parse(output))
                    except parser.ParseError as exc:
                        raise parser.ParseError(
                            f"{command}: {exc} ({len(output)} response characters)"
                        ) from None
            except parser.ParseError as exc:
                await self.reset_session()
                if attempt:
                    raise parser.ParseError(f"{exc}; still invalid after a fresh-session retry") from None
                continue
            resolve_bands(snapshots[0], snapshots[1])
            return tuple(snapshots)
        raise ArubaError("Controller snapshot retry exhausted")

    async def reset_session(self) -> None:
        previous_sid, self.sid = self.sid, None
        if previous_sid:
            try:
                await self.request("POST", "/rest/logout", json={"sid": previous_sid})
            except ArubaError:
                pass

    async def close(self) -> None:
        """Log out without closing a session owned by Home Assistant."""
        await self.reset_session()
