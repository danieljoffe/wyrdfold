"""Live-stack gate for the waitlist→invite funnel (Phase 3 slice 4).

Drives the real endpoint against local Supabase's real auth: a seeded
waitlist signup is converted — auth user actually created, allowlist row
written, waitlist row stamped — and a second invite for the same address
is a clean 409 (the user now exists).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from supabase import Client

from app.dependencies import get_supabase, verify_api_key
from app.main import app

pytestmark = pytest.mark.integration


@pytest.fixture
def seeded_signup(service_client: Client) -> Iterator[str]:
    email = f"invite-drive-{uuid.uuid4().hex[:8]}@example.com"
    service_client.table("waitlist_signups").insert({"email": email}).execute()
    try:
        yield email
    finally:
        # Delete the auth user the invite created (cascades its rows).
        users = service_client.auth.admin.list_users()
        for u in users:
            if u.email == email:
                service_client.auth.admin.delete_user(u.id)
        service_client.table("wyrdfold_beta_invites").delete().eq("email", email).execute()
        service_client.table("waitlist_signups").delete().eq("email", email).execute()


def _client(service_client: Client) -> TestClient:
    app.dependency_overrides[verify_api_key] = lambda: None
    app.dependency_overrides[get_supabase] = lambda: service_client
    return TestClient(app)


def test_invite_creates_real_user_and_stamps_funnel(
    service_client: Client, seeded_signup: str
) -> None:
    email = seeded_signup
    try:
        r = _client(service_client).post("/admin/waitlist/invite", json={"email": email})

        assert r.status_code == 200
        assert r.json()["from_waitlist"] is True

        # A REAL auth user now exists for the address.
        users = [u for u in service_client.auth.admin.list_users() if u.email == email]
        assert len(users) == 1
        # The hook allowlist has the row.
        invites = (
            service_client.table("wyrdfold_beta_invites")
            .select("email")
            .eq("email", email)
            .execute()
            .data
        )
        assert invites
        # The funnel stamped the signup (drops out of the pending view).
        row = (
            service_client.table("waitlist_signups")
            .select("invited_at")
            .eq("email", email)
            .single()
            .execute()
            .data
        )
        assert row["invited_at"] is not None

        # Re-inviting a PENDING (unaccepted) user is a resend — 200, not an
        # error. GoTrue only refuses once the address belongs to a
        # confirmed account (next test).
        r2 = _client(service_client).post("/admin/waitlist/invite", json={"email": email})
        assert r2.status_code == 200
    finally:
        app.dependency_overrides.clear()


def test_inviting_a_confirmed_user_is_409(service_client: Client) -> None:
    """Negative: an address with a confirmed account can't be re-invited."""
    email = f"confirmed-{uuid.uuid4().hex[:8]}@example.com"
    created = service_client.auth.admin.create_user({"email": email, "email_confirm": True})
    try:
        r = _client(service_client).post("/admin/waitlist/invite", json={"email": email})
        assert r.status_code == 409
    finally:
        app.dependency_overrides.clear()
        service_client.auth.admin.delete_user(created.user.id)
        service_client.table("wyrdfold_beta_invites").delete().eq("email", email).execute()
