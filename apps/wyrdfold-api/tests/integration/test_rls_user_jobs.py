"""RLS integration test for the per-user user_jobs table (#75 C1).

Proves the policy on ``public.user_jobs`` scopes a JWT-bound user client to
its own rows: user A sees only A's row even with NO explicit user_id filter,
and cannot read B's. Runs against the live local Supabase stack (self-skips
when unreachable — see conftest).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

import pytest
from supabase import Client

pytestmark = pytest.mark.integration


@pytest.fixture
def seeded_user_jobs(
    service_client: Client, two_seeded_users: tuple[str, str]
) -> Iterator[tuple[str, str, str, str]]:
    """Seed one shared job posting + a user_jobs row per user, yield
    ``(uid_a, uid_b, posting_id, other_posting_id)``, then clean up.

    A second posting owned only by B lets us assert A can't read B's row.
    """
    uid_a, uid_b = two_seeded_users
    source_id = str(uuid.uuid4())
    posting_a = str(uuid.uuid4())
    posting_b = str(uuid.uuid4())
    board_token = f"test-{uuid.uuid4().hex[:12]}"
    try:
        # jobs.source_id FKs to sources; jobs rows are the FK target for
        # user_jobs.job_posting_id. Seed the minimum NOT NULL columns.
        service_client.table("sources").insert(
            {
                "id": source_id,
                "board_token": board_token,
                "company_name": "Acme",
                "provider": "greenhouse",
            }
        ).execute()
        service_client.table("jobs").insert(
            [
                {
                    "id": posting_a,
                    "external_id": "ext-a",
                    "source_id": source_id,
                    "title": "Job A",
                    "company_name": "Acme",
                    "status": "new",
                },
                {
                    "id": posting_b,
                    "external_id": "ext-b",
                    "source_id": source_id,
                    "title": "Job B",
                    "company_name": "Acme",
                    "status": "new",
                },
            ]
        ).execute()
        service_client.table("user_jobs").insert(
            [
                {
                    "user_id": uid_a,
                    "job_posting_id": posting_a,
                    "status": "applied",
                },
                {
                    "user_id": uid_b,
                    "job_posting_id": posting_b,
                    "status": "resume_draft",
                },
            ]
        ).execute()
        yield uid_a, uid_b, posting_a, posting_b
    finally:
        # ON DELETE CASCADE from sources -> jobs -> user_jobs cleans the rest.
        service_client.table("sources").delete().eq("id", source_id).execute()


def test_user_client_sees_only_own_user_jobs(
    user_client_factory: Callable[[str], Client],
    seeded_user_jobs: tuple[str, str, str, str],
) -> None:
    uid_a, uid_b, posting_a, posting_b = seeded_user_jobs
    client_a = user_client_factory(uid_a)

    # No user_id filter — RLS alone must scope the result to A's row.
    resp = client_a.table("user_jobs").select("*").execute()
    rows = resp.data or []

    assert len(rows) == 1
    assert rows[0]["user_id"] == uid_a
    assert rows[0]["job_posting_id"] == posting_a
    assert rows[0]["status"] == "applied"


def test_user_client_cannot_read_other_users_user_job(
    user_client_factory: Callable[[str], Client],
    seeded_user_jobs: tuple[str, str, str, str],
) -> None:
    uid_a, uid_b, posting_a, posting_b = seeded_user_jobs
    client_a = user_client_factory(uid_a)

    # Explicitly targeting B's posting still returns nothing for A.
    resp = (
        client_a.table("user_jobs")
        .select("*")
        .eq("job_posting_id", posting_b)
        .execute()
    )
    assert (resp.data or []) == []
