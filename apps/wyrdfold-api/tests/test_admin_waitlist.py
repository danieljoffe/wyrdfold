"""Waitlist→invite funnel endpoints (Phase 3 slice 4).

Pins the operator surface's contract with everything mocked:
- api-key gating (the whole /admin router);
- invite: allowlist upsert → auth-admin invite (with redirect) → stamp,
  in that order; direct (non-waitlist) invites don't stamp;
- already-registered → 409 with the allowlist upsert still durable;
- invalid shape → 422 with zero side effects;
- the pending filter queries `invited_at IS NULL`.

The live-stack proof (real auth user created, real rows) is in
tests/integration/test_waitlist_invite.py.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.dependencies import get_supabase, verify_api_key
from app.main import app


@pytest.fixture
def sb() -> Any:
    fake = MagicMock(name="supabase")
    # waitlist select → no rows by default (direct invite).
    fake.table.return_value.select.return_value.eq.return_value.execute.return_value.data = []
    app.dependency_overrides[verify_api_key] = lambda: None
    app.dependency_overrides[get_supabase] = lambda: fake
    yield fake
    app.dependency_overrides.clear()


def test_admin_waitlist_requires_api_key() -> None:
    """Negative: the whole surface is operator-only."""
    app.dependency_overrides.clear()
    client = TestClient(app)
    assert client.get("/admin/waitlist").status_code == 401
    assert client.post("/admin/waitlist/invite", json={"email": "a@b.co"}).status_code == 401


def test_invite_from_waitlist_upserts_invites_and_stamps(
    sb: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "next_app_url", "https://app.example")
    sb.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [
        {"id": "wl-1"}
    ]

    r = TestClient(app).post("/admin/waitlist/invite", json={"email": "  Jane@Example.com "})

    assert r.status_code == 200
    assert r.json() == {
        "email": "jane@example.com",
        "invited": True,
        "from_waitlist": True,
    }
    # Allowlist upsert (idempotent) with the normalized address.
    upsert = sb.table.return_value.upsert.call_args
    assert upsert.args[0] == {"email": "jane@example.com"}
    assert upsert.kwargs["on_conflict"] == "email"
    # Invite went out with the FE callback as redirect.
    invite = sb.auth.admin.invite_user_by_email.call_args
    assert invite.args[0] == "jane@example.com"
    assert invite.args[1] == {"redirect_to": "https://app.example/auth/callback"}
    # The waitlist row got stamped.
    stamped = sb.table.return_value.update.call_args.args[0]
    assert "invited_at" in stamped


def test_direct_invite_skips_waitlist_stamp(sb: MagicMock) -> None:
    r = TestClient(app).post("/admin/waitlist/invite", json={"email": "new@example.com"})

    assert r.status_code == 200
    assert r.json()["from_waitlist"] is False
    sb.auth.admin.invite_user_by_email.assert_called_once()
    sb.table.return_value.update.assert_not_called()


def test_already_registered_is_409_after_allowlist_upsert(
    sb: MagicMock,
) -> None:
    """Negative: re-inviting a registered user fails clearly, but the
    allowlist upsert has already happened (harmless + idempotent)."""
    sb.auth.admin.invite_user_by_email.side_effect = Exception(
        "A user with this email address has already been registered"
    )

    r = TestClient(app).post("/admin/waitlist/invite", json={"email": "old@example.com"})

    assert r.status_code == 409
    sb.table.return_value.upsert.assert_called_once()
    sb.table.return_value.update.assert_not_called()


def test_invalid_email_is_422_with_no_side_effects(sb: MagicMock) -> None:
    r = TestClient(app).post("/admin/waitlist/invite", json={"email": "not-an-email"})

    assert r.status_code == 422
    sb.table.return_value.upsert.assert_not_called()
    sb.auth.admin.invite_user_by_email.assert_not_called()


def test_pending_filter_queries_null_invited_at(sb: MagicMock) -> None:
    chain = sb.table.return_value.select.return_value.order.return_value
    chain.is_.return_value.execute.return_value.data = [
        {"email": "p@example.com", "created_at": "2026-07-01T00:00:00+00:00", "invited_at": None}
    ]
    chain.execute.return_value.data = []

    r = TestClient(app).get("/admin/waitlist", params={"pending": "true"})

    assert r.status_code == 200
    assert r.json()["entries"][0]["email"] == "p@example.com"
    chain.is_.assert_called_once_with("invited_at", "null")
