"""RLS + the register RPC for from-url source registration (feature B).

Runs against a LIVE local Supabase stack so the cap, the ownership ledger, and
the RLS policy are exercised for real (the mock suite can't show Postgres RLS).
Self-skips when the stack isn't reachable; the default suite deselects the
``integration`` marker.
"""

from __future__ import annotations

import contextlib
import uuid

import pytest
from supabase import Client

from tests.integration.conftest import create_auth_user, delete_auth_user

pytestmark = pytest.mark.integration


def _register(
    client: Client,
    *,
    user_id: str,
    board_token: str,
    cap: int = 25,
    provider: str = "ashby",
    company: str = "Test Co",
) -> object:
    resp = client.rpc(
        "register_source_from_url",
        {
            "p_user_id": user_id,
            "p_provider": provider,
            "p_board_token": board_token,
            "p_company_name": company,
            "p_cap": cap,
        },
    ).execute()
    return resp.data


def _cleanup(service_client: Client, tokens: list[str]) -> None:
    # ON DELETE CASCADE from sources → source_registrations clears the registration too.
    with contextlib.suppress(Exception):
        service_client.table("sources").delete().in_("board_token", tokens).execute()


def _token() -> str:
    return f"test-board-{uuid.uuid4().hex[:12]}"


def test_register_creates_enabled_source_and_ownership(service_client: Client) -> None:
    uid = create_auth_user(service_client)
    tok = _token()
    try:
        assert _register(service_client, user_id=uid, board_token=tok) == "registered"

        src = (
            service_client.table("sources")
            .select("enabled,provider,company_name")
            .eq("board_token", tok)
            .single()
            .execute()
            .data
        )
        assert src["enabled"] is True  # auto-enabled → the poller picks it up
        assert src["provider"] == "ashby"

        own = (
            service_client.table("source_registrations")
            .select("user_id")
            .eq("user_id", uid)
            .execute()
            .data
        )
        assert len(own) == 1
    finally:
        _cleanup(service_client, [tok])
        delete_auth_user(service_client, uid)


def test_reregister_same_user_is_idempotent(service_client: Client) -> None:
    uid = create_auth_user(service_client)
    tok = _token()
    try:
        assert _register(service_client, user_id=uid, board_token=tok) == "registered"
        assert _register(service_client, user_id=uid, board_token=tok) == "already_owned"
        own = (
            service_client.table("source_registrations")
            .select("user_id")
            .eq("user_id", uid)
            .execute()
            .data
        )
        assert len(own) == 1  # no duplicate ownership row
    finally:
        _cleanup(service_client, [tok])
        delete_auth_user(service_client, uid)


def test_second_user_links_to_shared_source(service_client: Client) -> None:
    uid_a = create_auth_user(service_client)
    uid_b = create_auth_user(service_client)
    tok = _token()
    try:
        assert _register(service_client, user_id=uid_a, board_token=tok) == "registered"
        assert _register(service_client, user_id=uid_b, board_token=tok) == "linked"
        srcs = (
            service_client.table("sources").select("id").eq("board_token", tok).execute().data
        )
        assert len(srcs) == 1  # one shared source, two ownerships
    finally:
        _cleanup(service_client, [tok])
        delete_auth_user(service_client, uid_a)
        delete_auth_user(service_client, uid_b)


def test_cap_reached_is_a_full_noop(service_client: Client) -> None:
    """At the cap, a NEW board is refused AND no source row is created for it."""
    uid = create_auth_user(service_client)
    t1, t2 = _token(), _token()
    try:
        assert _register(service_client, user_id=uid, board_token=t1, cap=1) == "registered"
        assert _register(service_client, user_id=uid, board_token=t2, cap=1) == "cap_reached"
        # cap_reached created no source for the refused board.
        t2_rows = service_client.table("sources").select("id").eq("board_token", t2).execute().data
        assert t2_rows == []
    finally:
        _cleanup(service_client, [t1, t2])
        delete_auth_user(service_client, uid)


def test_rls_user_sees_only_own_ownerships(
    service_client: Client, user_client_factory: object
) -> None:
    uid_a = create_auth_user(service_client)
    uid_b = create_auth_user(service_client)
    tok = _token()
    try:
        _register(service_client, user_id=uid_a, board_token=tok)
        client_b = user_client_factory(uid_b)  # type: ignore[operator]
        visible = client_b.table("source_registrations").select("user_id").execute().data
        # The RLS SELECT policy scopes to auth.uid() — B never sees A's row.
        assert not any(r["user_id"] == uid_a for r in visible)
    finally:
        _cleanup(service_client, [tok])
        delete_auth_user(service_client, uid_a)
        delete_auth_user(service_client, uid_b)


def test_rls_denies_direct_ownership_insert(
    service_client: Client, user_client_factory: object
) -> None:
    """An authenticated user cannot INSERT ownership directly — that would mint
    unlimited rows and bypass the RPC's cap. Only the definer RPC writes."""
    uid = create_auth_user(service_client)
    tok = _token()
    src = (
        service_client.table("sources")
        .insert(
            {"provider": "ashby", "board_token": tok, "company_name": "Co", "enabled": True}
        )
        .execute()
        .data[0]
    )
    client = user_client_factory(uid)  # type: ignore[operator]
    try:
        with pytest.raises(Exception):
            client.table("source_registrations").insert(
                {"user_id": uid, "source_id": src["id"]}
            ).execute()
        rows = (
            service_client.table("source_registrations")
            .select("user_id")
            .eq("user_id", uid)
            .execute()
            .data
        )
        assert rows == []  # nothing was written
    finally:
        _cleanup(service_client, [tok])
        delete_auth_user(service_client, uid)
