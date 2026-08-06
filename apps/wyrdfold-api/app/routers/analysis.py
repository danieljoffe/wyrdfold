"""Analysis router.

``POST /analysis/{job_id}?target_id=...`` kicks off (or returns a cached) LLM
analysis for a job posting against a specific target. The LLM call is ~26s, so
the POST is **non-blocking** (#459): on a cache miss it hands the run to a
detached background task and returns ``202 {"status": "running"}`` at once, and
the client polls ``GET /analysis/{job_id}?target_id=...`` until the result
lands in the durable ``analyses`` cache. Because the task persists regardless
of the client, the user is free to navigate away and come back to a finished
analysis. Cache key is ``(job_posting_id, target_id, optimized_doc_id)``.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from supabase import AsyncClient

from app.background import spawn_detached
from app.cache import job_list_cache, jobs_cache_prefix
from app.config import Settings
from app.constants import resolve_owner
from app.dependencies import (
    enforce_llm_budget,
    get_async_service_supabase,
    get_async_supabase_for_caller,
    get_current_user_id_optional,
    get_llm_client,
    get_settings,
    verify_api_key_or_jwt,
)
from app.models.analysis import AnalysisStatusResponse, JobAnalysisRecord
from app.models.experience import OptimizedDoc
from app.models.targets import JobTarget
from app.services.analysis import persistence, run_registry
from app.services.analysis.analyze import DEFAULT_PURPOSE, analyze_job
from app.services.analysis.scoring import scorecard_to_numeric
from app.services.experience import optimized
from app.services.llm import cost_log
from app.services.llm.client import LLMClient
from app.services.targets import crud as targets_crud

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/analysis",
    tags=["analysis"],
    dependencies=[Depends(verify_api_key_or_jwt)],
)


def _no_profile_response() -> JSONResponse:
    """The ``no_profile`` empty-state marker (#105).

    A 200 (not a 4xx) so the panel's auto-fired call doesn't log a console
    error, and the body carries a structured code without leaking any internal
    endpoint path into the UI.
    """
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "code": "no_profile",
            "message": ("Set up your experience profile to generate a job-fit analysis."),
        },
    )


# ---- Inline async reads (#57 slice 3) -------------------------------------
#
# Handlers run on the async user/service client. A few shared sync helpers
# (``targets_crud.get_user_target_ids`` / ``targets_crud.get`` /
# ``optimized.get_latest`` / ``budget.check_daily_count``) stay SYNC for their
# not-yet-converted callers (poller / targets / experience chain) — a sync
# helper can't take the async client, and converting them would break those
# callers. So these thin async inlines run the same queries on the async client
# (the insights.py::_user_target_ids / experience.py / tailor.py pattern). No
# sync+async twin in the shared layer.


async def _user_target_ids(supabase: AsyncClient, user_id: str) -> set[str]:
    """Any-status target ids for the user. Async inline of
    ``targets_crud.get_user_target_ids`` (sync twin kept for its sync callers)."""
    resp = (
        await supabase.table("user_targets").select("target_id").eq("user_id", user_id).execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    return {r["target_id"] for r in rows}


async def _optimized_latest(supabase: AsyncClient, user_id: str | None) -> OptimizedDoc | None:
    """Async inline of ``optimized.get_latest`` (sync twin kept for the poller /
    targets / annotations chain). Reads fresh: the module TTL cache is populated
    only by those sync callers and every write invalidates it."""
    resp = await (
        supabase.table(optimized.TABLE)
        .select("*")
        .order("version", desc=True)
        .limit(1)
        .eq("user_id", resolve_owner(user_id))
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    return OptimizedDoc.model_validate(rows[0]) if rows else None


async def _target_get(supabase: AsyncClient, target_id: str) -> JobTarget | None:
    """Async inline of ``targets_crud.get`` (sync twin kept for its other
    callers). Reuses ``crud._parse_target`` so the row shape stays identical."""
    resp = await (
        supabase.table(targets_crud.TARGETS_TABLE).select("*").eq("id", target_id).execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    return targets_crud._parse_target(rows[0]) if rows else None


async def _fetch_job_description(supabase: AsyncClient, job_id: str) -> str | None:
    """The posting's ``description_html`` (empty string when NULL), or ``None``
    when the posting doesn't exist — so the caller can 404 vs 422 distinctly."""
    resp = await (
        supabase.table("jobs")
        .select("id, description_html")
        .eq("id", job_id)
        .limit(1)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    if not rows:
        return None
    return rows[0].get("description_html") or ""


async def _check_daily_count(
    supabase: AsyncClient,
    *,
    user_id: str,
    purpose: str,
    limit: int,
    in_flight: int = 0,
) -> None:
    """Async inline of ``budget.check_daily_count`` (sync twin kept for its unit
    test). Raise 429 if the user already has ``limit`` ``llm_costs`` rows for
    ``purpose`` in the rolling 24h window; ``in_flight`` is added before the
    comparison so a burst of concurrent kicks (whose cost rows land ~26s later)
    can't slip past a gate that only sees already-persisted rows. ``limit=0``
    disables."""
    if limit <= 0:
        return
    since = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
    resp = await (
        supabase.table("llm_costs")
        # head=True → count only, no rows shipped (HEAD request).
        .select("id", count="exact", head=True)  # type: ignore[arg-type]
        .eq("user_id", user_id)
        .eq("purpose", purpose)
        .gte("created_at", since)
        .execute()
    )
    used = resp.count or 0
    if used + in_flight >= limit:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "analysis_daily_limit",
                "purpose": purpose,
                "limit": limit,
                "used": used + in_flight,
            },
        )


