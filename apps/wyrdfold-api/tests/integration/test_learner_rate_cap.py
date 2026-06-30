"""End-to-end gate for the learner's re-score projection / learning-rate cap
(#5 P4), against a live Supabase stack with the LLM mocked.

Seeds a target whose recent jobs all match "python", then drives the learner
with a confident patch and asserts the projection's verdict actually governs
the outcome: a patch that would hard-exclude the whole list is STAGED (target
untouched, feedback preserved, projection recorded), while a patch irrelevant
to the list AUTO-APPLIES. This exercises the real deterministic scorer over
real rows — what the unit tests mock out.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from supabase import Client

from app.models.learning import ProfilePatch
from app.models.llm import LLMResult, LLMUsage
from app.services.llm_learner import run_llm_learner

pytestmark = pytest.mark.integration

_N_JOBS = 12  # > learning_rescore_min_jobs (10)
_PROFILE = {
    "categories": {"core_skills": {"keywords": {"python": 3}, "weight": 2.0}},
    "seniority": {"level": None, "signals": []},
    "domain": {"signals": [], "weight": 0.5},
    "negative": {"keywords": [], "weight": -10.0},
}


def _llm_result() -> LLMResult:
    return LLMResult(
        content="{}",
        model="claude-sonnet-4-6",
        usage=LLMUsage(input_tokens=10, output_tokens=5),
        cost_usd=0.0,
        latency_ms=1,
    )


@pytest.fixture
def seeded_target(
    service_client: Client, two_seeded_users: tuple[str, str]
) -> Iterator[tuple[str, str]]:
    """A target + N python jobs + scores + 3 unapplied feedback rows.

    Yields (uid, target_id). Cleans up all seeded rows in FK-safe order.
    """
    uid, _ = two_seeded_users
    tag = uuid.uuid4().hex[:8]
    source_id: str = (
        service_client.table("sources")
        .insert({"board_token": f"p4-{tag}", "company_name": "P4 Co"})
        .execute()
        .data[0]["id"]
    )
    target_id: str = (
        service_client.table("targets")
        .insert(
            {
                "label": f"P4 Python {tag}",
                "scoring_profile": _PROFILE,
                "search_keywords": ["python engineer"],
                "profile_version": 1,
            }
        )
        .execute()
        .data[0]["id"]
    )
    job_ids: list[str] = []
    for i in range(_N_JOBS):
        jid = (
            service_client.table("jobs")
            .insert(
                {
                    "external_id": f"p4-{tag}-{i}",
                    "source_id": source_id,
                    # "Contract" is a non-target title word the learner can
                    # legitimately negative (it doesn't collide with the
                    # target's own "python"/"python engineer" terms, #47) — so
                    # the rate-cap test below can hard-exclude via the title
                    # without tripping the self-collision guard.
                    "title": "Contract Python Engineer",
                    "company_name": "P4 Co",
                    "description_html": "<p>Contract python engineer; python, django.</p>",
                }
            )
            .execute()
            .data[0]["id"]
        )
        job_ids.append(jid)
        service_client.table("scores").insert(
            {"job_posting_id": jid, "target_id": target_id, "score": 50, "excluded": False}
        ).execute()
    # 3 unapplied feedback rows (== _MIN_FEEDBACK_FOR_LEARN) so the learner runs.
    service_client.table("job_feedback").insert(
        [
            {
                "user_id": uid,
                "target_id": target_id,
                "job_posting_id": job_ids[i],
                "signal": "irrelevant",
                "reason": "not python after all",
            }
            for i in range(3)
        ]
    ).execute()
    try:
        yield uid, target_id
    finally:
        service_client.table("target_learning_log").delete().eq(
            "target_id", target_id
        ).execute()
        service_client.table("job_feedback").delete().eq("target_id", target_id).execute()
        service_client.table("scores").delete().eq("target_id", target_id).execute()
        service_client.table("jobs").delete().eq("source_id", source_id).execute()
        service_client.table("targets").delete().eq("id", target_id).execute()
        service_client.table("sources").delete().eq("id", source_id).execute()


def _profile_version(client: Client, target_id: str) -> int:
    row = (
        client.table("targets")
        .select("profile_version")
        .eq("id", target_id)
        .single()
        .execute()
        .data
    )
    return int(row["profile_version"])


def _unapplied_feedback_count(client: Client, target_id: str) -> int:
    rows = (
        client.table("job_feedback")
        .select("id")
        .eq("target_id", target_id)
        .is_("applied_at", "null")
        .execute()
        .data
    )
    return len(rows or [])


@pytest.mark.asyncio
async def test_outlier_patch_is_staged_not_applied(
    service_client: Client, seeded_target: tuple[str, str]
) -> None:
    uid, target_id = seeded_target
    # "contract" hard-excludes every seeded job (it's in each title) WITHOUT
    # colliding with the target's own python terms, so it survives the
    # self-collision guard and reaches the learning-rate cap (#47).
    patch_obj = ProfilePatch(
        add_negative=["contract"], confidence=0.95, rationale="all irrelevant"
    )
    with patch(
        "app.services.llm_learner.complete_json",
        return_value=(patch_obj, _llm_result()),
    ):
        result = await run_llm_learner(
            service_client, object(), user_id=uid, target_id=target_id  # type: ignore[arg-type]
        )

    assert result is not None
    assert result.applied is False  # staged by the learning-rate cap
    # Target untouched, feedback preserved for a later (reviewed) run.
    assert _profile_version(service_client, target_id) == 1
    assert _unapplied_feedback_count(service_client, target_id) == 3
    # A staged log row whose projection says "capped".
    log = (
        service_client.table("target_learning_log")
        .select("status, projection")
        .eq("target_id", target_id)
        .single()
        .execute()
        .data
    )
    assert log["status"] == "staged"
    assert log["projection"]["capped"] is True
    assert log["projection"]["jobs_moved"] == _N_JOBS


@pytest.mark.asyncio
async def test_self_colliding_negative_is_dropped_before_apply(
    service_client: Client, seeded_target: tuple[str, str]
) -> None:
    uid, target_id = seeded_target
    # "python" is the target's own core skill AND a token of its "python
    # engineer" search keyword — adding it as a negative would hard-zero every
    # legitimate job. The guard drops it before apply, so nothing self-harming
    # lands on the shared profile (#47).
    patch_obj = ProfilePatch(
        add_negative=["python"], confidence=0.95, rationale="all irrelevant"
    )
    with patch(
        "app.services.llm_learner.complete_json",
        return_value=(patch_obj, _llm_result()),
    ):
        result = await run_llm_learner(
            service_client, object(), user_id=uid, target_id=target_id  # type: ignore[arg-type]
        )

    assert result is not None
    profile = (
        service_client.table("targets")
        .select("scoring_profile")
        .eq("id", target_id)
        .single()
        .execute()
        .data["scoring_profile"]
    )
    # The self-colliding negative never reached the shared profile.
    assert profile["negative"]["keywords"] == []


@pytest.mark.asyncio
async def test_irrelevant_patch_auto_applies(
    service_client: Client, seeded_target: tuple[str, str]
) -> None:
    uid, target_id = seeded_target
    # "blockchain" matches none of the python jobs -> no churn -> applies.
    patch_obj = ProfilePatch(
        add_negative=["blockchain"], confidence=0.95, rationale="filter blockchain"
    )
    with patch(
        "app.services.llm_learner.complete_json",
        return_value=(patch_obj, _llm_result()),
    ):
        result = await run_llm_learner(
            service_client, object(), user_id=uid, target_id=target_id  # type: ignore[arg-type]
        )

    assert result is not None
    assert result.applied is True
    assert _profile_version(service_client, target_id) == 2  # bumped
    assert _unapplied_feedback_count(service_client, target_id) == 0  # consumed
    log = (
        service_client.table("target_learning_log")
        .select("status, projection")
        .eq("target_id", target_id)
        .single()
        .execute()
        .data
    )
    assert log["status"] == "applied"
    assert log["projection"]["capped"] is False
