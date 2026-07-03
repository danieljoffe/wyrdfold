"""Closed-signup perimeter (deployment-modes Phase 2, slice 2).

`supabase/config.toml` wires the `before-user-created` hook
(`hook_restrict_wyrdfold_beta`): self-service signups are rejected unless the
email is in `wyrdfold_beta_invites`, while ADMIN-API creates bypass the hook —
the property that keeps owner provisioning and every test fixture working.
These pin all three behaviors on the live stack, so the perimeter can't
silently reopen (the pre-2026-07 state: prod closed via dashboard-only config,
every other stack silently open).

NB: requires a stack booted from this branch's config.toml — if this fails
with "perimeter open" on your machine, `supabase stop && supabase start`.
"""

from __future__ import annotations

import uuid

import pytest
from supabase import Client

from tests.integration.conftest import delete_auth_user

pytestmark = pytest.mark.integration


def _email(tag: str) -> str:
    return f"{tag}-{uuid.uuid4().hex[:8]}@wyrdfold.test"


def test_uninvited_otp_signup_is_rejected(anon_client: Client) -> None:
    email = _email("uninvited")
    with pytest.raises(Exception, match=r"[Uu]ser not found"):
        anon_client.auth.sign_in_with_otp({"email": email})


def test_invited_otp_signup_is_admitted(
    anon_client: Client, service_client: Client
) -> None:
    email = _email("invited")
    service_client.table("wyrdfold_beta_invites").insert({"email": email}).execute()
    try:
        # OTP send succeeding == the hook admitted the user (it fires before
        # user creation; rejection surfaces as an auth error here).
        anon_client.auth.sign_in_with_otp({"email": email})
        row = (
            service_client.table("wyrdfold_beta_invites")
            .select("email")
            .eq("email", email)
            .execute()
            .data
        )
        assert row, "sanity: invite row present"
    finally:
        listed = service_client.auth.admin.list_users()
        # supabase-py has returned both a bare list and a paginated object
        # with .users across versions — accept either.
        for u in getattr(listed, "users", listed):
            if u.email == email:
                delete_auth_user(service_client, u.id)
        service_client.table("wyrdfold_beta_invites").delete().eq(
            "email", email
        ).execute()


def test_admin_create_bypasses_the_hook(service_client: Client) -> None:
    """Owner provisioning + every conftest fixture depend on this: the hook
    gates self-service signup, NOT admin-API creation."""
    email = _email("admin-bypass")
    resp = service_client.auth.admin.create_user(
        {"email": email, "email_confirm": True}
    )
    try:
        assert resp.user is not None and resp.user.email == email
    finally:
        # Guarded so an assertion failure isn't masked by AttributeError here.
        if resp.user is not None:
            delete_auth_user(service_client, resp.user.id)
