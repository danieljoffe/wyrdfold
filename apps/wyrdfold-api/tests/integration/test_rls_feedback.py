"""RLS gate for job_feedback (#79 Phase 2 — job_feedback read slice).

Proves what the mock suite can't: with NO Python ``user_id`` filter, a
JWT-bound user client reading ``job_feedback`` sees only its own rows —
Postgres RLS (``job_feedback_self_select``) is the control. If
``list_feedback`` ever drops its filter, or the policy regresses, this
fails. The route migration to the user client (``get_user_supabase``) is
what makes RLS the backstop instead of hand-written Python.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from supabase import Client

pytestmark = pytest.mark.integration


@pytest.fixture
def seeded_feedback(
    service_client: Client, two_seeded_users: tuple[str, str]
) -> Iterator[tuple[str, str]]:
    """Seed one job_feedback row for each of two users (sharing one
    catalog job + target), then clean up. Parents are required by the
    job_feedback FKs (job_posting_id -> jobs, target_id -> targets).
    """
    uid_a, uid_b = two_seeded_users
    source_id = (
        service_client.table("sources")
        .insert({"board_token": "rls-int", "company_name": "RLS Co"})
        .execute()
        .data[0]["id"]
    )
    target_id = (
        service_client.table("targets")
        .insert({"label": "RLS Int Target"})
        .execute()
        .data[0]["id"]
    )
    job_id = (
        service_client.table("jobs")
        .insert(
            {
                "external_id": "rls-int-1",
                "source_id": source_id,
                "title": "Engineer",
                "company_name": "RLS Co",
            }
        )
        .execute()
        .data[0]["id"]
    )
    service_client.table("job_feedback").insert(
        [
            {
                "user_id": uid_a,
                "job_posting_id": job_id,
                "target_id": target_id,
                "signal": "relevant",
            },
            {
                "user_id": uid_b,
                "job_posting_id": job_id,
                "target_id": target_id,
                "signal": "irrelevant",
            },
        ]
    ).execute()
    try:
        yield uid_a, uid_b
    finally:
        # Deleting the job/target would cascade the feedback, but be explicit.
        service_client.table("job_feedback").delete().in_(
            "user_id", [uid_a, uid_b]
        ).execute()
        service_client.table("jobs").delete().eq("id", job_id).execute()
        service_client.table("targets").delete().eq("id", target_id).execute()
        service_client.table("sources").delete().eq("id", source_id).execute()


def test_job_feedback_select_is_rls_scoped(
    seeded_feedback: tuple[str, str],
    user_client_factory: Callable[[str], Client],
) -> None:
    uid_a, uid_b = seeded_feedback

    # No `.eq("user_id", ...)` anywhere — RLS alone must scope the result.
    rows_a = (
        user_client_factory(uid_a).table("job_feedback").select("user_id").execute().data
    )
    assert rows_a, "user A should see their own feedback"
    assert all(r["user_id"] == uid_a for r in rows_a), f"cross-tenant leak: {rows_a}"

    rows_b = (
        user_client_factory(uid_b).table("job_feedback").select("user_id").execute().data
    )
    assert all(r["user_id"] == uid_b for r in rows_b), f"cross-tenant leak: {rows_b}"
