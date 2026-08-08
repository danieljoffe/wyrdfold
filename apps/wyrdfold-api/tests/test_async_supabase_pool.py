"""#57 slice 1: the async service-role client foundation.

Stood up alongside the sync client so the poller's hot DB paths can migrate off
`asyncio.to_thread` onto native coroutines. These verify construction/lifecycle
(no network — `acreate_client` is lazy); the hot-path migration + load test come
in later slices.
"""

from __future__ import annotations

import httpx
import pytest
from supabase import AsyncClient

import app.supabase_pool as pool
from app.config import settings


@pytest.mark.asyncio
async def test_get_async_supabase_is_none_before_init() -> None:
    # The lifespan (which inits it) isn't booted in tests.
    assert pool.get_async_supabase() is None


@pytest.mark.asyncio
async def test_init_creates_async_client_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "supabase_url", "https://test-project.supabase.co")
    monkeypatch.setattr(settings, "supabase_service_role_key", "svc-role-fake")
    try:
        await pool.init_async_supabase()
        client = pool.get_async_supabase()
        assert isinstance(client, AsyncClient)
        # Core ops the migration will use are present.
        assert callable(client.table)
        assert callable(client.rpc)
    finally:
        await pool.close_async_supabase()
    assert pool.get_async_supabase() is None  # close resets the singleton


@pytest.mark.asyncio
async def test_init_is_noop_when_service_role_key_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "supabase_url", "https://test-project.supabase.co")
    monkeypatch.setattr(settings, "supabase_service_role_key", "")
    await pool.init_async_supabase()
    assert pool.get_async_supabase() is None


@pytest.mark.asyncio
async def test_async_transport_constructs_with_http2() -> None:
    """HTTP/2 is the #57 payoff (safe in a single loop). Constructing with
    `http2=True` also proves the `h2` dependency is present — it would raise
    at build time otherwise."""
    client = pool._build_async_http2_client()
    try:
        assert isinstance(client, httpx.AsyncClient)
    finally:
        await client.aclose()


# ---- HTTP/2 GOAWAY at the connection's stream ceiling ----------------------
#
# Regression for the 2026-08-08 prod defect: a long-lived HTTP/2 connection hit
# Supabase's 20,000-stream cap, the peer sent GOAWAY, and httpx surfaced
# ``RemoteProtocolError: <ConnectionTerminated ... last_stream_id:19999>``. The
# in-flight request died; for the cost-log INSERT its caller swallowed the
# exception, so the LLM spend was never recorded. Silent, recurring data loss.
#
# Retrying is safe BY PROTOCOL: GOAWAY's ``last_stream_id`` is the highest
# stream the peer processed, and the failing request was assigned a higher one,
# so the server provably never saw it — which is why replaying a non-idempotent
# INSERT is correct here specifically.


class _FlakyTransport(httpx.AsyncBaseTransport):
    """Raises the given exceptions in order, then succeeds."""

    def __init__(self, *raises: Exception) -> None:
        self._raises = list(raises)
        self.attempts = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.attempts += 1
        if self._raises:
            raise self._raises.pop(0)
        return httpx.Response(201, json={"ok": True}, request=request)


def _goaway() -> httpx.RemoteProtocolError:
    return httpx.RemoteProtocolError(
        "<ConnectionTerminated error_code:0, last_stream_id:19999, additional_data:None>"
    )


@pytest.mark.asyncio
async def test_goaway_is_retried_once_on_a_fresh_connection() -> None:
    inner = _FlakyTransport(_goaway())
    transport = pool._GoawayRetryTransport(inner)
    async with httpx.AsyncClient(transport=transport) as client:
        resp = await client.post("https://db.test/rest/v1/llm_costs", json={"a": 1})
    assert resp.status_code == 201, "the write must land after the GOAWAY retry"
    assert inner.attempts == 2


@pytest.mark.asyncio
async def test_goaway_retry_is_not_infinite() -> None:
    """A peer that keeps terminating is a real fault — surface it."""
    inner = _FlakyTransport(_goaway(), _goaway())
    transport = pool._GoawayRetryTransport(inner)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx.RemoteProtocolError):
            await client.post("https://db.test/rest/v1/llm_costs", json={"a": 1})
    assert inner.attempts == 2


@pytest.mark.asyncio
async def test_non_goaway_protocol_errors_are_not_replayed() -> None:
    """A mid-body protocol error gives no guarantee the server didn't process
    the request, so replaying it could double-write. Only GOAWAY is safe."""
    inner = _FlakyTransport(httpx.RemoteProtocolError("peer closed mid-response body"))
    transport = pool._GoawayRetryTransport(inner)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx.RemoteProtocolError):
            await client.post("https://db.test/rest/v1/llm_costs", json={"a": 1})
    assert inner.attempts == 1


@pytest.mark.asyncio
async def test_async_pool_is_wrapped_in_the_goaway_retry_transport() -> None:
    """The guard is only worth anything if the real pool actually uses it."""
    client = pool._build_async_http2_client()
    try:
        assert isinstance(client._transport, pool._GoawayRetryTransport)
    finally:
        await client.aclose()
