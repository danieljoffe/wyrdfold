"""Integration test for the global archive split (#75 C3).

Proves the ``get_target_jobs`` RPC gates on the new GLOBAL liveness column
``jobs.archived_at``, independent of the caller's per-user status:

  Case 1: a LIVE job (archived_at IS NULL) with NO user_jobs row resolves to
          status ``'new'`` and appears in the list.
  Case 2: once the job is globally archived (archived_at set), it is excluded
          from the list entirely — regardless of per-user status.

Runs against the live local Supabase stack (self-skips when unreachable —
see conftest).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from supabase import Client

pytestmark = pytest.mark.integration


@pytest.fixture
def seeded_target_job(
    service_client: Client, two_seeded_users: tuple[str, str]
) -> Iterator[tuple[str, str, str]]:
    """Seed a source + a live job + a target + a score (target -> job) and
    link user A to the target via user_targets. NO user_jobs row is seeded,
    so A's per-user status resolves to the default ``'new'``.

    Yields ``(uid_a, target_id, posting_id)`` and cleans up afterwards
    (source cascade removes jobs/scores; targets + user_targets explicit).
    """
    uid_a, _uid_b = two_seeded_users
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
                "external_id": "ext-live",
                "source_id": source_id,
                "title": "Live Job",
                "company_name": "Acme",
                # archived_at left NULL -> globally live.
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
        service_client.table("user_targets").insert(
            {"user_id": uid_a, "target_id": target_id, "is_active": True}
        ).execute()
        yield uid_a, target_id, posting_id
    finally:
        service_client.table("user_targets").delete().eq("target_id", target_id).execute()
        # source cascade -> jobs -> scores
        service_client.table("sources").delete().eq("id", source_id).execute()
        service_client.table("targets").delete().eq("id", target_id).execute()


def _get_target_jobs(client: Client, target_id: str, user_id: str | None) -> list[dict]:
    resp = client.rpc(
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
    return resp.data or []


def test_live_job_with_no_user_jobs_row_resolves_new(
    service_client: Client,
    seeded_target_job: tuple[str, str, str],
) -> None:
    """Case 1: a live job with no per-user row appears as 'new'."""
    uid_a, target_id, posting_id = seeded_target_job

    rows = _get_target_jobs(service_client, target_id, uid_a)
    matched = [r for r in rows if r["id"] == posting_id]
    assert len(matched) == 1, f"expected the live job once, got {rows!r}"
    assert matched[0]["status"] == "new"


def test_globally_archived_job_is_excluded(
    service_client: Client,
    seeded_target_job: tuple[str, str, str],
) -> None:
    """Case 2: setting jobs.archived_at hides the job from the list,
    regardless of the caller's per-user status."""
    uid_a, target_id, posting_id = seeded_target_job

    # Globally archive the job via the service client.
    service_client.table("jobs").update({"archived_at": datetime.now(UTC).isoformat()}).eq(
        "id", posting_id
    ).execute()

    rows = _get_target_jobs(service_client, target_id, uid_a)
    matched = [r for r in rows if r["id"] == posting_id]
    assert matched == [], f"globally-archived job should be excluded, got {rows!r}"
