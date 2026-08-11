"""Feedback endpoints — per-(user, target) signal capture + learner triggers.

Routes:
- ``POST   /jobs/{job_id}/feedback`` — upsert a signal
- ``DELETE /jobs/{job_id}/feedback`` — undo (used by toast Undo)
- ``GET    /targets/{target_id}/feedback`` — list a user's rows
- ``POST   /targets/{target_id}/learn`` — deterministic learner (v1)
- ``POST   /targets/{target_id}/learn-llm`` — LLM ProfilePatch learner (v2)
- ``GET    /targets/{target_id}/learning-log`` — audit list for settings UI
- ``POST   /targets/{target_id}/learn/{run_id}/apply`` — accept a staged patch
- ``POST   /targets/{target_id}/learn/{run_id}/reject`` — reject a staged patch

Auth: JWT (``get_current_user_id``). API-key only callers are not
expected here — feedback is fundamentally per-user.
"""

from __future__ import annotations

from typing import Any, cast

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
)
from supabase import AsyncClient

from app.background import spawn_detached
from app.dependencies import (
    enforce_llm_budget,
    get_async_service_supabase,
    get_async_user_supabase,
    get_current_user_id,
    get_llm_client,
)
from app.models.feedback import (
    FeedbackCreate,
    FeedbackCreateResponse,
    FeedbackList,
    LearnerPatchSummary,
)
from app.models.learning import LearningRunResult, TargetLearningLogRow
from app.rate_limit import limiter
from app.services.feedback import (
    delete_feedback,
    list_for_target,
    run_learner_and_rescore_off_loop,
    run_learner_off_loop,
    upsert_feedback,
)
from app.services.llm.client import LLMClient
from app.services.llm_learner import (
    StagedPatchConflictError,
    apply_staged_patch_off_loop,
    reject_staged_patch_off_loop,
    run_llm_learner_off_loop,
)

router = APIRouter(tags=["feedback"])


async def _job_exists(supabase: AsyncClient, job_id: str) -> bool:
    resp = await supabase.table("jobs").select("id").eq("id", job_id).execute()
    return bool(resp.data)


async def _target_exists_for_user(supabase: AsyncClient, user_id: str, target_id: str) -> bool:
    resp = (
        await supabase.table("user_targets")
        .select("target_id")
        .eq("user_id", user_id)
        .eq("target_id", target_id)
        .execute()
    )
    return bool(resp.data)


async def _learning_log_rows(
    supabase: AsyncClient,
    *,
    user_id: str,
    target_id: str,
    status: str | None,
    limit: int,
) -> list[TargetLearningLogRow]:
    """Async read of the caller's ``target_learning_log`` rows for one target.

    Extracted from the handler so the blocking ``.execute()`` never sits inline
    in an ``async def`` route body (the #107 guard scans handler bodies). Runs on
    the caller's RLS-bound client — the ``.eq("user_id")`` filter stays as
    belt-and-suspenders + 404 UX."""
    query = (
        supabase.table("target_learning_log")
        .select("*")
        .eq("user_id", user_id)
        .eq("target_id", target_id)
        .order("created_at", desc=True)
        .limit(limit)
    )
    if status:
        query = query.eq("status", status)
    resp = await query.execute()
    rows = cast(list[dict[str, Any]], resp.data or [])
    return [TargetLearningLogRow.model_validate(r) for r in rows]