async def _resolve_state(
    job_id: str,
    target_id: str,
    *,
    supabase: AsyncClient,
    caller_supabase: AsyncClient,
    user_id: str | None,
) -> tuple[OptimizedDoc | None, JobAnalysisRecord | None]:
    """Shared POST/GET preamble: ownership gate → optimized doc → cache read.

    * **Ownership** — the analysis WRITE re-ranks the shared ``(job, target)``
      scores row, so a JWT caller must be linked to ``target_id`` (audit #24
      F2). 404 not 403 so non-owners can't enumerate target existence. api-key
      callers (``user_id`` None) are operators and bypass, matching the targets
      router. Raises ``HTTPException(404)``.
    * **Optimized doc** — needed for the cache key. ``None`` → the caller has no
      experience profile yet.
    * **Cache** — keyed on ``(job, target, optimized version)``. Stays on the
      **service-role** client (a false miss here is silent duplicate LLM spend).

    Returns ``(optimized, cached)``: ``optimized is None`` → no-profile;
    ``cached is not None`` → a persisted analysis exists.
    """
    if user_id is not None and target_id not in await _user_target_ids(caller_supabase, user_id):
        raise HTTPException(status_code=404, detail="Target not found.")

    current_optimized = await _optimized_latest(caller_supabase, user_id)
    if current_optimized is None:
        return None, None

    cached = await persistence.get_cached(
        supabase,
        job_id,
        target_id=target_id,
        optimized_doc_id=current_optimized.id,
        user_id=user_id,
    )
    return current_optimized, cached


