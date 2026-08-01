"""Async per-request user Supabase client + token-bleed gate (#57 slice 3).

The async mirror of ``test_user_supabase_client.py``. The headline test is the
same concurrency gate: fire many simultaneous requests through the per-request
async user client with different JWTs and prove no request ever sends another
request's token. This is what makes the shared-pool-per-request-client design
safe (option B from #79) — the shared ``_async_user_httpx`` carries no auth; the
token lives only on each per-request client.

Also pins the build-time invariant that makes the per-request cost negligible:
``acreate_client`` must make NO ``get_session()`` network call, because the
Authorization header is pre-set in the client options.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import HTTPException

from app import supabase_pool
from app.config import Settings
from app.dependencies import get_async_supabase_for_caller, get_async_user_supabase


@pytest.fixture
def recording_pool_async(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Swap the shared async httpx pool for a MockTransport that records, per
    request, the ``marker`` query value alongside the Authorization header. A
    correctly-isolated client always sends ``Bearer <marker>``.
    """
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        marker = request.url.params.get("marker", "")
        # postgrest encodes `.eq("marker", v)` as `marker=eq.<v>`
        marker = marker.removeprefix("eq.")
        auth = request.headers.get("authorization", "")
        seen.append((marker, auth))
        return httpx.Response(200, json=[])

    monkeypatch.setattr(
        supabase_pool,
        "_async_user_httpx",
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(supabase_pool.settings, "supabase_url", "https://test.supabase.co")
    monkeypatch.setattr(supabase_pool.settings, "supabase_anon_key", "anon-key-test")
    return seen


async def _query(token: str) -> None:
    client = await supabase_pool.get_async_user_client(token)
    await client.table("jobs").select("id").eq("marker", token).execute()


@pytest.mark.asyncio
async def test_async_user_client_sends_its_own_bearer(
    recording_pool_async: list[tuple[str, str]],
) -> None:
    await _query("tok-solo")
    assert recording_pool_async == [("tok-solo", "Bearer tok-solo")]


@pytest.mark.asyncio
async def test_no_get_session_network_call_on_build(
    recording_pool_async: list[tuple[str, str]],
) -> None:
    """Building the client must NOT hit the network. ``acreate_client`` skips
    its ``get_session()`` round-trip because the Authorization header is pre-set
    in the options — so the recorder stays empty until we actually query."""
    client = await supabase_pool.get_async_user_client("tok-x")
    assert recording_pool_async == []  # construction issued no request
    await client.table("jobs").select("id").eq("marker", "tok-x").execute()
    assert recording_pool_async == [("tok-x", "Bearer tok-x")]  # exactly one


@pytest.mark.asyncio
async def test_no_token_bleed_under_async_concurrency(
    recording_pool_async: list[tuple[str, str]],
) -> None:
    tokens = [f"user-{i % 8}-jwt" for i in range(400)]
    await asyncio.gather(*[_query(t) for t in tokens])

    assert len(recording_pool_async) == len(tokens)
    # The gate: every request's Authorization matches its own marker. A bleed
    # (one client sending another's token) shows up as a mismatch.
    mismatches = [(m, a) for m, a in recording_pool_async if a != f"Bearer {m}"]
    assert mismatches == [], f"token bleed detected: {mismatches[:5]}"


# ---- dependency guards ------------------------------------------------


def _req_with_auth(value: str | None) -> SimpleNamespace:
    headers = {"authorization": value} if value is not None else {}
    return SimpleNamespace(headers=headers, url=SimpleNamespace(path="/x"))


@pytest.mark.asyncio
async def test_async_dependency_requires_bearer() -> None:
    s = Settings(supabase_url="https://x.supabase.co", supabase_anon_key="anon")
    with pytest.raises(HTTPException) as exc:
        await get_async_user_supabase(_req_with_auth(None), s)  # type: ignore[arg-type]
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_async_dependency_503_when_anon_key_unset() -> None:
    s = Settings(supabase_url="https://x.supabase.co", supabase_anon_key="")
    with pytest.raises(HTTPException) as exc:
        await get_async_user_supabase(_req_with_auth("Bearer tok"), s)  # type: ignore[arg-type]
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_async_dependency_returns_bound_client(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = MagicMock()

    async def _fake(token: str) -> MagicMock:
        return sentinel

    monkeypatch.setattr("app.supabase_pool.get_async_user_client", _fake)
    s = Settings(supabase_url="https://x.supabase.co", supabase_anon_key="anon")
    result = await get_async_user_supabase(_req_with_auth("Bearer abc"), s)  # type: ignore[arg-type]
    assert result is sentinel


@pytest.mark.asyncio
async def test_async_caller_dependency_401_without_token() -> None:
    s = Settings(supabase_url="https://x.supabase.co", supabase_anon_key="anon")
    with pytest.raises(HTTPException) as exc:
        await get_async_supabase_for_caller(_req_with_auth(None), s)  # type: ignore[arg-type]
    assert exc.value.status_code == 401