# `async def` (#57 slice 4): the foreground reads + the job_feedback upsert run
# natively on the pooled async user client, off the threadpool.
@router.post("/jobs/{job_id}/feedback", response_model=FeedbackCreateResponse)
async def create_feedback(
    job_id: str,
    body: FeedbackCreate,
    user_id: str = Depends(get_current_user_id),
    # #79 R1: the foreground reads + the job_feedback upsert run through the
    # caller's JWT so RLS is the backstop (job_feedback self-CRUD, user_targets
    # self). The background learner keeps the service-role client — it writes
    # the shared `targets` catalog and re-scores, which RLS denies to a user
    # client.
    supabase: AsyncClient = Depends(get_async_user_supabase),
) -> FeedbackCreateResponse:
    if not await _job_exists(supabase, job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    if not await _target_exists_for_user(supabase, user_id, body.target_id):
        # Don't leak whether the target exists for someone else — same 404.
        raise HTTPException(status_code=404, detail="Target not found for user")

    row = await upsert_feedback(
        supabase,
        user_id=user_id,
        job_posting_id=job_id,
        target_id=body.target_id,
        signal=body.signal,
        reason=body.reason,
    )

    # Only ``irrelevant`` signals feed the v1 learner — positive feedback
    # routes to ``secondary_skills`` in v2 (LLM only, since the heuristic
    # for "which positive token to weight" is harder). The learner writes the
    # shared `targets` catalog + re-scores under service-role, so it runs off
    # the event loop as a detached task — NEVER a starlette BackgroundTask,
    # which deadlocks the pooled async client under uvloop (app/background.py).
    queued = body.signal == "irrelevant"
    if queued:
        spawn_detached(
            run_learner_and_rescore_off_loop(user_id=user_id, target_id=body.target_id),
            name=f"feedback-learner:{body.target_id}",
        )

    return FeedbackCreateResponse(feedback=row, queued_learn_run=queued)


# `async def` (#57 slice 4): the delete runs natively on the async user client.
@router.delete("/jobs/{job_id}/feedback", status_code=204)
async def remove_feedback(
    job_id: str,
    target_id: str = Query(...),
    user_id: str = Depends(get_current_user_id),
    # #79 R1: delete through the caller's JWT (job_feedback self-DELETE RLS).
    supabase: AsyncClient = Depends(get_async_user_supabase),
) -> None:
    await delete_feedback(
        supabase,
        user_id=user_id,
        job_posting_id=job_id,
        target_id=target_id,
    )


# `async def` (#57 slice 4): the reads run natively on the async user client.
@router.get("/targets/{target_id}/feedback", response_model=FeedbackList)
async def list_feedback(
    target_id: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user_id: str = Depends(get_current_user_id),
    # #79 Phase 2 (job_feedback slice): read through the caller's JWT so
    # Postgres RLS (job_feedback_self_select) is the backstop. The user_id
    # filter in list_for_target stays as belt-and-suspenders + 404 UX.
    supabase: AsyncClient = Depends(get_async_user_supabase),
) -> FeedbackList:
    if not await _target_exists_for_user(supabase, user_id, target_id):
        raise HTTPException(status_code=404, detail="Target not found for user")
    rows, total = await list_for_target(
        supabase,
        user_id=user_id,
        target_id=target_id,
        limit=limit,
        offset=offset,
    )
    return FeedbackList(rows=rows, total=total)


# `async def` (#57 slice 4): the ownership precheck runs on the async service
# client; the deterministic learner (shared #191 profile-write RPC, service-role)
# stays sync and is driven off the loop via ``asyncio.to_thread``.
@router.post(
    "/targets/{target_id}/learn",
    response_model=LearnerPatchSummary | None,
)
# #191: the learner mutates the SHARED target profile — throttle the
# contribution surface per user so it can't be spammed across targets.
@limiter.limit("10/minute")
async def run_learner_now(
    request: Request,
    target_id: str,
    user_id: str = Depends(get_current_user_id),
    supabase: AsyncClient = Depends(get_async_service_supabase),
) -> Any:
    if not await _target_exists_for_user(supabase, user_id, target_id):
        raise HTTPException(status_code=404, detail="Target not found for user")
    return await run_learner_off_loop(user_id=user_id, target_id=target_id)


@router.post(
    "/targets/{target_id}/learn-llm",
    response_model=LearningRunResult | None,
    dependencies=[Depends(enforce_llm_budget)],
)
# #191 + LLM-backed: tighter than the deterministic learner.
@limiter.limit("5/minute")
async def run_llm_learner_now(
    request: Request,
    target_id: str,
    user_id: str = Depends(get_current_user_id),
    supabase: AsyncClient = Depends(get_async_service_supabase),
    llm: LLMClient = Depends(get_llm_client),
) -> Any:
    """Force-run the LLM ProfilePatch learner.

    Returns ``None`` if there's nothing to learn from (below threshold).
    Returns a result with ``applied=True`` when the patch was auto-applied
    (confidence ≥ 0.6), or ``applied=False`` when it was staged for review.
    """
    # The ownership precheck runs on the async service client; the learner chain
    # itself stays sync-client (it awaits the LLM but its DB work leans on the
    # shared #191 RPC + the sync re-score projection), driven off the loop by the
    # ``run_llm_learner_off_loop`` seam on the pooled sync service client.
    if not await _target_exists_for_user(supabase, user_id, target_id):
        raise HTTPException(status_code=404, detail="Target not found for user")
    return await run_llm_learner_off_loop(llm, user_id=user_id, target_id=target_id)


# `async def` (#57 slice 4): the read runs natively on the async user client
# via the ``_learning_log_rows`` helper (kept out of the handler body so the
# blocking-call guard has no inline ``.execute()`` to flag).
@router.get(
    "/targets/{target_id}/learning-log",
    response_model=list[TargetLearningLogRow],
)
async def list_learning_log(
    target_id: str,
    status: str | None = Query(default=None, pattern="^(applied|staged|rejected)$"),
    limit: int = Query(50, ge=1, le=200),
    user_id: str = Depends(get_current_user_id),
    # #79 R1: read through the caller's JWT (target_learning_log self-SELECT
    # RLS). The .eq("user_id") filter stays as belt-and-suspenders + 404 UX.
    supabase: AsyncClient = Depends(get_async_user_supabase),
) -> list[TargetLearningLogRow]:
    if not await _target_exists_for_user(supabase, user_id, target_id):
        raise HTTPException(status_code=404, detail="Target not found for user")
    return await _learning_log_rows(
        supabase, user_id=user_id, target_id=target_id, status=status, limit=limit
    )


# `async def` (#57 slice 4): precheck on the async service client; the staged
# apply (shared #191 RPC, service-role) stays sync, driven off the loop via
# ``asyncio.to_thread``.
@router.post(
    "/targets/{target_id}/learn/{run_id}/apply",
    response_model=LearningRunResult,
)
@limiter.limit("20/minute")  # #191: shared-profile write surface
async def apply_learning_run(
    request: Request,
    target_id: str,
    run_id: str,
    user_id: str = Depends(get_current_user_id),
    supabase: AsyncClient = Depends(get_async_service_supabase),
) -> Any:
    if not await _target_exists_for_user(supabase, user_id, target_id):
        raise HTTPException(status_code=404, detail="Target not found for user")
    try:
        result = await apply_staged_patch_off_loop(user_id=user_id, run_id=run_id)
    except StagedPatchConflictError as e:
        raise HTTPException(
            status_code=409,
            detail=(
                "The target profile changed while applying this patch — "
                "re-review it against the current profile and retry."
            ),
        ) from e
    if result is None:
        raise HTTPException(
            status_code=404,
            detail="No staged patch with that run_id for this user",
        )
    return result


# `async def` (#57 slice 4): precheck on the async service client; the staged
# reject (service-role) stays sync, driven off the loop via ``asyncio.to_thread``.
@router.post(
    "/targets/{target_id}/learn/{run_id}/reject",
    response_model=LearningRunResult,
)
@limiter.limit("20/minute")  # #191: shared-profile write surface
async def reject_learning_run(
    request: Request,
    target_id: str,
    run_id: str,
    user_id: str = Depends(get_current_user_id),
    supabase: AsyncClient = Depends(get_async_service_supabase),
) -> Any:
    if not await _target_exists_for_user(supabase, user_id, target_id):
        raise HTTPException(status_code=404, detail="Target not found for user")
    result = await reject_staged_patch_off_loop(user_id=user_id, run_id=run_id)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail="No staged patch with that run_id for this user",
        )
    return result
