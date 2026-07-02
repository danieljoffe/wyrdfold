"""RLS gate for Phase 1 — the experience "slice 2" tables.

`optimized` GET, `preferences`, and `turns` endpoints now use
`get_user_supabase` (Phase 1), so each table's `auth.uid() = user_id` policy
must scope reads to the caller and reject a forged-owner write. Proven here on
the live stack via the JWT-bound user client (the same client the endpoints use).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from postgrest.exceptions import APIError
from supabase import Client

pytestmark = pytest.mark.integration


def test_optimized_docs_read_is_rls_scoped(
    two_seeded_users: tuple[str, str],
    user_client_factory: Callable[[str], Client],
    service_client: Client,
) -> None:
    uid_a, uid_b = two_seeded_users
    service_client.table("experience_optimized_docs").insert(
        [
            {"user_id": uid_a, "version": 1, "payload": {"summary": "A"}, "source": "llm"},
            {"user_id": uid_b, "version": 1, "payload": {"summary": "B"}, "source": "llm"},
        ]
    ).execute()

    rows = (
        user_client_factory(uid_a)
        .table("experience_optimized_docs")
        .select("user_id")
        .execute()
        .data
    )
    seen = {r["user_id"] for r in rows}
    assert uid_a in seen
    assert uid_b not in seen, "RLS leak: A sees B's optimized doc"


def test_preferences_write_own_ok_but_cross_user_denied(
    two_seeded_users: tuple[str, str],
    user_client_factory: Callable[[str], Client],
) -> None:
    uid_a, uid_b = two_seeded_users
    client_a = user_client_factory(uid_a)

    # Own write is permitted by the WITH CHECK policy...
    client_a.table("experience_preferences").insert(
        {"user_id": uid_a, "payload": {"x": 1}}
    ).execute()
    rows = client_a.table("experience_preferences").select("user_id").execute().data
    assert {r["user_id"] for r in rows} == {uid_a}

    # ...but forging user B's ownership is rejected in-DB.
    with pytest.raises(APIError):
        client_a.table("experience_preferences").insert(
            {"user_id": uid_b, "payload": {"forged": 1}}
        ).execute()


def test_turns_write_own_ok_and_read_scoped(
    two_seeded_users: tuple[str, str],
    user_client_factory: Callable[[str], Client],
    service_client: Client,
) -> None:
    uid_a, uid_b = two_seeded_users
    # B's turn, seeded via service-role (bypasses RLS).
    service_client.table("experience_conversation_turns").insert(
        {
            "user_id": uid_b,
            "conversation_type": "onboarding",
            "turn_index": 0,
            "role": "user",
            "content": "B's turn",
        }
    ).execute()

    client_a = user_client_factory(uid_a)
    client_a.table("experience_conversation_turns").insert(
        {
            "user_id": uid_a,
            "conversation_type": "onboarding",
            "turn_index": 0,
            "role": "user",
            "content": "A's turn",
        }
    ).execute()

    rows = (
        client_a.table("experience_conversation_turns").select("user_id").execute().data
    )
    seen = {r["user_id"] for r in rows}
    assert uid_a in seen
    assert uid_b not in seen, "RLS leak: A sees B's conversation turn"


def test_reset_content_via_user_client_wipes_only_own(
    two_seeded_users: tuple[str, str],
    user_client_factory: Callable[[str], Client],
    service_client: Client,
) -> None:
    """delete / conversation/reset run reset_content on the RLS client. A
    destructive wipe must only remove the caller's rows — RLS's DELETE policy
    (auth.uid() = user_id) enforces that even though the query filters by
    user_id in app code too. Another user's data is untouched.
    """
    from app.services.conversation import orchestrator

    uid_a, uid_b = two_seeded_users
    service_client.table("experience_prose_docs").insert(
        [
            {"user_id": uid_a, "version": 1, "content": "A resume"},
            {"user_id": uid_b, "version": 1, "content": "B resume"},
        ]
    ).execute()

    orchestrator.reset_content(user_client_factory(uid_a), user_id=uid_a)

    # Checked via service-role (bypasses RLS) so we see the true DB state.
    a_left = (
        service_client.table("experience_prose_docs")
        .select("id")
        .eq("user_id", uid_a)
        .execute()
        .data
    )
    b_left = (
        service_client.table("experience_prose_docs")
        .select("id")
        .eq("user_id", uid_b)
        .execute()
        .data
    )
    assert len(a_left) == 0, "A's own prose should be wiped"
    assert len(b_left) == 1, "B's prose must survive A's reset (no cross-user wipe)"
