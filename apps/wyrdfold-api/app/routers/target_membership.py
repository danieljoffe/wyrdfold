"""Target-membership for the search grid (#467 §11).

For the caller's OWN targets, which of a batch of search listings are already in
each? Powers the card's pipeline-state badge ("✓ In 'Frontend Roles'") and the
detail's bound/unbound split (a bound listing unlocks match/tailor).

MEMBERSHIP means "in the target's pipeline", which requires more than a bare
``scores`` row: the row must be non-excluded AND ``promising`` (the stage-1
admit — a scores row alone only proves the pair was once *evaluated*), and the
job's ``role_family`` must pass the #277 family gate for that target. Before
those two conditions were enforced here, any evaluated pair badged — off-family
listings ("Customer Support Specialist") rendered as In "Senior Frontend
Engineer" on /search. Family semantics are the shared
``services.qualification.family_gate`` predicate; the job family comes from the
trigger-synced ``scores.job_role_family`` denorm
(``20260717040000_scores_denorm_live_family.sql``), so no ``jobs`` join.

AUTHZ — the sensitive bit (kept deliberately narrow): the ``target_id`` filter on
the ``scores`` read is derived ONLY from the caller's own ``user_targets`` (scoped
by the authenticated ``user_id`` AND RLS-backstopped on the caller's client). A
caller can therefore never surface another user's target-membership: the read is
bounded to target ids they own, and target ids are never taken from request input.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from supabase import AsyncClient

from app.dependencies import (
    get_async_user_supabase,
    get_current_user_id,
    verify_api_key_or_jwt,
)
from app.services.qualification.family_gate import passes_family_gate

logger = logging.getLogger(__name__)

router = APIRouter(
    tags=["target-membership"],
    dependencies=[Depends(verify_api_key_or_jwt)],
)

# A search page is <=25; cap generously but bound the IN() query (#414 guard).
_MAX_IDS = 100


class TargetMembershipRequest(BaseModel):
    job_posting_ids: list[str] = Field(..., max_length=_MAX_IDS)


class TargetRef(BaseModel):
    target_id: str
    label: str


class TargetMembershipResponse(BaseModel):
    """``job_posting_id`` → the caller's targets that already contain it (empty
    map value / absent key = not in any of the caller's targets)."""

    memberships: dict[str, list[TargetRef]]


async def _membership(
    supabase: AsyncClient, user_id: str, job_posting_ids: list[str]
) -> dict[str, list[TargetRef]]:
    ids = list(dict.fromkeys(job_posting_ids))  # dedup, preserve order
    if not ids:
        return {}

    # The caller's OWN targets — scoped by user_id (+ RLS on the caller's client).
    # This is the ONLY source of the target ids the scores read is filtered by.
    ut_rows = cast(
        list[dict[str, Any]],
        (
            await supabase.table("user_targets")
            .select("target_id")
            .eq("user_id", user_id)
            .execute()
        ).data
        or [],
    )
    target_ids = [r["target_id"] for r in ut_rows]
    if not target_ids:
        return {}

    label_rows = cast(
        list[dict[str, Any]],
        (
            await supabase.table("targets")
            .select("id, label, role_family")
            .in_("id", target_ids)
            .execute()
        ).data
        or [],
    )
    label_by_id = {r["id"]: r.get("label") or "" for r in label_rows}
    family_by_id = {r["id"]: r.get("role_family") for r in label_rows}

    # Scores read: bounded to (the page's job ids) x (the caller's target ids).
    # ``promising IS TRUE`` in SQL (NULL = never stage-1-admitted → not a
    # member); the family gate is relational, so it filters below in Python.
    score_rows = cast(
        list[dict[str, Any]],
        (
            await supabase.table("scores")
            .select("job_posting_id, target_id, job_role_family")
            .in_("job_posting_id", ids)
            .in_("target_id", target_ids)
            .eq("excluded", False)
            .eq("promising", True)
            .execute()
        ).data
        or [],
    )
    out: dict[str, list[TargetRef]] = {}
    for r in score_rows:
        tid = r["target_id"]
        if not passes_family_gate(family_by_id.get(tid), r.get("job_role_family")):
            continue
        out.setdefault(r["job_posting_id"], []).append(
            TargetRef(target_id=tid, label=label_by_id.get(tid, ""))
        )
    return out


@router.post("/jobs/target-membership", response_model=TargetMembershipResponse)
async def target_membership(
    body: TargetMembershipRequest,
    user_id: str = Depends(get_current_user_id),
    supabase: AsyncClient = Depends(get_async_user_supabase),
) -> TargetMembershipResponse:
    """Which of the caller's targets already contain each listing (batch).

    Uses the caller's RLS-scoped client; the scores read is filtered to the
    caller's own target ids (see the module docstring). The three DB reads
    await natively on the pooled async user client (#57 slice 4).
    """
    memberships = await _membership(supabase, user_id, body.job_posting_ids)
    return TargetMembershipResponse(memberships=memberships)
