"""Controller transport parity and credential containment checks."""

import logging
import ssl
from dataclasses import replace
from unittest.mock import patch

import aiohttp
import pytest
from conftest import FIXTURES

from custom_components.aruba_monitor.aruba import ArubaClient, ArubaError, AuthError, UnsupportedCommand
from custom_components.aruba_monitor.config import ControllerSettings
from custom_components.aruba_monitor.parser import ParseError


def settings(**updates):
    return ControllerSettings(
        **{
            "aruba_host": "192.0.2.10",
            "aruba_ip": "192.0.2.10",
            "aruba_user": "test-user",
            "aruba_password": "very-secret-password",
            **updates,
        }
    )


class Response:
    def __init__(self, payload=None, status=200, error=None):
        self.payload, self.status, self.error = payload, status, error

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self, **kwargs):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class Session:
    def __init__(self, handler):
        self.handler, self.calls = handler, []

    def request(self, method, url, **kwargs):
        request = {"method": method, "url": url, **kwargs}
        self.calls.append(request)
        return self.handler(request)


async def test_login_session_reuse_and_one_reauthentication(hass):
    logins, shows = [], []

    def handler(request):
        if request["url"].endswith("/rest/login"):
            logins.append(request["json"])
            return Response({"Status": "Success", "sid": f"sid-{len(logins)}"})
        shows.append(request["params"])
        if len(shows) == 1:
            return Response({"Status-code": 1, "message": "Invalid session"})
        return Response({"Status": "Success", "Command output": "result"})

    session = Session(handler)
    client = ArubaClient(hass, settings(), session=session)
    assert await client.show("show clients") == "result"
    assert await client.show("show aps") == "result"
    assert len(logins) == 2
    assert shows[0]["sid"] == "sid-1" and shows[1]["sid"] == "sid-2"
    assert logins[0] == {"user": "test-user", "passwd": "very-secret-password"}
    assert all(call["allow_redirects"] is False for call in session.calls)
    assert all(call["timeout"].total == 15 for call in session.calls)


async def test_reauthentication_is_bounded(hass):
    def handler(request):
        return Response(
            {"Status": "Success", "sid": "sid"}
            if request["url"].endswith("/rest/login")
            else {"Status-code": 1}
        )

    session = Session(handler)
    client = ArubaClient(hass, settings(), session=session)
    with pytest.raises(AuthError):
        await client.show("show clients")
    assert len(session.calls) == 4
    assert client.sid is None


async def test_unsupported_command_is_not_an_auth_error(hass):
    session = Session(lambda _: Response({"Status": "Failed", "Status-code": 6}))
    client = ArubaClient(hass, settings(), session=session)
    client.sid = "secret-session"
    with pytest.raises(UnsupportedCommand):
        await client.show("show network")
    assert client.sid == "secret-session"
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    "response",
    [
        Response(status=503),
        Response(ValueError("secret-session")),
        Response([]),
        Response({"Status": "Success"}),
        Response(error=aiohttp.ClientConnectionError("https://controller/?sid=secret-session")),
        Response(error=TimeoutError("very-secret-password")),
    ],
)
async def test_errors_do_not_expose_sid_or_password(hass, response):
    client = ArubaClient(hass, settings(), session=Session(lambda _: response))
    client.sid = "secret-session"
    with pytest.raises(ArubaError) as error:
        await client.show("show clients")
    assert "secret-session" not in str(error.value)
    assert "very-secret-password" not in str(error.value)


async def test_transport_leaves_global_logging_unchanged(hass, caplog):
    caplog.set_level(logging.DEBUG)
    levels = {name: logging.getLogger(name).level for name in ("httpx", "httpcore", "aiohttp.client")}
    client = ArubaClient(
        hass,
        settings(),
        session=Session(lambda _: Response({"Status": "Success", "Command output": "safe"})),
    )
    client.sid = "secret-session"
    await client.show("show clients")
    assert levels == {name: logging.getLogger(name).level for name in levels}
    assert "secret-session" not in caplog.text
    assert "very-secret-password" not in caplog.text


