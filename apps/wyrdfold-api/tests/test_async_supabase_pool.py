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
