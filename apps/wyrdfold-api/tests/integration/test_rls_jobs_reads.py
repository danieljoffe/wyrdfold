"""RLS integration test for per-user status in the jobs-list read (#75 C2).

Proves the ``get_target_jobs`` RPC resolves a job's ``status`` from the
caller's ``user_jobs`` row (the new ``p_user_id`` param): two users who
share the SAME scored job see DIFFERENT statuses, and a NULL ``p_user_id``
(api-key/cron path) sees ``'new'``. Runs against the live local Supabase
stack (self-skips when unreachable — see conftest).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from supabase import Client

pytestmark = pytest.mark.integration


@pytest.fixture
def seeded_shared_job(
    service_client: Client, two_seeded_users: tuple[str, str]
) -> Iterator[tuple[str, str, str, str]]:
    """Seed one source + one job + one target + a score linking them, then
    link BOTH users to the target and give each a different per-user status
    on the SAME job. Yields ``(uid_a, uid_b, target_id, posting_id)`` and
    cleans up afterwards (source cascade removes jobs/scores/user_jobs;
    targets + user_targets are deleted explicitly).
    """
    uid_a, uid_b = two_seeded_users
    source_id = str(uuid.uuid4())
    target_id = str(uuid.uuid4())
    posting_id = str(uuid.uuid4())
    board_token = f"test-{uuid.uuid4().hex[:12]}"
    try:
        service_client.table("sources").insert(
            {
                "id": source_id,
                "board_token": board_token,
                "company_name": "Acme",
                "provider": "greenhouse",
            }
        ).execute()
        service_client.table("jobs").insert(
            {
                "id": posting_id,
                "external_id": "ext-shared",
                "source_id": source_id,
                "title": "Shared Job",
                "company_name": "Acme",
            }
        ).execute()
        service_client.table("targets").insert({"id": target_id, "label": "Test Target"}).execute()
        # Score links target -> job; score 50, not excluded -> shows in list.
        service_client.table("scores").insert(
            {
                "job_posting_id": posting_id,
                "target_id": target_id,
                "score": 50,
                "excluded": False,
            }
        ).execute()
        # Both users active on the target.
        service_client.table("user_targets").insert(
            [
                {"user_id": uid_a, "target_id": target_id, "is_active": True},
                {"user_id": uid_b, "target_id": target_id, "is_active": True},
            ]
        ).execute()
        # Per-user status on the SAME job: A applied, B saved.
        service_client.table("user_jobs").insert(
            [
                {"user_id": uid_a, "job_posting_id": posting_id, "status": "applied"},
                {"user_id": uid_b, "job_posting_id": posting_id, "status": "saved"},
            ]
        ).execute()
        yield uid_a, uid_b, target_id, posting_id
    finally:
        service_client.table("user_targets").delete().eq("target_id", target_id).execute()
        # source cascade -> jobs -> scores + user_jobs
        service_client.table("sources").delete().eq("id", source_id).execute()
        service_client.table("targets").delete().eq("id", target_id).execute()


def _status_for(
    service_client: Client, target_id: str, posting_id: str, user_id: str | None
) -> str:
    resp = service_client.rpc(
        "get_target_jobs",
        {
            "p_target_id": target_id,
            "p_min_score": 0,
            "p_status": None,
            "p_company": None,
            "p_search": None,
            "p_sort": "score",
            "p_ascending": False,
            "p_limit": 20,
            "p_after_value": None,
            "p_after_id": None,
            "p_user_id": user_id,
        },
    ).execute()
    rows = [r for r in (resp.data or []) if r["id"] == posting_id]
    assert len(rows) == 1, f"expected the shared job once, got {resp.data!r}"
    return rows[0]["status"]


def test_get_target_jobs_resolves_per_user_status(
    service_client: Client,
    seeded_shared_job: tuple[str, str, str, str],
) -> None:
    uid_a, uid_b, target_id, posting_id = seeded_shared_job

    # Same job, different callers -> different per-user status.
    assert _status_for(service_client, target_id, posting_id, uid_a) == "applied"
    assert _status_for(service_client, target_id, posting_id, uid_b) == "saved"
    # NULL caller (api-key/cron path) has no user_jobs row -> 'new'.
    assert _status_for(service_client, target_id, posting_id, None) == "new"
