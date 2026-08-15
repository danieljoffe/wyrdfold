from datetime import UTC, datetime
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException
from supabase import AsyncClient

from app.cache import job_list_cache, jobs_cache_prefix
from app.dependencies import (
    get_async_user_supabase,
    get_current_user_id,
    verify_supabase_jwt,
)
from app.models.schemas import StatusUpdate
from app.services.db_read import fetch_one

# `verify_supabase_jwt` (not `_or_jwt`): status mutations are user actions,
# never invoked by cron. Keeping the api-key fallback would let a leaked
# operator key alter any user's job status.
router = APIRouter(
    prefix="/jobs",
    tags=["status"],
    dependencies=[Depends(verify_supabase_jwt)],
)


async def _assert_user_owns_posting(
    supabase: AsyncClient, posting_id: str, user_id: str
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
    #
    # ``fetch_one``, not ``.single()``: the latter RAISES on zero rows, so the
    # 404 below was unreachable and an unknown posting id 500'd (see
    # app/services/db_read.py).
    posting = await fetch_one(supabase.table("jobs").select("id").eq("id", posting_id))
    if posting is None:
        raise HTTPException(status_code=404, detail="Posting not found")

    # 2. Get the caller's active+inactive target ids (auth boundary, not a
    # filter — the user can act on jobs even from a deactivated target).
    user_targets_resp = (
        await supabase.table("user_targets").select("target_id").eq("user_id", user_id).execute()
    )
    user_target_ids = {cast(dict[str, Any], r)["target_id"] for r in user_targets_resp.data or []}
    if not user_target_ids:
        raise HTTPException(status_code=404, detail="Posting not found")

    # 3. Confirm at least one of the user's targets has a score row for
    # this posting. If so, the user is allowed to mutate its status.
    score_resp = (
        await supabase.table("scores")
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


async def _upsert_user_job(
    supabase: AsyncClient, *, user_id: str, job_posting_id: str, status: str
) -> None:
    """Async inline of ``persistence.upsert_user_job`` — the per-user pipeline
    status write (``user_jobs``). ``persistence.upsert_user_job`` stays sync for
    the not-yet-converted routers (``jobs`` legacy path / ``targets.from_input``),
    so this async handler inlines the same upsert rather than fork a twin (#57
    slice 4 — mirrors ``jobs._upsert_user_job_async`` / ``persistence.
    mark_job_resume_draft``)."""
    await (
        supabase.table("user_jobs")
        .upsert(
            {
                "user_id": user_id,
                "job_posting_id": job_posting_id,
                "status": status,
                "updated_at": datetime.now(UTC).isoformat(),
            },
            on_conflict="user_id,job_posting_id",
        )
        .execute()
    )


async def _status_history_rows(
    supabase: AsyncClient, posting_id: str, user_id: str
) -> list[dict[str, Any]]:
    """The caller's own status transitions for a posting (most recent first).

    Scope to the caller's own transitions (#113): a posting is shared catalog,
    so two users targeting the same job both "own" it — without the user_id
    filter each would see the other's pipeline actions in the history.

    A module-level async helper so the handler holds no inline ``.execute()`` on
    the loop (#107 static guard bans a literal ``.execute()`` in an ``async def``
    handler body — it can't tell the async client apart statically)."""
    result = (
        await supabase.table("status_log")
        .select("id, old_status, new_status, note, created_at")
        .eq("posting_id", posting_id)
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .limit(50)
        .execute()
    )
    return cast(list[dict[str, Any]], result.data or [])


async def _prior_user_status(supabase: AsyncClient, user_id: str, posting_id: str) -> str:
    """The caller's current per-user status for a posting, for the audit log.

    ``jobs.status`` was dropped in #75 C4 — the prior status is the caller's own
    per-user state in ``user_jobs`` (absent → ``'new'``). Module-level async
    helper: keeps the ``.execute()`` out of the handler body (#107 guard)."""
    resp = (
        await supabase.table("user_jobs")
        .select("status")
        .eq("user_id", user_id)
        .eq("job_posting_id", posting_id)
        .limit(1)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    return cast(str, rows[0]["status"]) if rows else "new"


async def _insert_status_log(
    supabase: AsyncClient,
    *,
    posting_id: str,
    old_status: str,
    new_status: str,
    note: str | None,
    user_id: str,
) -> None:
    """Append one ``status_log`` audit row for the caller's transition.

    Module-level async helper: keeps the ``.execute()`` out of the handler body
    (#107 guard)."""
    await (
        supabase.table("status_log")
        .insert(
            {
                "posting_id": posting_id,
                "old_status": old_status,
                "new_status": new_status,
                "note": note,
                "user_id": user_id,
            }
        )
        .execute()
    )


# Handlers are `async def` (#57 slice 4): their DB round-trips await natively on
# the pooled RLS user client instead of tying up a threadpool worker per call.
# Each DB access delegates to a module-level async helper (above) so the handler
# body carries no inline ``.execute()`` — the #107 static guard bans a literal
# ``.execute()`` in an ``async def`` handler regardless of the (async) client.
@router.get("/{posting_id}/status-history")
async def get_status_history(
    posting_id: str,
    user_id: str = Depends(get_current_user_id),
    # #88 Phase 2: RLS client — status_log has a per-user SELECT policy, and
    # the ownership probe only reads shared-catalog tables (SELECT true).
    supabase: AsyncClient = Depends(get_async_user_supabase),
) -> dict[str, Any]:
    await _assert_user_owns_posting(supabase, posting_id, user_id)
    entries = await _status_history_rows(supabase, posting_id, user_id)
    return {"entries": entries}


@router.post("/{posting_id}/status")
async def update_status(
    posting_id: str,
    body: StatusUpdate,
    user_id: str = Depends(get_current_user_id),
    # #88 Phase 2: RLS client — user_jobs has a full CRUD self-policy and
    # status_log gained a self-INSERT policy (20260702100000), so RLS pins
    # both writes to the caller underneath the app-layer user_id values.
    supabase: AsyncClient = Depends(get_async_user_supabase),
) -> dict[str, Any]:
    posting = await _assert_user_owns_posting(supabase, posting_id, user_id)
    target_id = posting["target_id"]

    old_status = await _prior_user_status(supabase, user_id, posting_id)

    await _insert_status_log(
        supabase,
        posting_id=posting_id,
        old_status=old_status,
        new_status=body.status,
        note=body.note,
        user_id=user_id,
    )

    # Per-user pipeline state lives in user_jobs (#75 C3): this writer no
    # longer touches the global jobs.status. The list/counts read per-user
    # status from user_jobs and gate global liveness on jobs.archived_at.
    await _upsert_user_job(supabase, user_id=user_id, job_posting_id=posting_id, status=body.status)

    # Scoped invalidation: a single posting status change only affects the
    # owning target's cached pages and the global view. Sibling targets'
    # cached pages stay warm.
    job_list_cache.invalidate(prefix=f"{jobs_cache_prefix(target_id=target_id)}:")
    job_list_cache.invalidate(prefix=f"{jobs_cache_prefix(target_id=None)}:")
    return {"success": True, "old_status": old_status, "new_status": body.status}