async def test_collection_error_identifies_command_without_controller_output(hass):
    def handler(request):
        if request["url"].endswith("/rest/login"):
            return Response({"Status": "Success", "sid": "fresh-session"})
        if request["url"].endswith("/rest/logout"):
            return Response({"Status": "Success"})
        command = request["params"]["cmd"]
        output = (
            (FIXTURES / "clients.txt").read_text() if command == "show clients" else "private-network-data"
        )
        return Response({"Status": "Success", "Command output": output})

    client = ArubaClient(hass, settings(), session=Session(handler))
    client.sid = "secret-session"
    with pytest.raises(ParseError, match="show aps: Missing or unsupported table header") as error:
        await client.collect()
    assert "private-network-data" not in str(error.value)
    assert "secret-session" not in str(error.value)
    assert "still invalid after a fresh-session retry" in str(error.value)
    assert client.sid is None


async def test_malformed_snapshot_retries_whole_collection_with_fresh_session(hass):
    calls = []
    outputs = {"show clients": "clients.txt", "show aps": "aps.txt", "show network": "networks.txt"}
    fixtures = {command: (FIXTURES / filename).read_text() for command, filename in outputs.items()}

    def handler(request):
        if request["url"].endswith("/rest/login"):
            calls.append("login")
            return Response({"Status": "Success", "sid": "fresh-session"})
        if request["url"].endswith("/rest/logout"):
            calls.append("logout")
            return Response({"Status": "Success"})
        command = request["params"]["cmd"]
        calls.append(command)
        output = (
            "" if command == "show aps" and request["params"]["sid"] == "old-session" else fixtures[command]
        )
        return Response({"Status": "Success", "Command output": output})

    client = ArubaClient(hass, settings(), session=Session(handler))
    client.sid = "old-session"
    clients, aps, networks = await client.collect()
    assert (len(clients), len(aps), len(networks)) == (3, 2, 3)
    assert {client["band"] for client in clients} == {"2.4 GHz", "5 GHz"}
    assert all(radio["band"] for ap in aps for radio in ap["radios"])
    assert calls == [
        "show clients",
        "show aps",
        "logout",
        "login",
        "show clients",
        "show aps",
        "show network",
    ]


async def test_custom_ca_is_loaded_in_executor_and_scoped_to_request(hass):
    session = Session(lambda _: Response({"Status": "Success", "Command output": "safe"}))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client = ArubaClient(hass, settings(aruba_ca="/config/controller-ca.pem"), session=session)
    client.sid = "secret-session"
    with patch(
        "custom_components.aruba_monitor.aruba.ssl.create_default_context", return_value=context
    ) as create:
        assert await client.show("show aps") == "safe"
    create.assert_called_once_with(cafile="/config/controller-ca.pem")
    assert session.calls[0]["ssl"] is context


async def test_bad_ca_failure_is_sanitized(hass):
    client = ArubaClient(hass, settings(aruba_ca="/secret/path"), session=Session(lambda _: None))
    with (
        patch(
            "custom_components.aruba_monitor.aruba.ssl.create_default_context", side_effect=OSError("secret")
        ),
        pytest.raises(ArubaError, match="Cannot load the configured controller CA certificate") as error,
    ):
        await client.show("show aps")
    assert "secret" not in str(error.value)


async def test_disabled_tls_and_logout_use_existing_ha_session(hass):
    session = Session(lambda _: Response({"Status": "Success"}))
    client = ArubaClient(hass, settings(aruba_verify_tls=False), session=session)
    client.sid = "secret-session"
    await client.close()
    assert client.sid is None
    assert session.calls[0]["ssl"] is False
    assert session.calls[0]["json"] == {"sid": "secret-session"}


async def test_dns_resolution_is_async_and_failure_safe(hass):
    session = Session(lambda _: Response({"Status": "Success", "Command output": "safe"}))
    client = ArubaClient(hass, settings(aruba_ip=""), session=session)
    client.sid = "secret-session"
    with patch("asyncio.BaseEventLoop.getaddrinfo", side_effect=OSError("secret-session")):
        with pytest.raises(ArubaError, match="Cannot resolve controller") as error:
            await client.show("show aps")
    assert "secret-session" not in str(error.value)


def test_settings_do_not_show_credentials_in_repr():
    assert "very-secret-password" not in repr(settings())
    with pytest.raises(ValueError):
        replace(settings(), interval=float("nan"))
