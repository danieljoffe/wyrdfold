"""Fixtures for RLS integration tests (#79 Phase 2+).

These run against a LIVE local Supabase stack (`supabase start`) so that
Postgres RLS is actually exercised — the whole point is to prove the
JWT-bound user client is scoped by policy, which the mock-based suite can
never show. They self-skip when the local stack isn't reachable, and the
standard suite deselects the `integration` marker by default (see
pyproject.toml `addopts`).

The keys below are the well-known, publicly-documented Supabase local-dev
defaults — not secrets. Override via env (`SUPABASE_TEST_*`) for a CI
Postgres or a non-default local setup.
"""

from __future__ import annotations

import contextlib
import os
import time
import uuid
from collections.abc import Callable, Iterator

import httpx
import jwt
import pytest
from supabase import Client, create_client

from app import supabase_pool

LOCAL_URL = os.environ.get("SUPABASE_TEST_URL", "http://127.0.0.1:54321")
ANON_KEY = os.environ.get(
    "SUPABASE_TEST_ANON_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZS1kZW1vIiwicm9sZSI6ImFub24iLCJleHAiOjE5ODM4MTI5OTZ9."
    "CRXP1A7WOeoJeXxjNni43kdQwgnWNReilDMblYTn_I0",
)
SERVICE_KEY = os.environ.get(
    "SUPABASE_TEST_SERVICE_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZS1kZW1vIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImV4cCI6MTk4MzgxMjk5Nn0."
    "EGIM96RAZx35lJzdJsyH-qQwv8Hdp7fsn3W0YpN81IU",
)
JWT_SECRET = os.environ.get(
    "SUPABASE_TEST_JWT_SECRET",
    "super-secret-jwt-token-with-at-least-32-characters-long",
)


def _mint_user_jwt(user_id: str) -> str:
    """A token PostgREST will verify (HS256 against the local JWT secret),
    carrying the `authenticated` role and `sub` that drive `auth.uid()`."""
    now = int(time.time())
    return jwt.encode(
        {
            "sub": user_id,
            "role": "authenticated",
            "aud": "authenticated",
            "iat": now,
            "exp": now + 3600,
        },
        JWT_SECRET,
        algorithm="HS256",
    )


def _stack_reachable() -> bool:
    try:
        resp = httpx.get(
            f"{LOCAL_URL}/rest/v1/", headers={"apikey": ANON_KEY}, timeout=2.0
        )
    except httpx.HTTPError:
        return False
    return resp.status_code < 500


@pytest.fixture(scope="session")
def _require_stack() -> None:
    if not _stack_reachable():
        pytest.skip(
            "local Supabase stack not reachable at "
            f"{LOCAL_URL} — run `supabase start`"
        )


@pytest.fixture
def service_client(_require_stack: None) -> Client:
    """Service-role client (bypasses RLS) — for seeding and cleanup only."""
    return create_client(LOCAL_URL, SERVICE_KEY)


@pytest.fixture
def anon_client(_require_stack: None) -> Client:
    """Anonymous client (anon key, `anon` role) — the public, unauthenticated
    surface. RLS + grants must deny it everything not explicitly public."""
    return create_client(LOCAL_URL, ANON_KEY)


@pytest.fixture
def user_client_factory(
    _require_stack: None, monkeypatch: pytest.MonkeyPatch
) -> Callable[[str], Client]:
    """Point the app's per-request user-client factory at the local stack,
    then hand back a builder that mints a JWT for `user_id` and returns the
    exact client the API uses in production (`supabase_pool.get_user_client`).
    """
    monkeypatch.setattr(supabase_pool.settings, "supabase_url", LOCAL_URL)
    monkeypatch.setattr(supabase_pool.settings, "supabase_anon_key", ANON_KEY)
    # Force a fresh real httpx pool (other tests monkeypatch this to a mock).
    monkeypatch.setattr(supabase_pool, "_user_httpx", None)

    def _make(user_id: str) -> Client:
        return supabase_pool.get_user_client(_mint_user_jwt(user_id))

    return _make


def create_auth_user(service_client: Client) -> str:
    """Create a real ``auth.users`` row (the FK target for per-user data) and
    return its id. Phase 0 added ``user_id -> auth.users(id) ON DELETE CASCADE``,
    so a synthetic test user must exist in ``auth.users`` or its inserts fail the
    FK. Random email keeps parallel/repeat runs from colliding.
    """
    email = f"rls-{uuid.uuid4().hex[:12]}@test.local"
    return service_client.auth.admin.create_user(
        {"email": email, "email_confirm": True}
    ).user.id


def delete_auth_user(service_client: Client, user_id: str) -> None:
    """Delete the auth user; ON DELETE CASCADE clears all their per-user rows."""
    with contextlib.suppress(Exception):
        service_client.auth.admin.delete_user(user_id)


@pytest.fixture
def two_seeded_users(service_client: Client) -> Iterator[tuple[str, str]]:
    """Seed two real auth users + a ``user_profiles`` / ``llm_costs`` row each,
    yield their ids, then delete the auth users (ON DELETE CASCADE clears their
    rows). Cleanup runs even if the test body raises.
    """
    uid_a = create_auth_user(service_client)
    uid_b = create_auth_user(service_client)
    try:
        service_client.table("user_profiles").insert(
            [
                {"user_id": uid_a, "name": "User A"},
                {"user_id": uid_b, "name": "User B"},
            ]
        ).execute()
        service_client.table("llm_costs").insert(
            [
                {"user_id": uid_a, "model": "test", "purpose": "test", "cost_usd": 1},
                {"user_id": uid_b, "model": "test", "purpose": "test", "cost_usd": 2},
            ]
        ).execute()
        yield uid_a, uid_b
    finally:
        delete_auth_user(service_client, uid_a)
        delete_auth_user(service_client, uid_b)
