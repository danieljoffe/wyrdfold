"""SEC-H2 (audit 2026-07-18): the user_* score-write SECURITY DEFINER RPCs
must NOT be executable by `authenticated` — they write the shared `scores`
catalog and are now service_role-only. An authenticated user hitting PostgREST
directly (browser anon key) gets permission denied.

Requires migration 20260718010000. Runs against the local Supabase stack.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest
from supabase import Client

pytestmark = pytest.mark.integration


def _is_permission_denied(err: pytest.ExceptionInfo[Exception]) -> bool:
    s = str(err.value).lower()
    return "42501" in s or "permission denied" in s


def test_score_write_rpcs_locked_to_service_role(
    service_client: Client,
    user_client_factory: Callable[[str], Client],
    two_seeded_users: tuple[str, str],
) -> None:
    uid, _other = two_seeded_users
    user = user_client_factory(uid)
    jid, aid = str(uuid.uuid4()), str(uuid.uuid4())

    # Seed a target the user ACTUALLY FOLLOWS, so the RPCs' in-DB follower
    # check would PASS — the only thing that can block the call is the revoked
    # EXECUTE grant. (Without this, an unfollowed target would raise the
    # follower check's own 42501 and the test would false-pass on a re-grant.)
    tid = (
        service_client.table("targets")
        .insert({"label": f"Lockdown {uid[:8]}"})
        .execute()
        .data[0]["id"]
    )
    service_client.table("user_targets").insert(
        {"user_id": uid, "target_id": tid, "is_active": True}
    ).execute()

    try:
        with pytest.raises(Exception) as e1:
            user.rpc(
                "user_upsert_score",
                {
                    "p_row": {
                        "job_posting_id": jid,
                        "target_id": tid,
                        "score": 0,
                        "excluded": True,
                        "scoring_status": "stage2",
                        "scored_profile_version": 1,
                        "recency_score": 0,
                        "updated_at": "2026-07-18T00:00:00+00:00",
                        "score_breakdown": {},
                    }
                },
            ).execute()
        assert _is_permission_denied(e1), f"user_upsert_score: {e1.value}"

        with pytest.raises(Exception) as e2:
            user.rpc(
                "user_apply_score_blend",
                {
                    "p_job_posting_id": jid,
                    "p_target_id": tid,
                    "p_score": 50,
                    "p_analysis_id": aid,
                },
            ).execute()
        assert _is_permission_denied(e2), f"user_apply_score_blend: {e2.value}"

        with pytest.raises(Exception) as e3:
            user.rpc(
                "user_set_scores_included",
                {"p_job_posting_id": jid, "p_target_ids": [tid]},
            ).execute()
        assert _is_permission_denied(e3), f"user_set_scores_included: {e3.value}"
    finally:
        service_client.table("targets").delete().eq("id", tid).execute()
