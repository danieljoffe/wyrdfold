"""Tests for the SSRF IP-pinning transport (#29 R1 M2 / #192).

The backend resolves a host, refuses the connection if ANY resolved address
is disallowed, and pins the connect to the validated IP — closing the DNS
rebind window between an ``assert_safe_host`` check and the socket connect.
"""

from __future__ import annotations

import ipaddress
from unittest.mock import AsyncMock

import httpcore
import httpx
import pytest

import app.http_client as hc
import app.services.safe_http as sh


def _returns(*ip_strs: str):
    async def _resolve(_host: str, _port: int):
        return [ipaddress.ip_address(s) for s in ip_strs]

    return _resolve


def test_transport_installs_pinning_backend() -> None:
    t = sh.SsrfSafeTransport()
    assert isinstance(t._pool._network_backend, sh._PinningBackend)


@pytest.mark.asyncio
async def test_backend_blocks_internal_without_dialing_or_leaking_ip(monkeypatch):
    base = AsyncMock()
    backend = sh._PinningBackend(base)
    monkeypatch.setattr(sh, "_resolve_ips", _returns("169.254.169.254"))

    with pytest.raises(httpcore.ConnectError) as exc_info:
        await backend.connect_tcp("metadata.evil", 443)

    assert "169.254.169.254" not in str(exc_info.value)  # no IP leak
    base.connect_tcp.assert_not_called()  # never dialed the internal host


@pytest.mark.asyncio
async def test_backend_blocks_when_any_resolved_address_is_internal(monkeypatch):
    """Split-horizon / round-robin DNS mixing a public and an internal record
    must be refused wholesale."""
    base = AsyncMock()
    backend = sh._PinningBackend(base)
    monkeypatch.setattr(sh, "_resolve_ips", _returns("1.1.1.1", "10.0.0.5"))

    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("split.evil", 443)
    base.connect_tcp.assert_not_called()


@pytest.mark.asyncio
async def test_backend_pins_validated_public_ip(monkeypatch):
    base = AsyncMock()
    backend = sh._PinningBackend(base)
    monkeypatch.setattr(sh, "_resolve_ips", _returns("93.184.216.34"))

    await backend.connect_tcp("example.com", 443, timeout=5.0)

    # Dialed the resolved IP literal, not the hostname — so no second
    # resolution can rebind onto an internal address.
    dialed_host = base.connect_tcp.call_args.args[0]
    assert dialed_host == "93.184.216.34"


@pytest.mark.asyncio
async def test_backend_blocks_unresolvable(monkeypatch):
    base = AsyncMock()
    backend = sh._PinningBackend(base)
    monkeypatch.setattr(sh, "_resolve_ips", _returns())  # empty → unresolvable

    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("nope.invalid", 80)
    base.connect_tcp.assert_not_called()


@pytest.mark.asyncio
async def test_backend_delegates_unix_socket_and_sleep(monkeypatch):
    base = AsyncMock()
    backend = sh._PinningBackend(base)
    await backend.connect_unix_socket("x.sock", timeout=1.0)  # mock; never opened
    await backend.sleep(0.0)
    base.connect_unix_socket.assert_awaited_once()
    base.sleep.assert_awaited_once()


@pytest.mark.asyncio
async def test_client_with_safe_transport_blocks_internal_host(monkeypatch):
    """End-to-end through a real httpx client: a request to a host that
    resolves internal is refused at connect (surfaces as httpx.ConnectError)
    — no real socket is opened."""
    monkeypatch.setattr(sh, "_resolve_ips", _returns("127.0.0.1"))
    async with httpx.AsyncClient(transport=sh.SsrfSafeTransport()) as client:
        with pytest.raises(httpx.ConnectError):
            await client.get("http://loopback.evil/x")


@pytest.mark.asyncio
async def test_get_with_size_cap_pins_against_rebind(monkeypatch):
    """The TOCTOU backstop: even if ``validate_host`` passes (a rebind after
    the check), the pinning connect refuses the internal address."""
    # validate_host passes (simulating a name that looked safe at check time),
    # but the connect-time resolution returns an internal IP (the rebind).
    monkeypatch.setattr(sh, "_resolve_ips", _returns("169.254.169.254"))
    try:
        with pytest.raises(httpx.HTTPError):  # ConnectError is an HTTPError
            await hc.get_with_size_cap("http://rebind.test/", validate_host=lambda _h: None)
    finally:
        await hc.close_safe_http_client()