# response_model=None: the handler returns either a JobAnalysisRecord or a 200
# JSONResponse marker (the no-profile empty state #105, or a 202 running/status
# marker #459), which FastAPI can't express as a single response model — it
# serializes each return as-is.
@router.post("/{job_id}", response_model=None, dependencies=[Depends(enforce_llm_budget)])
async def create_analysis(
    job_id: str,
    target_id: str = Query(..., description="Target the user is viewing the job under"),
    supabase: AsyncClient = Depends(get_async_service_supabase),
    # #6 R2 / #88 dual-client split:
    # * `caller_supabase` (JWT → RLS user client, api-key → service-role)
    #   carries every read a JWT caller is entitled to make directly — the
    #   ownership gate (user_targets, self-scoped), the optimized doc
    #   (experience_optimized_docs, self-scoped), the target + job posting
    #   (SELECT-true shared catalog).
    # * `supabase` (service-role) keeps the COST-BEARING plumbing: the
    #   analyses cache read + upsert and the llm_costs write/count.
    #   `analyses`/`llm_costs` have no INSERT policy for `authenticated` by
    #   design, and an RLS surprise on the cache read or the daily-count
    #   read would silently turn into duplicate LLM spend / a widened
    #   budget — the failure mode must stay impossible, not just tested.
    caller_supabase: AsyncClient = Depends(get_async_supabase_for_caller),
    llm: LLMClient = Depends(get_llm_client),
    user_id: str | None = Depends(get_current_user_id_optional),
    s: Settings = Depends(get_settings),
) -> JobAnalysisRecord | JSONResponse:
    optimized_doc, cached = await _resolve_state(
        job_id,
        target_id,
        supabase=supabase,
        caller_supabase=caller_supabase,
        user_id=user_id,
    )
    if optimized_doc is None:
        # The panel auto-fires this on first open; a 404 would both leak an
        # internal endpoint path into the UI and log a console error on every
        # profile-less job open. Render the "set up your profile" CTA instead
        # via a 200 marker (#105).
        return _no_profile_response()

    if cached is not None:
        # Re-apply the LLM blend even on a cache hit. The blend writes the
        # per-target score + flips ``scoring_status`` to ``complete``, but
        # analyses persisted *before* this code shipped never had that backfill
        # done — so a returning user whose analysis was previously cached would
        # still see the keyword-only score in the list view. ``_apply_llm_blend``
        # is idempotent, so re-running it on every cache hit is a cheap no-op
        # once the row already has the blended score.
        await _apply_llm_blend(
            supabase,
            job_posting_id=job_id,
            target_id=target_id,
            scorecard=cached.scorecard,
            analysis_id=cached.id,
        )
        return cached

    key = (job_id, target_id, optimized_doc.id)

    # Dedup: a run is already in flight for this exact cache key → don't spawn a
    # second (double LLM spend) and don't re-count; tell the client to keep
    # polling. This also makes the panel's auto-fire + any StrictMode
    # double-invoke safe.
    if run_registry.is_running(key):
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=AnalysisStatusResponse(status="running").model_dump(),
        )

    # Claim the key immediately — no ``await`` between the is_running check and
    # begin(), so on the single event loop two concurrent kicks can't both pass
    # the check and both spawn. Validation below runs *after* the claim; if it
    # fails we release the claim so the key is instantly re-kickable.
    run_registry.begin(key, user_id=user_id)
    try:
        # Cache miss + fresh claim → this run WILL spend LLM money. Count gate
        # sits here, after the cache + dedup checks, so re-viewing a cached or
        # in-flight analysis is always free and never 429s. ``in_flight``
        # excludes THIS run (already claimed above); it counts the caller's
        # OTHER concurrent kicks, whose cost rows won't exist until their LLM
        # calls return ~26s from now — without it a burst could all pass a gate
        # that only sees persisted rows and blow past the cap.
        if user_id is not None:
            other_in_flight = max(0, run_registry.running_count_for_user(user_id) - 1)
            await _check_daily_count(
                supabase,
                user_id=user_id,
                purpose=DEFAULT_PURPOSE,
                limit=s.analysis_daily_limit,
                in_flight=other_in_flight,
            )

        # Fetch target (existence + LLM context) and the JD in-line so 404/422
        # surface as real HTTP status on the POST, not as a silent task failure.
        target = await _target_get(caller_supabase, target_id)
        if target is None:
            raise HTTPException(status_code=404, detail="Target not found.")

        description_html = await _fetch_job_description(caller_supabase, job_id)
        if description_html is None:
            raise HTTPException(status_code=404, detail="Job posting not found.")
        if not description_html.strip():
            raise HTTPException(
                status_code=422,
                detail="Job posting has no description to analyze.",
            )
    except Exception:
        # Validation failed (429/404/422) → release the claim so a corrected
        # retry can re-kick immediately, then surface the real error.
        run_registry.finish(key)
        raise

    target_context = f"Target: {target.label}" + (
        f"\nDescription: {target.description}" if target.description else ""
    )

    # Hand the ~26s LLM run to a DETACHED task and return now. spawn_detached
    # (not starlette BackgroundTasks — uvloop pool-deadlock, see
    # app/background.py) is exactly the "202, the work outlives the request"
    # semantic: it persists regardless of the client, so the user can navigate
    # away and a later POST/GET hits the cache. The task runs on the
    # service-role client (the entitlement reads already happened above).
    spawn_detached(
        _run_analysis_task(
            key=key,
            supabase=supabase,
            llm=llm,
            job_id=job_id,
            target_id=target_id,
            user_id=user_id,
            optimized_doc=optimized_doc,
            target_context=target_context,
            description_html=description_html,
        ),
        name=f"analysis:{job_id}:{target_id}",
    )
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=AnalysisStatusResponse(status="running").model_dump(),
    )


