"""RLS gate for Phase 1 — tailor documents.

The tailor read/write endpoints now use `get_user_supabase`. `documents` has an
`auth.uid() = user_id` policy (same shape as experience_optimized_docs) and
`document_versions` is parent-scoped (same shape as experience_chunks). Prove
both scope to the caller on the live stack.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from postgrest.exceptions import APIError
from supabase import Client

pytestmark = pytest.mark.integration


def _doc(user_id: str) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "resume_type": "generic",
        "jd_snapshot": "jd text",
        "jd_snapshot_hash": "hash",
        "payload": {},
        "warnings": [],
        "document_type": "resume",
    }


def test_documents_read_is_rls_scoped(
    two_seeded_users: tuple[str, str],
    user_client_factory: Callable[[str], Client],
    service_client: Client,
) -> None:
    uid_a, uid_b = two_seeded_users
    service_client.table("documents").insert([_doc(uid_a), _doc(uid_b)]).execute()

    rows = (
        user_client_factory(uid_a).table("documents").select("user_id").execute().data
    )
    seen = {r["user_id"] for r in rows}
    assert uid_a in seen
    assert uid_b not in seen, "RLS leak: A sees B's tailored document"


def test_documents_write_own_ok_but_cross_user_denied(
    two_seeded_users: tuple[str, str],
    user_client_factory: Callable[[str], Client],
) -> None:
    uid_a, uid_b = two_seeded_users
    client_a = user_client_factory(uid_a)

    client_a.table("documents").insert(_doc(uid_a)).execute()
    with pytest.raises(APIError):
        client_a.table("documents").insert(_doc(uid_b)).execute()


def test_document_version_write_for_another_users_doc_is_denied(
    two_seeded_users: tuple[str, str],
    user_client_factory: Callable[[str], Client],
    service_client: Client,
) -> None:
    """A version can only attach to a document the caller owns — the
    parent-scoped WITH CHECK rejects a forged parent (like experience_chunks)."""
    uid_a, uid_b = two_seeded_users
    b_doc = service_client.table("documents").insert(_doc(uid_b)).execute().data[0]
    client_a = user_client_factory(uid_a)

    with pytest.raises(APIError):
        client_a.table("document_versions").insert(
            {"resume_id": b_doc["id"], "payload": {}, "source": "edit"}
        ).execute()
