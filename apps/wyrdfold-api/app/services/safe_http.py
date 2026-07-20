"""SSRF-safe outbound HTTP — pins the validated resolved IP at connect time.

The per-hop ``assert_safe_host`` checks in :mod:`app.services.validate` and
``http_client.get_with_size_cap`` resolve-and-validate a hostname, but httpx
then re-resolves the name before connecting — a DNS rebind in that window
(the check says ``1.2.3.4``, the connect lands on ``169.254.169.254``) would
slip through (#29 R1 M2 / #192).

This module closes that window at the transport layer. :class:`_PinningBackend`
resolves the host itself, rejects the connection if **any** resolved address is
disallowed (private / loopback / link-local / metadata — the same predicate the
format-layer guard uses), and connects to the *validated* IP. Because it fires
on every connection, it also validates each redirect hop automatically — so a
client using this transport can follow redirects safely without a per-caller
per-hop loop (this is what closes ``url_health``'s previously-unguarded HEAD
redirect following).

TLS is unaffected: httpcore runs ``start_tls`` on the returned stream using the
*origin hostname* for SNI and certificate verification, independent of the IP
``connect_tcp`` dialed — so pinning to the IP does not weaken cert checks.

The resolve is done with ``loop.getaddrinfo`` (async) — a blocking
``socket.getaddrinfo`` here would stall the event loop.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket

import httpcore
import httpx
from httpcore._backends.auto import AutoBackend
from httpcore._backends.base import AsyncNetworkBackend, AsyncNetworkStream

from app.services.validate import _is_disallowed_address

logger = logging.getLogger(__name__)

_IpAddr = ipaddress.IPv4Address | ipaddress.IPv6Address


async def _resolve_ips(host: str, port: int) -> list[_IpAddr]:
    """Async-resolve *host* to its de-duplicated A/AAAA addresses.

    Uses the running loop's resolver (never blocking ``socket.getaddrinfo``,
    which would stall the event loop). Returns ``[]`` when the name doesn't
    resolve; the caller treats that as a refusal.
    """
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return []
    out: list[_IpAddr] = []
    seen: set[str] = set()
    for info in infos:
        addr = str(info[4][0])
        if addr in seen:
            continue
        seen.add(addr)
        try:
            out.append(ipaddress.ip_address(addr))
        except ValueError:
            continue
    return out


class _PinningBackend(AsyncNetworkBackend):
    """Network backend that validates + pins the resolved IP before connect.

    Wraps a base backend (the default :class:`AutoBackend`) and delegates
    everything except ``connect_tcp``, where it enforces the SSRF gate.
    """

    def __init__(self, base: AsyncNetworkBackend | None = None) -> None:
        self._base = base if base is not None else AutoBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 — httpcore backend interface
        local_address: str | None = None,
        socket_options: object = None,
    ) -> AsyncNetworkStream:
        ips = await _resolve_ips(host, port)
        if not ips:
            raise httpcore.ConnectError(f"hostname did not resolve: {host}")
        # Reject if ANY resolved address is disallowed — defends against
        # split-horizon / round-robin DNS that mixes a public and an internal
        # record. Generic message: never echo the resolved IP to callers.
        if any(_is_disallowed_address(ip) for ip in ips):
            logger.warning("ssrf_block(connect): %s resolved to a disallowed address", host)
            raise httpcore.ConnectError("host resolves to a disallowed (private/internal) address")
        # Pin: connect to the validated IP literal so no second resolution can
        # rebind onto an internal address. httpcore still SNIs/cert-verifies
        # against the original hostname when it runs start_tls on this stream.
        pinned = str(ips[0])
        return await self._base.connect_tcp(
            pinned,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,  # type: ignore[arg-type]
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109 — httpcore backend interface
        socket_options: object = None,
    ) -> AsyncNetworkStream:
        # No hostname to resolve; delegate unchanged. (Not used by our clients.)
        return await self._base.connect_unix_socket(
            path,
            timeout=timeout,
            socket_options=socket_options,  # type: ignore[arg-type]
        )

    async def sleep(self, seconds: float) -> None:
        await self._base.sleep(seconds)


class SsrfSafeTransport(httpx.AsyncHTTPTransport):
    """``AsyncHTTPTransport`` whose connections pin the validated resolved IP.

    Reuses httpx's own pool/TLS/limits setup and only swaps the pool's network
    backend for :class:`_PinningBackend`. httpcore's ``AsyncConnectionPool``
    reads ``self._network_backend`` lazily when it creates each connection, so
    replacing it after ``super().__init__`` is effective for every connection.
    """

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._pool._network_backend = _PinningBackend(self._pool._network_backend)


def build_ssrf_safe_transport() -> SsrfSafeTransport:
    """Return a fresh SSRF-pinning transport for an ``httpx.AsyncClient``."""
    return SsrfSafeTransport()