@router.get("/{job_id}", response_model=None)
async def get_analysis(
    job_id: str,
    target_id: str = Query(..., description="Target the user is viewing the job under"),
    supabase: AsyncClient = Depends(get_async_service_supabase),
    caller_supabase: AsyncClient = Depends(get_async_supabase_for_caller),
    user_id: str | None = Depends(get_current_user_id_optional),
) -> JobAnalysisRecord | JSONResponse:
    """Poll the state of a (possibly backgrounded) analysis (#459).

    Read-only companion to POST: returns the persisted ``JobAnalysisRecord``
    once the detached run lands, or a status marker while it hasn't:

    * ``running`` — still in flight; keep polling.
    * ``error``   — the run failed; offer a retry (POST again).
    * ``idle``    — nothing cached and nothing in flight (e.g. a restart
      dropped the run); the client re-kicks via POST.

    No LLM spend, no writes here — so no budget gate and no score blend (the
    task blends on completion, before it clears the in-flight flag, so a record
    returned here already reflects the blended ranking).
    """
    optimized_doc, cached = await _resolve_state(
        job_id,
        target_id,
        supabase=supabase,
        caller_supabase=caller_supabase,
        user_id=user_id,
    )
    if optimized_doc is None:
        return _no_profile_response()
    if cached is not None:
        return cached

    st = run_registry.get((job_id, target_id, optimized_doc.id))
    if st is not None and st.status == "running":
        marker = AnalysisStatusResponse(status="running")
    elif st is not None and st.status == "error":
        marker = AnalysisStatusResponse(status="error", message=st.error)
    else:
        marker = AnalysisStatusResponse(status="idle")
    return JSONResponse(status_code=status.HTTP_200_OK, content=marker.model_dump())


