"""End-to-end api-key scope tests (audit #29 round 3 / H4).

Proves the dedicated cron/automation key (WYRDFOLD_CRON_KEY):
  - authenticates the strictly-operator routes (e.g. POST /poll), and
  - is REJECTED by the user-data routers (e.g. GET /jobs) — the key
    isolation that makes it narrower than the legacy WYRDFOLD_API_KEY.
The legacy key still authenticates the operator routes, but (#29 R3 H4 /
#192) is now ALSO rejected by the user-data routers: those are JWT-only, so a
leak of any shared key can't reach user data.

Six operator routes sit on those JWT-only routers, so they need BOTH a user
JWT and the operator key. That is kept on purpose (docs/decisions.md,
2026-10-23); the tests at the bottom pin the set and the behaviour.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app import dependencies
from app.config import Settings
from app.dependencies import (
    get_async_service_supabase,
    get_current_user_id,
    get_settings,
    verify_api_key,
    verify_api_key_or_jwt,
    verify_supabase_jwt,
)
from app.main import app

_SETTINGS = Settings(
    wyrdfold_api_key="legacykey",
    wyrdfold_cron_key="cronkey",
    supabase_url="https://test-project.supabase.co",
)


def _client() -> TestClient:
    app.dependency_overrides[get_settings] = lambda: _SETTINGS
    app.dependency_overrides[get_async_service_supabase] = lambda: MagicMock()
    return TestClient(app, raise_server_exceptions=False)


def test_cron_key_authenticates_operator_poll_route() -> None:
    # /poll now schedules the poll in the background and returns 202;
    # patch the background body so the test never runs a real poll. The
    # auth assertion is unchanged: the cron key reaches the operator route.
    with patch("app.routers.poll.run_force_poll_locked"):
        client = _client()
        try:
            res = client.post("/poll", headers={"x-api-key": "cronkey"})
            assert res.status_code == 202
        finally:
            app.dependency_overrides.clear()


def test_legacy_key_still_authenticates_operator_poll_route() -> None:
    with patch("app.routers.poll.run_force_poll_locked"):
        client = _client()
        try:
            res = client.post("/poll", headers={"x-api-key": "legacykey"})
            assert res.status_code == 202
        finally:
            app.dependency_overrides.clear()


def test_cron_key_is_rejected_on_user_data_router() -> None:
    """The cron key must NOT authenticate against /jobs (a user-data router).
    No JWT + only the cron key → 401."""
    client = _client()
    try:
        res = client.get("/jobs", headers={"x-api-key": "cronkey"})
        assert res.status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_legacy_key_rejected_on_user_data_router() -> None:
    """#29 R3 H4 / #192: the legacy broad key is now rejected by /jobs too.
    Both shared keys are barred from user routers — only a JWT gets in — so a
    leaked automation key can't authenticate against user data."""
    client = _client()
    try:
        res = client.get("/jobs", headers={"x-api-key": "legacykey"})
        assert res.status_code == 401
    finally:
        app.dependency_overrides.clear()


# ---- Operator routes that also need a user JWT ------------------------------

# Operator routes on JWT-gated routers: the router demands a signed-in user's
# JWT and the route adds the operator key, so a caller needs both. Kept on
# purpose (docs/decisions.md, 2026-10-23): a leaked key alone can't reach them.
_JWT_AND_KEY_ROUTES = {
    ("POST", "/jobs/rescore/{target_id}"),
    ("POST", "/jobs/backfill-salary"),
    ("POST", "/sources"),
    ("POST", "/sources/seed"),
    ("POST", "/targets/{target_id}/poll-jobs"),
    ("GET", "/targets/{target_id}/funnel"),
}
_JWT_GATES = {verify_api_key_or_jwt, verify_supabase_jwt, get_current_user_id}


def _dependency_calls(dependant: Any) -> set[Any]:
    calls: set[Any] = set()
    for sub in dependant.dependencies:
        calls.add(sub.call)
        calls |= _dependency_calls(sub)
    return calls


