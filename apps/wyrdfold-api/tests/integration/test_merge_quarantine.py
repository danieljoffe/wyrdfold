"""Live-stack gate for #191 slice 1b — the reference-JD merge quarantine.

Proves against a real Postgres: the `apply_target_profile_merge` RPC's
guards bite (non-follower refused, stale version refused, extras written /
COALESCE-kept), and the staged-merge lifecycle works end-to-end — a
quarantined (suppressed-at-birth) contribution stays out of the shared
profile until `apply_staged_patch` approves it (unsuppress + fresh re-merge
through the RPC), while `reject_staged_patch` leaves the quarantine intact.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from supabase import AsyncClient, Client

from app.models.learning import ProfilePatch
from app.services.llm_learner import apply_staged_patch, reject_staged_patch

pytestmark = pytest.mark.integration

_BASE_PROFILE = {
    "categories": {"core_skills": {"keywords": {"python": 3}, "weight": 2.0}},
    "negative": {"keywords": [], "weight": -10.0},
}
# What the quarantined contribution would merge in.
_CONTRIB_PROFILE = {
    "categories": {"core_skills": {"keywords": {"golang": 3}, "weight": 2.0}},
    "negative": {"keywords": [], "weight": -10.0},
}
_MERGED_NEXT = {
    "categories": {"core_skills": {"keywords": {"python": 3, "golang": 3}, "weight": 2.0}},
    "negative": {"keywords": [], "weight": -10.0},
}


@pytest.fixture
def seeded(service_client: Client, two_seeded_users: tuple[str, str]) -> Iterator[dict[str, Any]]:
    """A target followed by user A, one live ref JD (user A) and one
    QUARANTINED ref JD (suppressed-at-birth) + its staged merge log row."""
    uid_a, uid_b = two_seeded_users
    tag = uuid.uuid4().hex[:8]
    target_id: str = (
        service_client.table("targets")
        .insert(
            {
                "label": f"Quarantine {tag}",
                "scoring_profile": _BASE_PROFILE,
                "search_keywords": ["python engineer"],
                "profile_version": 1,
            }
        )
        .execute()
        .data[0]["id"]
    )
    service_client.table("user_targets").insert(
        {"user_id": uid_a, "target_id": target_id}
    ).execute()
    live_jd_id: str = (
        service_client.table("reference_jds")
        .insert(
            {
                "target_id": target_id,
                "user_id": uid_a,
                "jd_text": "python " * 20,
                "extracted_profile": _BASE_PROFILE,
            }
        )
        .execute()
        .data[0]["id"]
    )
    quarantined_jd_id: str = (
        service_client.table("reference_jds")
        .insert(
            {
                "target_id": target_id,
                "user_id": uid_a,
                "jd_text": "golang " * 20,
                "extracted_profile": _CONTRIB_PROFILE,
                "suppressed": True,
            }
        )
        .execute()
        .data[0]["id"]
    )
    note = "[staged merge: over the cap]"
    staged_run_id: str = (
        service_client.table("target_learning_log")
        .insert(
            {
                "user_id": uid_a,
                "target_id": target_id,
                "status": "staged",
                "kind": "merge",
                "prev_profile": _BASE_PROFILE,
                "next_profile": _MERGED_NEXT,
                "diff": ProfilePatch(confidence=0.0, rationale=note).model_dump(mode="json"),
                "confidence": 0.0,
                "rationale": note,
                "signals_consumed": 0,
                "merge_payload": {
                    "ref_jd_id": quarantined_jd_id,
                    "search_keywords": ["golang engineer"],
                    "example_promising_titles": ["Golang Engineer"],
                    "example_unpromising_titles": ["Sales Rep"],
                },
            }
        )
        .execute()
        .data[0]["id"]
    )
    ctx = {
        "uid_a": uid_a,
        "uid_b": uid_b,
        "target_id": target_id,
        "live_jd_id": live_jd_id,
        "quarantined_jd_id": quarantined_jd_id,
        "staged_run_id": staged_run_id,
    }
    try:
        yield ctx
    finally:
        service_client.table("target_learning_log").delete().eq("target_id", target_id).execute()
        service_client.table("reference_jds").delete().eq("target_id", target_id).execute()
        service_client.table("user_targets").delete().eq("target_id", target_id).execute()
        service_client.table("targets").delete().eq("id", target_id).execute()


def _target(client: Client, target_id: str) -> dict[str, Any]:
    return (
        client.table("targets")
        .select("scoring_profile,profile_version,search_keywords")
        .eq("id", target_id)
        .single()
        .execute()
        .data
    )


def _jd_suppressed(client: Client, jd_id: str) -> bool:
    return bool(
        client.table("reference_jds")
        .select("suppressed")
        .eq("id", jd_id)
        .single()
        .execute()
        .data["suppressed"]
    )


# ---- merge RPC guards -------------------------------------------------------


def test_merge_rpc_writes_profile_and_extras(
    service_client: Client, seeded: dict[str, Any]
) -> None:
    rows = (
        service_client.rpc(
            "apply_target_profile_merge",
            {
                "p_user_id": seeded["uid_a"],
                "p_target_id": seeded["target_id"],
                "p_next_profile": _MERGED_NEXT,
                "p_expected_version": 1,
                "p_search_keywords": ["golang engineer"],
                "p_example_promising": ["Golang Engineer"],
                "p_example_unpromising": None,
            },
        )
        .execute()
        .data
    )
    assert rows == [{"outcome": "applied", "new_version": 2}]
    state = _target(service_client, seeded["target_id"])
    assert state["profile_version"] == 2
    assert "golang" in state["scoring_profile"]["categories"]["core_skills"]["keywords"]
    # Non-NULL extras written; NULL extras left alone (COALESCE).
    assert state["search_keywords"] == ["golang engineer"]


def test_merge_rpc_refuses_non_follower_and_stale_version(
    service_client: Client, seeded: dict[str, Any]
) -> None:
    for params, expected in [
        ({"p_user_id": seeded["uid_b"], "p_expected_version": 1}, "not_a_follower"),
        ({"p_user_id": seeded["uid_a"], "p_expected_version": 99}, "version_conflict"),
    ]:
        rows = (
            service_client.rpc(
                "apply_target_profile_merge",
                {
                    "p_target_id": seeded["target_id"],
                    "p_next_profile": _MERGED_NEXT,
                    **params,
                },
            )
            .execute()
            .data
        )
        assert rows[0]["outcome"] == expected
    state = _target(service_client, seeded["target_id"])
    assert state["profile_version"] == 1
    assert state["search_keywords"] == ["python engineer"]


def test_merge_rpc_not_executable_by_anon(anon_client: Client, seeded: dict[str, Any]) -> None:
    with pytest.raises(Exception) as err:
        anon_client.rpc(
            "apply_target_profile_merge",
            {
                "p_user_id": seeded["uid_a"],
                "p_target_id": seeded["target_id"],
                "p_next_profile": _MERGED_NEXT,
                "p_expected_version": 1,
            },
        ).execute()
    assert "42501" in str(err.value) or "permission denied" in str(err.value)


# ---- staged-merge lifecycle -------------------------------------------------


async def test_apply_staged_merge_lifts_quarantine_and_remerges(
    service_client: Client, async_service_client: AsyncClient, seeded: dict[str, Any]
) -> None:
    result = await apply_staged_patch(
        async_service_client, user_id=seeded["uid_a"], run_id=seeded["staged_run_id"]
    )

    assert result is not None
    assert result.applied is True
    assert result.profile_version_after == 2
    # The contribution now counts: merged profile includes golang, the JD is
    # unsuppressed, and the merge_payload extras were written.
    state = _target(service_client, seeded["target_id"])
    assert "golang" in state["scoring_profile"]["categories"]["core_skills"]["keywords"]
    assert state["search_keywords"] == ["golang engineer"]
    assert _jd_suppressed(service_client, seeded["quarantined_jd_id"]) is False
    # Log row flipped with the actual transition recorded.
    log = (
        service_client.table("target_learning_log")
        .select("status,prev_profile,applied_run_id")
        .eq("id", seeded["staged_run_id"])
        .single()
        .execute()
        .data
    )
    assert log["status"] == "applied"
    assert log["applied_run_id"] is not None
    assert log["prev_profile"] == _BASE_PROFILE


async def test_reject_staged_merge_keeps_quarantine(
    service_client: Client, async_service_client: AsyncClient, seeded: dict[str, Any]
) -> None:
    result = await reject_staged_patch(
        async_service_client, user_id=seeded["uid_a"], run_id=seeded["staged_run_id"]
    )

    assert result is not None
    assert result.applied is False
    # Profile untouched, contribution still suppressed — permanently out of
    # every merge unless the vote quorum later rescues it.
    state = _target(service_client, seeded["target_id"])
    assert "golang" not in state["scoring_profile"]["categories"]["core_skills"]["keywords"]
    assert state["profile_version"] == 1
    assert _jd_suppressed(service_client, seeded["quarantined_jd_id"]) is True


async def test_apply_staged_merge_gone_contribution_is_none(
    service_client: Client, async_service_client: AsyncClient, seeded: dict[str, Any]
) -> None:
    """The quarantined JD was deleted while staged — nothing to approve."""
    service_client.table("reference_jds").delete().eq("id", seeded["quarantined_jd_id"]).execute()

    result = await apply_staged_patch(
        async_service_client, user_id=seeded["uid_a"], run_id=seeded["staged_run_id"]
    )

    assert result is None
    state = _target(service_client, seeded["target_id"])
    assert state["profile_version"] == 1
