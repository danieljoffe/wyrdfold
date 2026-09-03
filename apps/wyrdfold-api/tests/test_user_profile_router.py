"""Router tests for /profile/notifications — focuses on the
capability flags + the enable-when-unconfigured guard."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.dependencies import (
    get_async_user_supabase,
    get_current_user_id,
    verify_supabase_jwt,
)
from app.main import app


class _Resp:
    def __init__(self, data: Any, count: int | None = None) -> None:
        self.data = data
        self.count = count


_TEST_USER_ID = "00000000-0000-0000-0000-000000000001"


@pytest.fixture
def client_factory():
    def _make(supabase: MagicMock) -> TestClient:
        # GET routes now resolve the JWT-bound user client (#79 Phase 2);
        # PATCH/POST still use the service-role client. Point both at the
        # same mock so this router's tests exercise either path.
        app.dependency_overrides[get_async_user_supabase] = lambda: supabase
        app.dependency_overrides[verify_supabase_jwt] = lambda: _TEST_USER_ID
        app.dependency_overrides[get_current_user_id] = lambda: _TEST_USER_ID
        return TestClient(app)

    yield _make
    app.dependency_overrides.clear()


@pytest.fixture
def _reset_channel_settings(monkeypatch: pytest.MonkeyPatch):
    """Force both channels into 'unconfigured' state for the test."""
    monkeypatch.setattr(settings, "next_app_url", "")
    monkeypatch.setattr(settings, "job_alert_secret", "")
    monkeypatch.setattr(settings, "twilio_account_sid", "")
    monkeypatch.setattr(settings, "twilio_auth_token", "")
    monkeypatch.setattr(settings, "twilio_phone_number", "")


def _profile_row() -> dict[str, Any]:
    return {
        "id": "p1",
        "job_notifications_enabled": False,
        "job_score_threshold": 100,
        "sms_notifications_enabled": False,
        "sms_score_threshold": 100,
        "sms_daily_limit": 5,
        "phone_number": None,
        "email": None,
    }


def test_get_returns_capabilities_false_when_unconfigured(client_factory, _reset_channel_settings):
    sb = MagicMock()
    sb.table.return_value.select.return_value.eq.return_value.limit.return_value.execute = (
        AsyncMock(return_value=_Resp([_profile_row()]))
    )
    client = client_factory(sb)
    r = client.get("/profile/notifications")
    assert r.status_code == 200
    body = r.json()
    assert body["email_available"] is False
    assert body["sms_available"] is False


def test_get_returns_capabilities_true_when_configured(
    client_factory, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "next_app_url", "https://example.com")
    monkeypatch.setattr(settings, "job_alert_secret", "secret")
    monkeypatch.setattr(settings, "twilio_account_sid", "AC123")
    monkeypatch.setattr(settings, "twilio_auth_token", "token")
    monkeypatch.setattr(settings, "twilio_phone_number", "+15551234567")

    sb = MagicMock()
    sb.table.return_value.select.return_value.eq.return_value.limit.return_value.execute = (
        AsyncMock(return_value=_Resp([_profile_row()]))
    )
    client = client_factory(sb)
    r = client.get("/profile/notifications")
    assert r.status_code == 200
    body = r.json()
    assert body["email_available"] is True
    assert body["sms_available"] is True


def test_patch_rejects_enabling_email_when_unconfigured(client_factory, _reset_channel_settings):
    sb = MagicMock()
    client = client_factory(sb)
    r = client.patch(
        "/profile/notifications",
        json={"job_notifications_enabled": True},
    )
    assert r.status_code == 400
    assert "Email notifications are unavailable" in r.json()["detail"]


def test_patch_rejects_enabling_sms_when_unconfigured(client_factory, _reset_channel_settings):
    sb = MagicMock()
    client = client_factory(sb)
    r = client.patch(
        "/profile/notifications",
        json={"sms_notifications_enabled": True},
    )
    assert r.status_code == 400
    assert "SMS notifications are unavailable" in r.json()["detail"]


def test_patch_allows_disabling_email_even_when_unconfigured(
    client_factory, _reset_channel_settings
):
    """Operator may have removed the credentials after the user enabled
    the channel — the user must still be able to turn it off."""
    sb = MagicMock()
    sb.table.return_value.select.return_value.eq.return_value.limit.return_value.execute = (
        AsyncMock(return_value=_Resp([_profile_row()]))
    )
    sb.table.return_value.update.return_value.eq.return_value.execute = AsyncMock(
        return_value=_Resp(None)
    )
    # /profile UPDATE no longer reads back the row id — `.eq("user_id", ...)`
    # targets the row directly.
    client = client_factory(sb)
    r = client.patch(
        "/profile/notifications",
        json={"job_notifications_enabled": False},
    )
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# #866 — /profile/jobs-filters (server-side /jobs filter snapshots)
# ---------------------------------------------------------------------------


def _filters_row(prefs: Any) -> dict[str, Any]:
    return {"jobs_filter_prefs": prefs}


def test_jobs_filters_get_returns_stored_map(client_factory):
    stored = {"t-1": {"search": "react"}, "__all__": {"country": "US"}}
    sb = MagicMock()
    sb.table.return_value.select.return_value.eq.return_value.limit.return_value.execute = (
        AsyncMock(return_value=_Resp([_filters_row(stored)]))
    )
    client = client_factory(sb)
    r = client.get("/profile/jobs-filters")
    assert r.status_code == 200
    assert r.json() == {"filters": stored}


def test_jobs_filters_get_null_column_is_empty_map(client_factory):
    # Rows predating the #866 column (or an explicit NULL) must serve {}.
    sb = MagicMock()
    sb.table.return_value.select.return_value.eq.return_value.limit.return_value.execute = (
        AsyncMock(return_value=_Resp([_filters_row(None)]))
    )
    client = client_factory(sb)
    r = client.get("/profile/jobs-filters")
    assert r.status_code == 200
    assert r.json() == {"filters": {}}


def test_jobs_filters_put_replaces_the_map(client_factory):
    sb = MagicMock()
    sb.table.return_value.select.return_value.eq.return_value.limit.return_value.execute = (
        AsyncMock(return_value=_Resp([_filters_row({})]))
    )
    update_chain = sb.table.return_value.update
    update_chain.return_value.eq.return_value.execute = AsyncMock(return_value=_Resp(None))
    client = client_factory(sb)

    payload = {"filters": {"t-1": {"search": "python", "remoteOnly": "true"}}}
    r = client.put("/profile/jobs-filters", json=payload)

    assert r.status_code == 200
    assert r.json() == payload
    update_chain.assert_called_once_with({"jobs_filter_prefs": payload["filters"]})


def test_jobs_filters_put_rejects_too_many_keys(client_factory):
    # The caps keep the column from becoming an unbounded dumping ground —
    # prove the guard actually refuses, not just that valid input passes.
    sb = MagicMock()
    client = client_factory(sb)
    r = client.put(
        "/profile/jobs-filters",
        json={"filters": {f"t-{i}": {"search": "x"} for i in range(65)}},
    )
    assert r.status_code == 422
    assert "too many" in r.text


def test_jobs_filters_put_rejects_oversized_blob(client_factory):
    sb = MagicMock()
    client = client_factory(sb)
    r = client.put(
        "/profile/jobs-filters",
        json={"filters": {"t-1": {"search": "x" * 17_000}}},
    )
    assert r.status_code == 422
    assert "too large" in r.text
