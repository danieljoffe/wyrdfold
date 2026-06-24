"""End-to-end api-key scope tests (audit #29 round 3 / H4).

Proves the dedicated cron/automation key (WYRDFOLD_CRON_KEY):
  - authenticates the strictly-operator routes (e.g. POST /poll), and
  - is REJECTED by the user-data routers (e.g. GET /jobs) — the key
    isolation that makes it narrower than the legacy WYRDFOLD_API_KEY.
And that the legacy key still works on both (no regression).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.config import Settings
from app.dependencies import get_settings, get_supabase
from app.main import app

_SETTINGS = Settings(
    wyrdfold_api_key="legacykey",
    wyrdfold_cron_key="cronkey",
    supabase_url="https://test-project.supabase.co",
)


def _client() -> TestClient:
    app.dependency_overrides[get_settings] = lambda: _SETTINGS
    app.dependency_overrides[get_supabase] = lambda: MagicMock()
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


def test_legacy_key_still_accepted_on_user_data_router() -> None:
    """No regression: the legacy key still reaches /jobs (today's behavior).
    A 200 or any non-401 proves it passed the auth gate."""
    client = _client()
    try:
        res = client.get("/jobs", headers={"x-api-key": "legacykey"})
        assert res.status_code != 401
    finally:
        app.dependency_overrides.clear()
