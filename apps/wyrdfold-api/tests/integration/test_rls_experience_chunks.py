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

from app.models.experience import OptimizedPayload, Skill

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


def _one_chunk_payload() -> OptimizedPayload:
    return OptimizedPayload(
        summary="hi", roles=[], skills=[Skill(name="React")], outcomes=[]
    )


async def test_upsert_writes_chunks_via_rls_and_cost_via_service_role(
    two_seeded_users: tuple[str, str],
    user_client_factory: Callable[[str], Client],
    service_client: Client,
) -> None:
    """The Phase-1 dual-client fix, end-to-end on the real path: chunks write
    through the RLS user client (parent-scoped) while the cost row goes through
    the service-role client. Both must land. Regression for the #159 bug, where
    the cost write went through the RLS client and would 500 in prod.
    """
    from app.services.embeddings.mock import MockEmbeddingsClient
    from app.services.experience import chunks, optimized

    uid_a, _uid_b = two_seeded_users
    client_a = user_client_factory(uid_a)
    doc = optimized.create_version(
        client_a,
        user_id=uid_a,
        payload=_one_chunk_payload(),
        prose_doc_id=None,
        source="llm",
    )

    written = await chunks.upsert_for_optimized(
        client_a, MockEmbeddingsClient(), doc, user_id=uid_a, cost_supabase=service_client
    )
    assert len(written) >= 1  # chunks written via the RLS client

    costs = (
        client_a.table("llm_costs")
        .select("id")
        .eq("user_id", uid_a)
        .eq("purpose", chunks.DEFAULT_PURPOSE)
        .execute()
        .data
    )
    assert len(costs) >= 1  # cost row written via the service-role client


async def test_upsert_cost_through_user_client_is_denied(
    two_seeded_users: tuple[str, str],
    user_client_factory: Callable[[str], Client],
) -> None:
    """Regression: routing the cost write through the RLS (user) client — what
    #159 did — is denied by llm_costs RLS. This is why cost_supabase must be a
    service-role client.
    """
    from app.services.embeddings.mock import MockEmbeddingsClient
    from app.services.experience import chunks, optimized

    uid_a, _uid_b = two_seeded_users
    client_a = user_client_factory(uid_a)
    doc = optimized.create_version(
        client_a,
        user_id=uid_a,
        payload=_one_chunk_payload(),
        prose_doc_id=None,
        source="llm",
    )

    with pytest.raises(APIError):
        await chunks.upsert_for_optimized(
            client_a, MockEmbeddingsClient(), doc, user_id=uid_a, cost_supabase=client_a
        )
