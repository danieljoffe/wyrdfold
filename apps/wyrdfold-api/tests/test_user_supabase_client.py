"""Per-request user Supabase client + token-bleed gate (#79 Phase 0).

The headline test is the concurrency gate from the #79 design: fire many
simultaneous requests through the per-request user client with different
JWTs and prove no request ever sends another request's token. This is
what makes option B (per-request client over a shared httpx pool) safe
where option A (shared client + per-request rebind) was not.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from app import supabase_pool
from app.config import Settings
from app.dependencies import get_user_supabase


@pytest.fixture
def recording_pool(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Swap the shared httpx pool for a MockTransport that records, per
    request, the `marker` query value alongside the Authorization header.
    A correctly-isolated client always sends `Bearer <marker>`.
    """
    seen: list[tuple[str, str]] = []
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        marker = request.url.params.get("marker", "")
        # postgrest encodes `.eq("marker", v)` as `marker=eq.<v>`
        marker = marker.removeprefix("eq.")
        auth = request.headers.get("authorization", "")
        with lock:
            seen.append((marker, auth))
        return httpx.Response(200, json=[])

    monkeypatch.setattr(
        supabase_pool, "_user_httpx", httpx.Client(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(supabase_pool.settings, "supabase_url", "https://test.supabase.co")
    monkeypatch.setattr(supabase_pool.settings, "supabase_anon_key", "anon-key-test")
    return seen


def _query(token: str) -> None:
    client = supabase_pool.get_user_client(token)
    client.table("jobs").select("id").eq("marker", token).execute()


def test_user_client_sends_its_own_bearer(recording_pool: list[tuple[str, str]]) -> None:
    _query("tok-solo")
    assert recording_pool == [("tok-solo", "Bearer tok-solo")]


def test_no_token_bleed_under_concurrency(
    recording_pool: list[tuple[str, str]],
) -> None:
    tokens = [f"user-{i % 8}-jwt" for i in range(400)]
    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(_query, tokens))

    assert len(recording_pool) == len(tokens)
    # The gate: every request's Authorization matches its own marker.
    # A bleed (one client sending another's token) shows up as a mismatch.
    mismatches = [(m, a) for m, a in recording_pool if a != f"Bearer {m}"]
    assert mismatches == [], f"token bleed detected: {mismatches[:5]}"


# ---- dependency guards ------------------------------------------------


def _req_with_auth(value: str | None) -> SimpleNamespace:
    headers = {"authorization": value} if value is not None else {}
    return SimpleNamespace(headers=headers)


def test_dependency_requires_bearer() -> None:
    s = Settings(supabase_url="https://x.supabase.co", supabase_anon_key="anon")
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        get_user_supabase(_req_with_auth(None), s)  # type: ignore[arg-type]
    assert exc.value.status_code == 401


def test_dependency_503_when_anon_key_unset() -> None:
    s = Settings(supabase_url="https://x.supabase.co", supabase_anon_key="")
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        get_user_supabase(_req_with_auth("Bearer tok"), s)  # type: ignore[arg-type]
    assert exc.value.status_code == 503


def test_dependency_returns_bound_client(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = MagicMock()
    monkeypatch.setattr(
        "app.supabase_pool.get_user_client", lambda token: sentinel
    )
    s = Settings(supabase_url="https://x.supabase.co", supabase_anon_key="anon")
    result = get_user_supabase(_req_with_auth("Bearer abc"), s)  # type: ignore[arg-type]
    assert result is sentinel