async def _run_analysis_task(
    *,
    key: run_registry.Key,
    supabase: AsyncClient,
    llm: LLMClient,
    job_id: str,
    target_id: str,
    user_id: str | None,
    optimized_doc: OptimizedDoc,
    target_context: str,
    description_html: str,
) -> None:
    """Detached worker: run the LLM analysis, persist, cost-log, blend, then
    clear the in-flight flag (#459).

    Runs entirely on the **service-role** client — the ownership + entitlement
    reads already happened synchronously in ``create_analysis`` before this was
    spawned. Any failure marks the run ``error`` so the client's next poll can
    offer a retry; it must never let an exception escape (spawn_detached's
    done-callback only logs, it can't recover the user's request).
    """
    try:
        analysis, llm_result = await analyze_job(
            llm,
            optimized=optimized_doc.payload,
            job_description=description_html,
            target_context=target_context,
        )

        # Persist FIRST — once the cache row exists the analysis is "done" from
        # the user's perspective (a poll returns it), so a later best-effort
        # hiccup can't demote a finished analysis back to an error.
        record = await persistence.persist(
            supabase,
            job_posting_id=job_id,
            target_id=target_id,
            user_id=user_id,
            optimized_doc_id=optimized_doc.id,
            analysis=analysis,
            llm_result=llm_result,
        )

        # Cost logging is best-effort: a ledger hiccup must not discard the
        # analysis the user is waiting on (already persisted above).
        try:
            await cost_log.record_async(
                supabase,
                user_id=user_id,
                purpose=DEFAULT_PURPOSE,
                result=llm_result,
                metadata={
                    "job_posting_id": job_id,
                    "target_id": target_id,
                    "optimized_doc_id": optimized_doc.id,
                    "backgrounded": True,
                },
            )
        except Exception:
            logger.warning(
                "analysis cost-log failed job=%s target=%s",
                job_id,
                target_id,
                exc_info=True,
            )

        # Blend the LLM score into the per-target row. Done BEFORE finish() so a
        # poll that reads the freshly-cached record already sees the blended
        # ranking. Idempotent + best-effort inside _apply_llm_blend.
        await _apply_llm_blend(
            supabase,
            job_posting_id=job_id,
            target_id=target_id,
            scorecard=analysis.scorecard,
            analysis_id=record.id,
        )
        run_registry.finish(key)
    except Exception:
        # analyze_job (LLM/parse) or persist raised → surface a retryable error
        # via the poll rather than crashing the detached task silently.
        logger.exception("analysis task failed job=%s target=%s", job_id, target_id)
        run_registry.fail(key, message="Analysis failed. Please retry.")


async def _apply_llm_blend(
    supabase: AsyncClient,
    *,
    job_posting_id: str,
    target_id: str,
    scorecard: Any,
    analysis_id: str,
) -> None:
    """Blend the LLM scorecard into the per-target ``scores`` row.

    Reads the current keyword score (shared-readable), blends with the LLM
    numeric, then writes back score + scoring_status='complete' via the
    ``user_apply_score_blend`` SECURITY DEFINER RPC. SEC-H2: that RPC is now
    service_role-only, so this runs on the **service-role** client; ownership
    is enforced upstream by ``create_analysis``'s gate (the caller must follow
    ``target_id`` to reach here). Best-effort: failures are swallowed so an LLM
    blend hiccup doesn't fail the user's request.
    """
    try:
        # #609: the fit verdict IS the score. The old 60/40 keyword/LLM mix
        # let a saturated keyword placeholder drag every verdict toward 100
        # (and with the #609 keyword rescale it would have dragged every
        # verdict toward ~20 instead) — the keyword score is a retrieval
        # signal, not a component of the user-facing grade.
        blended = round(scorecard_to_numeric(scorecard))
        # Gated write: updates the shared (job, target) scores row + stamps
        # jobs.llm_analysis_id behind an ownership check enforced in Postgres.
        await supabase.rpc(
            "user_apply_score_blend",
            {
                "p_job_posting_id": job_posting_id,
                "p_target_id": target_id,
                "p_score": blended,
                "p_analysis_id": analysis_id,
            },
        ).execute()
        # Invalidate the in-memory list cache so the new blended score
        # is reflected immediately. Without this the dashboard +
        # /jobs ranking shows the old keyword-only score for up to 60s
        # (the TTLCache default) after the user runs analysis — the
        # detail endpoint refreshes correctly because it doesn't go
        # through the list cache. Scope to the owning target + the
        # untargeted global view; sibling targets aren't affected.
        job_list_cache.invalidate(prefix=f"{jobs_cache_prefix(target_id=target_id)}:")
        job_list_cache.invalidate(prefix=f"{jobs_cache_prefix(target_id=None)}:")
    except Exception:
        # Best-effort: a stale write here doesn't fail the user's
        # request (the analysis itself succeeded + was returned), but
        # the list ranking won't reflect the LLM blend until the next
        # cron pass. Log via WARNING so the spike is detectable.
        logger.warning(
            "Failed to blend LLM score for job=%s target=%s",
            job_posting_id,
            target_id,
            exc_info=True,
        )