def _api_routes(
    container: Any, inherited: frozenset[Any] = frozenset()
) -> Iterator[tuple[APIRoute, frozenset[Any]]]:
    """Every API route, with the dependencies its include adds.

    FastAPI 0.143 nests included routers (the router sits in
    ``original_router``) instead of flattening them into ``app.routes``, so
    this handles both shapes.
    """
    for route in container.routes:
        if isinstance(route, APIRoute):
            yield route, inherited
        elif (child := getattr(route, "original_router", None)) is not None:
            context = getattr(route, "include_context", None)
            extra = frozenset(dep.dependency for dep in getattr(context, "dependencies", []))
            yield from _api_routes(child, inherited | extra)


def test_exactly_these_operator_routes_also_need_a_jwt() -> None:
    """Walks the real app, so a new operator route on a user router, or one
    moved off it, fails here and the lists in config.py and .env.example get
    updated with it."""
    needs_both: set[tuple[str, str]] = set()
    key_only: set[tuple[str, str]] = set()
    walked = 0
    for route, inherited in _api_routes(app.router):
        walked += 1
        calls = _dependency_calls(route.dependant) | inherited
        if verify_api_key not in calls:
            continue
        bucket = needs_both if calls & _JWT_GATES else key_only
        bucket.update((method, route.path) for method in route.methods)

    # A walk that silently finds nothing would pass vacuously; these fail first.
    assert walked > 100
    assert ("POST", "/poll") in key_only
    assert needs_both == _JWT_AND_KEY_ROUTES


_SIGNING_KEY = ec.generate_private_key(ec.SECP256R1())
# Only the gates' verdicts matter: the request body some routes need.
_BODIES = {"/sources": {"action": "add", "board_token": "foo", "company_name": "F"}}


class _FakeJWKSClient:
    """Serves the test public key, as PyJWKClient would from the JWKS."""

    def get_signing_key_from_jwt(self, token: str) -> SimpleNamespace:
        jwt.get_unverified_header(token)
        return SimpleNamespace(key=_SIGNING_KEY.public_key())


def _user_jwt() -> str:
    payload = {
        "sub": "11111111-1111-1111-1111-111111111111",
        "aud": "authenticated",
        "iss": f"{_SETTINGS.supabase_url}/auth/v1",
        "exp": datetime.now(UTC) + timedelta(hours=1),
    }
    return jwt.encode(payload, _SIGNING_KEY, algorithm="ES256", headers={"kid": "test-kid"})


def _past_both_gates() -> None:
    # Stands in for the DB client, which every one of these routes asks for
    # after its auth gates: a 418 means the request got through both.
    raise HTTPException(status_code=418, detail="past both gates")


@pytest.fixture
def gated_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(dependencies, "_get_jwks_client", lambda s: _FakeJWKSClient())
    app.dependency_overrides[get_settings] = lambda: _SETTINGS
    app.dependency_overrides[get_async_service_supabase] = _past_both_gates
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.clear()


def _call(client: TestClient, method: str, route: str, headers: dict[str, str]) -> Any:
    path = route.format(target_id="t-1")
    return client.request(method, path, json=_BODIES.get(route), headers=headers)


@pytest.mark.parametrize(("method", "route"), sorted(_JWT_AND_KEY_ROUTES))
def test_operator_key_alone_is_rejected(gated_client: TestClient, method: str, route: str) -> None:
    res = _call(gated_client, method, route, {"x-api-key": "cronkey"})
    # The router's JWT gate turns it away before the key is even checked.
    assert (res.status_code, res.json()["detail"]) == (401, "Unauthorized")


@pytest.mark.parametrize(("method", "route"), sorted(_JWT_AND_KEY_ROUTES))
def test_user_jwt_alone_is_rejected(gated_client: TestClient, method: str, route: str) -> None:
    res = _call(gated_client, method, route, {"authorization": f"Bearer {_user_jwt()}"})
    assert (res.status_code, res.json()["detail"]) == (401, "Invalid API key")


@pytest.mark.parametrize(("method", "route"), sorted(_JWT_AND_KEY_ROUTES))
def test_user_jwt_plus_operator_key_gets_through(
    gated_client: TestClient, method: str, route: str
) -> None:
    headers = {"authorization": f"Bearer {_user_jwt()}", "x-api-key": "cronkey"}
    res = _call(gated_client, method, route, headers)
    assert res.status_code == 418
