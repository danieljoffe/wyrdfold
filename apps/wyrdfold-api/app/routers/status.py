from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException
from supabase import Client

from app.cache import job_list_cache, jobs_cache_prefix
from app.dependencies import (
    get_current_user_id,
    get_user_supabase,
    verify_supabase_jwt,
)
from app.models.schemas import StatusUpdate
from app.services.tailor import persistence

# `verify_supabase_jwt` (not `_or_jwt`): status mutations are user actions,
# never invoked by cron. Keeping the api-key fallback would let a leaked
# operator key alter any user's job status.
router = APIRouter(
    prefix="/jobs",
    tags=["status"],
    dependencies=[Depends(verify_supabase_jwt)],
)


def _assert_user_owns_posting(
    supabase: Client, posting_id: str, user_id: str
) -> dict[str, Any]:
    """Return ``{status, target_id}`` for the posting only if the caller is
    linked (via ``user_targets``) to at least one target that has scored
    this posting. 404 on either missing or unowned — don't leak existence
    of postings outside the user's targets.

    Ownership is derived through the ``scores`` table (the poller writes
    ``scores`` rows keyed by ``(job_posting_id, target_id)``). The
    ``jobs.target_id`` column is **not** populated by the poller — it's
    a vestigial pre-shared-targets column — so checking it as the source
    of truth always 404s on real postings.
    """
    # 1. Confirm the posting exists. ``jobs.status`` was dropped in #75 C4
    # (per-user status now lives in ``user_jobs``); select ``id`` purely as
    # an existence probe.
    posting_resp = (
        supabase.table("jobs")
        .select("id")
        .eq("id", posting_id)
        .single()
        .execute()
    )
    if not posting_resp.data:
        raise HTTPException(status_code=404, detail="Posting not found")
    posting = cast(dict[str, Any], posting_resp.data)

    # 2. Get the caller's active+inactive target ids (auth boundary, not a
    # filter — the user can act on jobs even from a deactivated target).
    user_targets_resp = (
        supabase.table("user_targets")
        .select("target_id")
        .eq("user_id", user_id)
        .execute()
    )
    user_target_ids = {
        cast(dict[str, Any], r)["target_id"]
        for r in user_targets_resp.data or []
    }
    if not user_target_ids:
        raise HTTPException(status_code=404, detail="Posting not found")

    # 3. Confirm at least one of the user's targets has a score row for
    # this posting. If so, the user is allowed to mutate its status.
    score_resp = (
        supabase.table("scores")
        .select("target_id")
        .eq("job_posting_id", posting_id)
        .in_("target_id", list(user_target_ids))
        .limit(1)
        .execute()
    )
    rows = cast(list[dict[str, Any]], score_resp.data or [])
    if not rows:
        raise HTTPException(status_code=404, detail="Posting not found")

    # Surface the owning target_id so callers can scope cache invalidation
    # to it (same shape the old query exposed via ``jobs.target_id``).
    posting["target_id"] = rows[0]["target_id"]
    return posting


# Sync `def` (not `async def`): supabase-py is synchronous, so FastAPI runs
# this in its threadpool, keeping the blocking `.execute()` round-trips off
# the event loop. See #107.
@router.get("/{posting_id}/status-history")
def get_status_history(
    posting_id: str,
    user_id: str = Depends(get_current_user_id),
    # #88 Phase 2: RLS client — status_log has a per-user SELECT policy, and
    # the ownership probe only reads shared-catalog tables (SELECT true).
    supabase: Client = Depends(get_user_supabase),
) -> dict[str, Any]:
    _assert_user_owns_posting(supabase, posting_id, user_id)
    # Scope to the caller's own transitions (#113): a posting is shared catalog,
    # so two users targeting the same job both "own" it — without the user_id
    # filter each would see the other's pipeline actions in the history.
    result = (
        supabase.table("status_log")
        .select("id, old_status, new_status, note, created_at")
        .eq("posting_id", posting_id)
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .limit(50)
        .execute()
    )
    return {"entries": result.data or []}


# Sync `def` (not `async def`): supabase-py is synchronous, so FastAPI runs
# this in its threadpool, keeping the blocking `.execute()` round-trips off
# the event loop. See #107.
@router.post("/{posting_id}/status")
def update_status(
    posting_id: str,
    body: StatusUpdate,
    user_id: str = Depends(get_current_user_id),
    # #88 Phase 2: RLS client — user_jobs has a full CRUD self-policy and
    # status_log gained a self-INSERT policy (20260702100000), so RLS pins
    # both writes to the caller underneath the app-layer user_id values.
    supabase: Client = Depends(get_user_supabase),
) -> dict[str, Any]:
    posting = _assert_user_owns_posting(supabase, posting_id, user_id)
    target_id = posting["target_id"]

    # ``jobs.status`` was dropped in #75 C4 — the prior status for the audit
    # log is the caller's own per-user state in ``user_jobs`` (absent = 'new').
    old_status_resp = (
        supabase.table("user_jobs")
        .select("status")
        .eq("user_id", user_id)
        .eq("job_posting_id", posting_id)
        .limit(1)
        .execute()
    )
    old_status_rows = cast(list[dict[str, Any]], old_status_resp.data or [])
    old_status = (
        cast(str, old_status_rows[0]["status"]) if old_status_rows else "new"
    )

    supabase.table("status_log").insert(
        {
            "posting_id": posting_id,
            "old_status": old_status,
            "new_status": body.status,
            "note": body.note,
            "user_id": user_id,
        }
    ).execute()

    # Per-user pipeline state lives in user_jobs (#75 C3): this writer no
    # longer touches the global jobs.status. The list/counts read per-user
    # status from user_jobs and gate global liveness on jobs.archived_at.
    persistence.upsert_user_job(
        supabase, user_id=user_id, job_posting_id=posting_id, status=body.status
    )

    # Scoped invalidation: a single posting status change only affects the
    # owning target's cached pages and the global view. Sibling targets'
    # cached pages stay warm.
    job_list_cache.invalidate(prefix=f"{jobs_cache_prefix(target_id=target_id)}:")
    job_list_cache.invalidate(prefix=f"{jobs_cache_prefix(target_id=None)}:")
    return {"success": True, "old_status": old_status, "new_status": body.status}
