"""RLS gate for #79 Phase 2 — experience reads.

Same proof as the profile-reads gate, for the experience tables: a read
issued through the JWT-bound user client is scoped by RLS (`auth.uid() =
user_id`), so the caller never sees another user's experience data — even
with no `.eq("user_id", ...)` filter, and through the actual production
read function (`prose.get_latest`).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from supabase import Client

pytestmark = pytest.mark.integration


@pytest.fixture
def seeded_prose(
    service_client: Client, two_seeded_users: tuple[str, str]
) -> Iterator[tuple[str, str]]:
    uid_a, uid_b = two_seeded_users
    service_client.table("experience_prose_docs").insert(
        [
            {"user_id": uid_a, "version": 1, "content": "A resume"},
            {"user_id": uid_b, "version": 1, "content": "B resume"},
        ]
    ).execute()
    try:
        yield uid_a, uid_b
    finally:
        service_client.table("experience_prose_docs").delete().in_(
            "user_id", [uid_a, uid_b]
        ).execute()


def test_prose_select_is_rls_scoped_without_python_filter(
    seeded_prose: tuple[str, str],
    user_client_factory: Callable[[str], Client],
) -> None:
    uid_a, uid_b = seeded_prose
    client_a = user_client_factory(uid_a)

    # No .eq("user_id", ...) — RLS alone must scope this.
    rows = (
        client_a.table("experience_prose_docs").select("user_id, content").execute().data
    )

    seen = {r["user_id"] for r in rows}
    assert uid_a in seen
    assert uid_b not in seen, "RLS leak: user A sees user B's prose doc"


def test_prose_get_latest_via_user_client_returns_only_own(
    seeded_prose: tuple[str, str],
    user_client_factory: Callable[[str], Client],
) -> None:
    """The real production read path (`prose.get_latest`) over the user
    client returns the caller's doc and nothing else."""
    from app.services.experience import prose

    uid_a, _uid_b = seeded_prose
    client_a = user_client_factory(uid_a)

    doc = prose.get_latest(client_a, user_id=uid_a)
    assert doc is not None
    assert doc.content == "A resume"
