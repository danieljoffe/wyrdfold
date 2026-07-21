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


def test_invited_otp_signup_is_admitted(anon_client: Client, service_client: Client) -> None:
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
        service_client.table("wyrdfold_beta_invites").delete().eq("email", email).execute()


def test_admin_create_bypasses_the_hook(service_client: Client) -> None:
    """Owner provisioning + every conftest fixture depend on this: the hook
    gates self-service signup, NOT admin-API creation."""
    email = _email("admin-bypass")
    resp = service_client.auth.admin.create_user({"email": email, "email_confirm": True})
    try:
        assert resp.user is not None and resp.user.email == email
    finally:
        # Guarded so an assertion failure isn't masked by AttributeError here.
        if resp.user is not None:
            delete_auth_user(service_client, resp.user.id)


def _set_mode(service_client: Client, mode: str) -> None:
    service_client.table("app_settings").upsert(
        {"key": "signup_mode", "value": mode}, on_conflict="key"
    ).execute()


def test_signup_mode_open_admits_uninvited_then_closed_rejects_again(
    anon_client: Client, service_client: Client
) -> None:
    """Phase 3 slice 5: the operator switch actually moves the perimeter.

    Flip 'open' → an UNINVITED self-service signup is admitted by the
    live hook; flip back 'closed' → uninvited is rejected again. The
    finally block restores 'closed' even on failure, so this test can
    never leave a stack open.
    """
    email = _email("switch-open")
    created_id: str | None = None
    try:
        _set_mode(service_client, "open")
        anon_client.auth.sign_in_with_otp({"email": email})
        users = [u for u in service_client.auth.admin.list_users() if u.email == email]
        assert len(users) == 1, "open mode must admit an uninvited signup"
        created_id = users[0].id
    finally:
        _set_mode(service_client, "closed")
        if created_id:
            delete_auth_user(service_client, created_id)

    with pytest.raises(Exception, match=r"[Uu]ser not found"):
        anon_client.auth.sign_in_with_otp({"email": _email("switch-closed")})


def test_unknown_mode_value_fails_safe_to_closed(
    anon_client: Client, service_client: Client
) -> None:
    """Negative: a corrupted setting ('opeen', '', anything ≠ 'open') must
    keep the perimeter CLOSED — only the exact sentinel admits."""
    try:
        _set_mode(service_client, "opeen")
        with pytest.raises(Exception, match=r"[Uu]ser not found"):
            anon_client.auth.sign_in_with_otp({"email": _email("corrupt-mode")})
    finally:
        _set_mode(service_client, "closed")


# A domain seeded into blocked_email_domains (20260721060000).
_BLOCKED_DOMAIN = "mailinator.com"


def test_open_signup_rejects_disposable_email_domain(
    anon_client: Client, service_client: Client
) -> None:
    """Open-signup abuse control: even with the perimeter OPEN, a seeded
    disposable-email domain is rejected by the hook, and no user is created.
    The finally restores 'closed' so a failure can't leave the stack open."""
    email = f"disposable-{uuid.uuid4().hex[:8]}@{_BLOCKED_DOMAIN}"
    try:
        _set_mode(service_client, "open")
        with pytest.raises(Exception, match=r"permanent email"):
            anon_client.auth.sign_in_with_otp({"email": email})
        created = [u for u in service_client.auth.admin.list_users() if u.email == email]
        assert not created, "a blocked disposable domain must not create a user"
    finally:
        _set_mode(service_client, "closed")


def test_disposable_block_is_open_only_invited_admitted_regardless(
    anon_client: Client, service_client: Client
) -> None:
    """The disposable check fires ONLY on the open path: a beta-INVITED address
    on a blocked domain is still admitted while closed (invites are pre-vetted),
    proving the new check didn't leak into the closed-beta invite gate."""
    email = f"invited-blocked-{uuid.uuid4().hex[:8]}@{_BLOCKED_DOMAIN}"
    created_id: str | None = None
    service_client.table("wyrdfold_beta_invites").insert({"email": email}).execute()
    try:
        # mode stays 'closed' (default) — invited admits despite the domain.
        anon_client.auth.sign_in_with_otp({"email": email})
        users = [u for u in service_client.auth.admin.list_users() if u.email == email]
        assert len(users) == 1, "closed+invited must admit regardless of domain"
        created_id = users[0].id
    finally:
        if created_id:
            delete_auth_user(service_client, created_id)
        service_client.table("wyrdfold_beta_invites").delete().eq("email", email).execute()
