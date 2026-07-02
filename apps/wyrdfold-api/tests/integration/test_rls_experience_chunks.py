"""RLS gate for Phase 1 slice-3 — the derive / optimized-POST write path.

Those endpoints (now on `get_user_supabase`) write the optimized doc THEN its
embedding chunks through the JWT-bound user client. `experience_chunks` has a
*parent-scoped* policy — a chunk is writable only if its `optimized_doc_id`
belongs to a doc the caller owns. Prove both legs succeed for own data and that
a chunk cannot be attached to another user's doc.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from postgrest.exceptions import APIError
from supabase import Client

pytestmark = pytest.mark.integration


def test_optimized_doc_then_chunk_write_under_rls(
    two_seeded_users: tuple[str, str],
    user_client_factory: Callable[[str], Client],
) -> None:
    uid_a, _uid_b = two_seeded_users
    client_a = user_client_factory(uid_a)

    # Leg 1: the optimized doc (WITH CHECK auth.uid() = user_id).
    doc = (
        client_a.table("experience_optimized_docs")
        .insert({"user_id": uid_a, "version": 1, "payload": {"summary": "A"}, "source": "llm"})
        .execute()
        .data[0]
    )
    # Leg 2: a chunk of that just-created doc — the parent-scoped WITH CHECK must
    # resolve the doc as the caller's and permit the write.
    client_a.table("experience_chunks").insert(
        {
            "optimized_doc_id": doc["id"],
            "chunk_type": "role",
            "chunk_ref": "r1",
            "content": "chunk content",
            "metadata": {},
        }
    ).execute()

    rows = (
        client_a.table("experience_chunks")
        .select("id")
        .eq("optimized_doc_id", doc["id"])
        .execute()
        .data
    )
    assert len(rows) == 1


def test_chunk_write_for_another_users_doc_is_denied(
    two_seeded_users: tuple[str, str],
    user_client_factory: Callable[[str], Client],
    service_client: Client,
) -> None:
    uid_a, uid_b = two_seeded_users
    # B's optimized doc, seeded via service-role.
    b_doc = (
        service_client.table("experience_optimized_docs")
        .insert({"user_id": uid_b, "version": 1, "payload": {}, "source": "llm"})
        .execute()
        .data[0]
    )
    client_a = user_client_factory(uid_a)

    # A cannot attach a chunk to B's doc — the parent-scoped WITH CHECK rejects it.
    with pytest.raises(APIError):
        client_a.table("experience_chunks").insert(
            {
                "optimized_doc_id": b_doc["id"],
                "chunk_type": "role",
                "chunk_ref": "r1",
                "content": "forged",
                "metadata": {},
            }
        ).execute()
