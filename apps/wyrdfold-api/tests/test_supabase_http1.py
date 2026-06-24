"""The shared supabase clients must speak HTTP/1.1, not HTTP/2.

Prod bug: the service-role singleton is shared process-wide and the
poller fans out a burst of concurrent ``asyncio.to_thread`` writes at it.
supabase-py's default postgrest transport enables HTTP/2, and httpcore's
HTTP/2 connection object is not safe for concurrent multi-thread use — the
streams corrupt (``LocalProtocolError: Received pseudo-header in trailer``
/ ``KeyError`` in ``httpcore/_sync/http2.py``) and the pooler drops the
socket (broken pipe / Server disconnected).

These tests pin the fix: every supabase client this module builds uses an
HTTP/1.1 connection pool, which multiplexes concurrency safely.
"""

from __future__ import annotations

import httpx
import pytest
from supabase import Client

import app.supabase_pool as sp
from app.config import settings


def _pool_http2(client: Client) -> bool:
    """Read the http2 flag off a built supabase client's postgrest pool."""
    pool = client.postgrest.session._transport._pool  # type: ignore[attr-defined]
    return bool(pool._http2)  # type: ignore[attr-defined]


def test_build_http1_client_disables_http2() -> None:
    c = sp._build_http1_client()
    try:
        assert c._transport._pool._http2 is False  # type: ignore[attr-defined]
        # And the postgrest-compatible defaults we promised to mirror.
        assert c.follow_redirects is True
    finally:
        c.close()


@pytest.fixture
def _service_role_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings, "supabase_url", "https://test-project.supabase.co"
    )
    monkeypatch.setattr(
        settings, "supabase_service_role_key", "test-service-role-key"
    )
    monkeypatch.setattr(settings, "supabase_anon_key", "test-anon-key")


def test_service_role_singleton_is_http1(_service_role_settings: None) -> None:
    sp.init_supabase()
    try:
        client = sp.get_supabase_pool()
        assert client is not None
        # The actual regression assertion: the shared service-role client's
        # postgrest transport must NOT be HTTP/2.
        assert _pool_http2(client) is False
    finally:
        sp.close_supabase()


def test_per_request_user_client_is_http1(_service_role_settings: None) -> None:
    try:
        client = sp.get_user_client("fake-jwt")
        assert _pool_http2(client) is False
    finally:
        sp.close_supabase()


def test_default_supabase_client_would_be_http2() -> None:
    """Negative control: prove the default (un-pinned) supabase transport
    IS HTTP/2 — i.e. the bug is real and our pin is what changes it. If
    supabase-py ever flips its default to HTTP/1.1, this test fails and we
    can simplify the pin."""
    from postgrest._sync.client import SyncPostgrestClient

    default = SyncPostgrestClient("https://test-project.supabase.co/rest/v1")
    try:
        pool = default.session._transport._pool  # type: ignore[attr-defined]
        assert pool._http2 is True  # type: ignore[attr-defined]
    finally:
        default.session.close()


def test_http1_client_is_a_real_httpx_client() -> None:
    c = sp._build_http1_client()
    try:
        assert isinstance(c, httpx.Client)
    finally:
        c.close()
