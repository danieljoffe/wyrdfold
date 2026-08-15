"""Lazy fit-score refresh (E2).

The per-user cached ``user_targets.fit_score`` is an LLM judgment of how well the
user's experience fits a target, computed at link time. A later profile edit
makes it stale — and the E fix (``resolve_current_payload``) only freshened NEW
targets; existing ones kept their old score with nothing to recompute them.

This refreshes existing targets LAZILY: on view (``GET /targets/mine``), targets
whose score was computed against an older prose master doc are recomputed in the
background — capped per view so one page load can't fire a burst of LLM calls,
and only paid for targets the user actually looks at (vs eagerly rescoring every
target on every profile edit). The view returns the cached scores immediately;
fresh ones land by the next view.

#57 PR-G2e-5: this module runs on the pooled async service client. ``/mine``
spawns ``refresh_stale_for_user`` as a DETACHED loop task (``spawn_detached``, not
a starlette ``BackgroundTask`` — the async pool deadlocks under uvloop there), and
the crud reads/writes it needs run through thin async inlines here (crud stays
SYNC for its poller/learner/operator callers).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, cast

from supabase import AsyncClient

from app.config import settings
from app.constants import resolve_owner
from app.models.targets import JobTarget
from app.services.experience import prose
from app.services.experience.resolve import resolve_current_payload
from app.services.llm import cost_log, provider_breaker
from app.services.llm.client import LLMClient
from app.services.llm.errors import LLMQuotaExhaustedError, LLMRateLimitedError
from app.services.targets import crud
from app.services.targets.fit_score import DEFAULT_PURPOSE as FIT_SCORE_PURPOSE
from app.services.targets.fit_score import derive_fit_score

logger = logging.getLogger(__name__)


# ── Async inlines of the crud reads/writes this module needs (#57 PR-G2e-5) ──
# crud stays SYNC for its poller/learner/operator callers; a sync helper can't
# take the async client and converting crud would ripple into all of them. So the
# queries this module needs run here on the async client, reusing crud's row
# parser so the persisted shape stays byte-identical.


async def _target_get(supabase: AsyncClient, target_id: str) -> JobTarget | None:
    """Async inline of ``crud.get``."""
    resp = await supabase.table(crud.TARGETS_TABLE).select("*").eq("id", target_id).execute()
    rows = cast(list[dict[str, Any]], resp.data or [])
    return crud._parse_target(rows[0]) if rows else None


async def _fit_score_marker(
    supabase: AsyncClient, *, user_id: str, target_id: str
) -> str | None:
    """Async inline of ``crud.get_fit_score_prose_doc_id`` — the version marker on
    the user's link, re-read right before a lazy refresh recomputes so a concurrent
    refresh (two quick views) doesn't double-spend the LLM."""
    resp = await (
        supabase.table(crud.USER_TARGETS_TABLE)
        .select("fit_score_prose_doc_id")
        .eq("user_id", user_id)
        .eq("target_id", target_id)
        .limit(1)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    return rows[0].get("fit_score_prose_doc_id") if rows else None


async def _update_fit_score(
    supabase: AsyncClient,
    *,
    user_id: str,
    target_id: str,
    fit_score: int,
    fit_score_reasoning: str | None,
    fit_score_prose_doc_id: str | None,
) -> None:
    """Async inline of ``crud.update_fit_score`` — a targeted UPDATE of ONLY the
    fit columns (never ``is_active``, so a background rescore can't flip the active
    flag or trip the active-target cap)."""
    await (
        supabase.table(crud.USER_TARGETS_TABLE)
        .update(
            {
                "fit_score": fit_score,
                "fit_score_reasoning": fit_score_reasoning,
                "fit_score_prose_doc_id": fit_score_prose_doc_id,
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )
        .eq("user_id", user_id)
        .eq("target_id", target_id)
        .execute()
    )


async def current_prose_doc_id(supabase: AsyncClient, user_id: str) -> str | None:
    """The id of the user's current experience master doc (the version a fresh
    fit score is stamped with), or None when they have no prose profile."""
    resp = await (
        supabase.table(prose.TABLE)
        .select("id")
        .order("version", desc=True)
        .limit(1)
        .eq("user_id", resolve_owner(user_id))
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    return rows[0].get("id") if rows else None


async def _not_deriving(supabase: AsyncClient, target_ids: list[str]) -> list[str]:
    """Of ``target_ids``, those whose derive is NOT currently in flight.

    Guards the unscored branch below against double-spend: a target created
    seconds ago is legitimately unscored because its inline derive is still
    running, and re-deriving it from a page view would pay twice and race the
    write. Callers pass an already-capped list, so the ``in_`` stays small.
    """
    if not target_ids:
        return []
    resp = await (
        supabase.table(crud.TARGETS_TABLE)
        .select("id, activation_status")
        .in_("id", target_ids)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    return [tid for r in rows if r.get("activation_status") != "deriving" and (tid := r.get("id"))]


async def stale_target_ids(
    supabase: AsyncClient, *, user_id: str, current_prose_doc_id: str, limit: int
) -> list[str]:
    """Up to ``limit`` target ids whose cached fit_score needs recomputing.

    Two kinds qualify, UNSCORED FIRST:

    - **Unscored** (``fit_score`` is null) and not currently deriving. These
      show no badge at all, so they are the worse user-visible state and get
      priority over a merely-stale number.
    - **Stale**: the link has a score but its version marker differs from
      ``current_prose_doc_id`` — including a NULL marker (scored before version
      tracking).

    ``limit <= 0`` disables the sweep.

    WHY UNSCORED IS INCLUDED (changed 2026-08-15). This function used to skip
    null scores on the reasoning that "their initial derive is still pending or
    was never possible". The first half is handled by the ``_not_deriving``
    guard; the second half was the bug. Nothing ever completes a derive that
    was never possible, so "pending" became permanent: a target whose creation
    ran with no resolvable payload (``from_input._apply_fit_score`` is called
    only ``if payload is not None``, and skipping it records no error either)
    kept a null score forever. This was the ONLY self-heal path in the system
    and it was written to skip exactly the state that needs healing — found on
    prod with a target unscored for a full day
    (``docs/ux-resweep-targets-2026-08-14.md`` C1).

    Spend stays bounded by the same per-view cap as before; unscored targets
    just consume it first. A target skipped because the cap filled, or because
    it was mid-derive, is retried on the next view.
    """
    if limit <= 0:
        return []
    resp = await (
        supabase.table("user_targets")
        .select("target_id, fit_score, fit_score_prose_doc_id")
        .eq("user_id", user_id)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])

    unscored_candidates = [
        tid for r in rows if r.get("fit_score") is None and (tid := r.get("target_id"))
    ]
    # Cap BEFORE the status lookup so the `in_` can't grow with the user's
    # target count (a large `in_` is also a 414 risk on this client).
    unscored = await _not_deriving(supabase, unscored_candidates[:limit])

    stale = [
        tid
        for r in rows
        if r.get("fit_score") is not None
        and r.get("fit_score_prose_doc_id") != current_prose_doc_id
        and (tid := r.get("target_id"))
    ]
    return (unscored + stale)[:limit]


async def refresh_stale_fit_scores(
    supabase: AsyncClient, llm: LLMClient, *, user_id: str, target_ids: list[str]
) -> int:
    """Recompute + re-stamp the fit score for each (stale) target. Best-effort;
    returns the number actually refreshed.

    Resolves the current payload ONCE (shared across the batch), then for each
    target re-checks its marker right before spending an LLM call — a concurrent
    refresh from a second quick view may have already freshened it — and writes
    only the fit columns (never ``is_active``, so a background rescore can't flip
    the active flag or trip the cap). One target's failure never aborts the rest.

    Rides the pooled async service client end to end (#57 PR-G2e-5): every read /
    write is awaited on the loop through the async inlines above, so nothing blocks.
    """
    try:
        payload, prose_doc_id = await resolve_current_payload(
            supabase, llm, cost_supabase=supabase, user_id=user_id
        )
    except (LLMQuotaExhaustedError, LLMRateLimitedError) as exc:
        # resolve may itself derive from prose (an LLM call). If the provider is
        # fatal, latch the breaker + bail — no point walking the targets.
        provider_breaker.trip_provider_fatal(exc)
        return 0
    if payload is None or prose_doc_id is None:
        return 0

    refreshed = 0
    for target_id in target_ids:
        try:
            # Re-check staleness against the freshly-resolved version before
            # paying for the LLM — cheap guard against double-spend on rapid views.
            marker = await _fit_score_marker(supabase, user_id=user_id, target_id=target_id)
            if marker == prose_doc_id:
                continue
            target = await _target_get(supabase, target_id)
            if target is None:
                continue

            fit_result, llm_result = await derive_fit_score(llm, payload=payload, target=target)
            await cost_log.record_async(
                supabase,
                user_id=user_id,
                purpose=FIT_SCORE_PURPOSE,
                result=llm_result,
                metadata={"target_id": target_id, "user_id": user_id, "reason": "lazy_refresh"},
            )
            await _update_fit_score(
                supabase,
                user_id=user_id,
                target_id=target_id,
                fit_score=fit_result.fit_score,
                fit_score_reasoning=fit_result.reasoning,
                fit_score_prose_doc_id=prose_doc_id,
            )
            refreshed += 1
        except (LLMQuotaExhaustedError, LLMRateLimitedError) as exc:
            # Provider-fatal (out of credits / sustained 429): every remaining
            # target would fail the same way. Latch the shared breaker so this
            # user's next /mine — and the poller — skip the doomed calls, and stop
            # this batch here instead of hammering. This is what stops a credits
            # outage from turning every /mine into a churn of failed refreshes.
            provider_breaker.trip_provider_fatal(exc)
            break
        except Exception:
            logger.exception("lazy fit-score refresh failed for target %s", target_id)

    if refreshed:
        logger.info("lazy fit-score refresh: %d target(s) for user %s", refreshed, user_id)
    return refreshed


async def refresh_stale_for_user(supabase: AsyncClient, llm: LLMClient, *, user_id: str) -> None:
    """Background entrypoint for the lazy refresh (E2): compute the user's current
    profile version + which of their targets are stale, then refresh them —
    ENTIRELY off the ``/targets/mine`` response path (the caller just spawns this
    detached and returns the cached scores immediately).

    Skips at the door when the provider-fatal breaker is latched (a credits
    outage / sustained 429), so a down provider can't make every ``/mine`` view
    re-schedule a doomed refresh + re-run the staleness query. The refresh itself
    trips the breaker on the first fatal error, so the very first outage latches
    it for the whole cooldown.
    """
    if provider_breaker.provider_fatal_active():
        return
    prose_doc_id = await current_prose_doc_id(supabase, user_id)
    if prose_doc_id is None:
        return
    stale = await stale_target_ids(
        supabase,
        user_id=user_id,
        current_prose_doc_id=prose_doc_id,
        limit=settings.fit_score_refresh_max_per_view,
    )
    if stale:
        await refresh_stale_fit_scores(supabase, llm, user_id=user_id, target_ids=stale)
