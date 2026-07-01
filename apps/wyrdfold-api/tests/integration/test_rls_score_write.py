"""RLS / authz gate for the user-facing scores write (#6 R2).

`scores` is the shared catalog and has no write policy, so a user JWT can't
touch it directly. The analysis-blend write goes through the
`user_apply_score_blend` SECURITY DEFINER RPC, which re-checks target
ownership against `auth.uid()` in the DB. These prove that gate end-to-end:
a follower can blend their target's score, a non-follower is rejected by the
RPC, and the service-role (poller/operator, auth.uid() NULL) is exempt.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

import pytest
from postgrest.exceptions import APIError
from supabase import Client

from app.constants import SYSTEM_USER_ID

pytestmark = pytest.mark.integration


@pytest.fixture
def seeded_for_blend(
    service_client: Client, two_seeded_users: tuple[str, str]
) -> Iterator[tuple[str, str, str, str, str, str]]:
    """Seed a source + job + two targets + a score on target_a, and link user A
    to target_a and user B to target_b via user_targets. Yields
    (uid_a, uid_b, job_id, target_a, target_b)."""
    uid_a, uid_b = two_seeded_users
    source_id = (
        service_client.table("sources")
        .insert({"board_token": f"r2-{uuid.uuid4().hex[:10]}", "company_name": "R2 Co"})
        .execute()
        .data[0]["id"]
    )
    target_a = (
        service_client.table("targets").insert({"label": "R2 Target A"}).execute().data[0]["id"]
    )
    target_b = (
        service_client.table("targets").insert({"label": "R2 Target B"}).execute().data[0]["id"]
    )
    job_id = (
        service_client.table("jobs")
        .insert(
            {"external_id": "r2-1", "source_id": source_id, "title": "Eng", "company_name": "R2 Co"}
        )
        .execute()
        .data[0]["id"]
    )
    service_client.table("scores").insert(
        {"job_posting_id": job_id, "target_id": target_a, "score": 50, "excluded": False}
    ).execute()
    service_client.table("user_targets").insert(
        [
            {"user_id": uid_a, "target_id": target_a, "is_active": True},
            {"user_id": uid_b, "target_id": target_b, "is_active": True},
        ]
    ).execute()
    # jobs.llm_analysis_id FKs to analyses.id, so seed a real analysis to stamp
    # (prod always persists the analysis before this blend write runs).
    analysis_id = (
        service_client.table("analyses")
        .insert(
            {
                "job_posting_id": job_id,
                "target_id": target_a,
                # analyses.user_id is NOT NULL (Phase 0); this seed exists only to
                # satisfy jobs.llm_analysis_id, so the system principal owns it.
                "user_id": SYSTEM_USER_ID,
                "scorecard": {},
                "recommendation": "seed",
                "model": "test",
            }
        )
        .execute()
        .data[0]["id"]
    )
    try:
        yield uid_a, uid_b, job_id, target_a, target_b, analysis_id
    finally:
        service_client.table("user_targets").delete().in_("user_id", [uid_a, uid_b]).execute()
        # Clear the jobs->analyses ref before deleting analyses (circular FK).
        service_client.table("jobs").update({"llm_analysis_id": None}).eq("id", job_id).execute()
        service_client.table("analyses").delete().eq("job_posting_id", job_id).execute()
        service_client.table("sources").delete().eq("id", source_id).execute()
        service_client.table("targets").delete().in_("id", [target_a, target_b]).execute()


def _score(service_client: Client, job_id: str, target_id: str) -> int:
    rows = (
        service_client.table("scores")
        .select("score")
        .eq("job_posting_id", job_id)
        .eq("target_id", target_id)
        .execute()
        .data
    )
    assert rows
    return int(rows[0]["score"])


def test_follower_can_blend_their_targets_score(
    seeded_for_blend: tuple[str, str, str, str, str, str],
    service_client: Client,
    user_client_factory: Callable[[str], Client],
) -> None:
    uid_a, _uid_b, job_id, target_a, _target_b, analysis_id = seeded_for_blend

    user_client_factory(uid_a).rpc(
        "user_apply_score_blend",
        {
            "p_job_posting_id": job_id,
            "p_target_id": target_a,
            "p_score": 88,
            "p_analysis_id": analysis_id,
        },
    ).execute()

    assert _score(service_client, job_id, target_a) == 88


def test_non_follower_is_rejected_by_the_rpc(
    seeded_for_blend: tuple[str, str, str, str, str, str],
    service_client: Client,
    user_client_factory: Callable[[str], Client],
) -> None:
    _uid_a, uid_b, job_id, target_a, _target_b, analysis_id = seeded_for_blend

    # User B does NOT follow target_a — the DEFINER RPC's in-body auth.uid()
    # check must reject this even though the function runs as owner.
    with pytest.raises(APIError):
        user_client_factory(uid_b).rpc(
            "user_apply_score_blend",
            {
                "p_job_posting_id": job_id,
                "p_target_id": target_a,
                "p_score": 1,
                "p_analysis_id": analysis_id,
            },
        ).execute()

    assert _score(service_client, job_id, target_a) == 50, "non-follower mutated a shared score"


def test_service_role_is_exempt(
    seeded_for_blend: tuple[str, str, str, str, str, str],
    service_client: Client,
) -> None:
    # service-role (auth.uid() NULL) is the poller/operator path — exempt.
    _uid_a, _uid_b, job_id, target_a, _target_b, analysis_id = seeded_for_blend

    service_client.rpc(
        "user_apply_score_blend",
        {
            "p_job_posting_id": job_id,
            "p_target_id": target_a,
            "p_score": 77,
            "p_analysis_id": analysis_id,
        },
    ).execute()

    assert _score(service_client, job_id, target_a) == 77


# --- R2 step 2: manual-add scores write RPCs -------------------------------


def _row(job_id: str, target_id: str, score: int) -> dict:
    return {
        "job_posting_id": job_id,
        "target_id": target_id,
        "score": score,
        "score_breakdown": {},
        "matched_keywords": [],
        "excluded": False,
        "scoring_status": "stage2",
        "scored_profile_version": 1,
        "recency_score": score,
        "updated_at": "2026-01-01T00:00:00+00:00",
    }


def test_user_upsert_score_enforces_ownership(
    seeded_for_blend: tuple[str, str, str, str, str, str],
    service_client: Client,
    user_client_factory: Callable[[str], Client],
) -> None:
    uid_a, uid_b, job_id, target_a, _target_b, _aid = seeded_for_blend

    # Follower upserts their own target's score.
    user_client_factory(uid_a).rpc(
        "user_upsert_score", {"p_row": _row(job_id, target_a, 70)}
    ).execute()
    assert _score(service_client, job_id, target_a) == 70

    # Non-follower (B doesn't follow target_a) is rejected by the RPC.
    with pytest.raises(APIError):
        user_client_factory(uid_b).rpc(
            "user_upsert_score", {"p_row": _row(job_id, target_a, 5)}
        ).execute()
    assert _score(service_client, job_id, target_a) == 70


def test_user_set_scores_included_enforces_ownership(
    seeded_for_blend: tuple[str, str, str, str, str, str],
    service_client: Client,
    user_client_factory: Callable[[str], Client],
) -> None:
    uid_a, uid_b, job_id, target_a, _target_b, _aid = seeded_for_blend
    service_client.table("scores").update({"excluded": True}).eq(
        "job_posting_id", job_id
    ).eq("target_id", target_a).execute()

    # Non-follower can't force-include.
    with pytest.raises(APIError):
        user_client_factory(uid_b).rpc(
            "user_set_scores_included",
            {"p_job_posting_id": job_id, "p_target_ids": [target_a]},
        ).execute()

    # Follower can.
    user_client_factory(uid_a).rpc(
        "user_set_scores_included",
        {"p_job_posting_id": job_id, "p_target_ids": [target_a]},
    ).execute()
    rows = (
        service_client.table("scores")
        .select("excluded")
        .eq("job_posting_id", job_id)
        .eq("target_id", target_a)
        .execute()
        .data
    )
    assert rows and rows[0]["excluded"] is False


def test_anon_cannot_call_score_write_rpcs(
    seeded_for_blend: tuple[str, str, str, str, str, str],
    service_client: Client,
    anon_client: Client,
) -> None:
    """#2 audit regression: these score-write DEFINER RPCs carried a leftover
    `anon` EXECUTE grant (Supabase default privileges; #6 R2's
    `REVOKE ... FROM PUBLIC` didn't remove the explicit per-role grant). Their
    in-body guard exempts ``auth.uid() IS NULL`` — which an anon caller is — so
    an unauthenticated anon-key holder could write the shared `scores` catalog
    and stamp `jobs.llm_analysis_id`. Anon must now be denied EXECUTE entirely:
    PostgREST stops exposing the RPC to the `anon` role, so each call errors and
    the seeded score (50) is untouched.
    """
    _uid_a, _uid_b, job_id, target_a, _target_b, analysis_id = seeded_for_blend

    with pytest.raises(APIError):
        anon_client.rpc(
            "user_apply_score_blend",
            {
                "p_job_posting_id": job_id,
                "p_target_id": target_a,
                "p_score": 99,
                "p_analysis_id": analysis_id,
            },
        ).execute()

    with pytest.raises(APIError):
        anon_client.rpc(
            "user_upsert_score", {"p_row": _row(job_id, target_a, 99)}
        ).execute()

    with pytest.raises(APIError):
        anon_client.rpc(
            "user_set_scores_included",
            {"p_job_posting_id": job_id, "p_target_ids": [target_a]},
        ).execute()

    # None of the rejected calls touched the shared score.
    assert _score(service_client, job_id, target_a) == 50
