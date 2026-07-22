"""RLS integration test for per-user status in the jobs-list read (#75 C2).

Proves the ``get_target_jobs`` RPC resolves a job's ``status`` from the
caller's ``user_jobs`` row (the new ``p_user_id`` param): two users who
share the SAME scored job see DIFFERENT statuses, and a NULL ``p_user_id``
(api-key/cron path) sees ``'new'``. Runs against the live local Supabase
stack (self-skips when unreachable — see conftest).

#88 dual-auth flip: GET /jobs, /pipeline-counts and GET /{id} now run on the
caller's RLS user client for JWT callers (``get_supabase_for_caller``). The
second half of this file proves that flip is loss-free and adds teeth:

* both list RPCs (``get_target_jobs``, ``pipeline_counts``) return
  IDENTICAL results on a user-JWT client vs the service-role client for
  the same user — no silently-lost rows on the hottest path; and
* the RPCs are SECURITY INVOKER, so a JWT client passing ANOTHER user's
  ``p_user_id`` can no longer read that user's pipeline state — RLS on
  ``user_jobs`` is the control, not the honesty of the parameter.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

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


def _list_rows(client: Client, target_id: str, user_id: str | None) -> list[dict[str, object]]:
    """The exact ``get_target_jobs`` call the list endpoint makes."""
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
    return list(resp.data or [])


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


def test_list_rpc_identical_on_user_jwt_client(
    service_client: Client,
    user_client_factory: Callable[[str], Client],
    seeded_shared_job: tuple[str, str, str, str],
) -> None:
    """#88 flip equivalence on the hottest path: the LIST driven through a
    user JWT returns row-for-row what the service-role client returned for
    the same user — including the per-user status overlay."""
    uid_a, _, target_id, posting_id = seeded_shared_job
    via_jwt = _list_rows(user_client_factory(uid_a), target_id, uid_a)
    via_service = _list_rows(service_client, target_id, uid_a)
    assert via_jwt == via_service
    # Not vacuously equal: the seeded job is present with A's status.
    mine = [r for r in via_jwt if r["id"] == posting_id]
    assert len(mine) == 1 and mine[0]["status"] == "applied"


def test_pipeline_counts_rpc_identical_on_user_jwt_client(
    service_client: Client,
    user_client_factory: Callable[[str], Client],
    seeded_shared_job: tuple[str, str, str, str],
) -> None:
    """Same equivalence for the /pipeline-counts RPC."""
    uid_a, _, target_id, _ = seeded_shared_job
    params = {"p_target_ids": [target_id], "p_min_score": None, "p_user_id": uid_a}
    via_jwt = user_client_factory(uid_a).rpc("pipeline_counts", params).execute().data
    via_service = service_client.rpc("pipeline_counts", params).execute().data
    assert via_jwt == via_service
    assert {r["status"]: r["count"] for r in via_jwt or []} == {"applied": 1}


def test_pipeline_counts_floor_exempts_pending_and_gates_liveness(
    service_client: Client, two_seeded_users: tuple[str, str]
) -> None:
    """Live proof of the 20260716050000 RPC semantics (the floor the list
    applies via ``_apply_score_floor``): with a floor set, a graded row below
    the floor is dropped, a Pending row (``scoring_status`` ≠ 'complete')
    passes regardless of its placeholder score, and a purged job never counts
    even above the floor."""
    uid_a, _ = two_seeded_users
    source_id = str(uuid.uuid4())
    target_id = str(uuid.uuid4())
    jobs = {
        "graded_pass": str(uuid.uuid4()),  # complete, 80 → counted
        "graded_fail": str(uuid.uuid4()),  # complete, 30 → floored out
        "pending_low": str(uuid.uuid4()),  # stage2, 30 → exempt, counted
        "purged_pass": str(uuid.uuid4()),  # complete, 80, purged → gated out
    }
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
            [
                {
                    "id": jid,
                    "external_id": f"ext-{name}",
                    "source_id": source_id,
                    "title": f"Job {name}",
                    "company_name": "Acme",
                    # purged implies archived (jobs_purged_implies_archived CHECK,
                    # 20260721120000) — a tombstone is always archived first, so a
                    # purged-but-unarchived row is a state prod can't produce. Set
                    # archived_at alongside purged_at to match the invariant.
                    "purged_at": ("2026-01-01T00:00:00Z" if name == "purged_pass" else None),
                    "archived_at": ("2026-01-01T00:00:00Z" if name == "purged_pass" else None),
                }
                for name, jid in jobs.items()
            ]
        ).execute()
        service_client.table("targets").insert({"id": target_id, "label": "Floor Target"}).execute()
        service_client.table("scores").insert(
            [
                {
                    "job_posting_id": jobs["graded_pass"],
                    "target_id": target_id,
                    "score": 80,
                    "scoring_status": "complete",
                    "excluded": False,
                },
                {
                    "job_posting_id": jobs["graded_fail"],
                    "target_id": target_id,
                    "score": 30,
                    "scoring_status": "complete",
                    "excluded": False,
                },
                {
                    "job_posting_id": jobs["pending_low"],
                    "target_id": target_id,
                    "score": 30,
                    "scoring_status": "stage2",
                    "excluded": False,
                },
                {
                    "job_posting_id": jobs["purged_pass"],
                    "target_id": target_id,
                    "score": 80,
                    "scoring_status": "complete",
                    "excluded": False,
                },
            ]
        ).execute()
        # Distinct per-user statuses on the two decisive rows, so the grouped
        # result distinguishes them: the pre-fix RPC also totalled 2 here
        # (purged_pass leaked in exactly as pending_low was floored out), and a
        # bare total would have passed against the old SQL by coincidence.
        service_client.table("user_jobs").insert(
            [
                {"user_id": uid_a, "job_posting_id": jobs["pending_low"], "status": "saved"},
                {"user_id": uid_a, "job_posting_id": jobs["purged_pass"], "status": "applied"},
            ]
        ).execute()
        counts = (
            service_client.rpc(
                "pipeline_counts",
                {"p_target_ids": [target_id], "p_min_score": 70, "p_user_id": uid_a},
            )
            .execute()
            .data
        )
        # graded_pass (above floor, no user_jobs row) = 'new'; pending_low
        # (floor-exempt) = 'saved'. graded_fail floored out; purged_pass
        # liveness-gated out — its 'applied' status must NOT appear.
        assert {r["status"]: r["count"] for r in counts or []} == {"new": 1, "saved": 1}
    finally:
        # source cascade -> jobs -> scores
        service_client.table("sources").delete().eq("id", source_id).execute()
        service_client.table("targets").delete().eq("id", target_id).execute()


def test_invoker_rpc_blocks_spoofed_user_id_on_jwt_client(
    user_client_factory: Callable[[str], Client],
    seeded_shared_job: tuple[str, str, str, str],
) -> None:
    """The flip's payoff: the RPCs are SECURITY INVOKER, so when B's JWT
    client passes A's ``p_user_id``, RLS on ``user_jobs`` hides A's rows and
    the status degrades to 'new' — B cannot read A's pipeline state by lying
    about the parameter. (On the service-role client the same spoofed call
    WOULD return A's status; the endpoints always derive ``p_user_id`` from
    the JWT, and this proves the DB now backstops that contract.)"""
    uid_a, uid_b, target_id, posting_id = seeded_shared_job
    spoofed = _list_rows(user_client_factory(uid_b), target_id, uid_a)
    row = next(r for r in spoofed if r["id"] == posting_id)
    assert row["status"] == "new"  # NOT A's 'applied'

    counts = (
        user_client_factory(uid_b)
        .rpc(
            "pipeline_counts",
            {"p_target_ids": [target_id], "p_min_score": None, "p_user_id": uid_a},
        )
        .execute()
        .data
    )
    assert {r["status"]: r["count"] for r in counts or []} == {"new": 1}
