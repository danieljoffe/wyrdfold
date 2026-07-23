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
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

from supabase import Client

from app.services.experience import prose
from app.services.experience.resolve import resolve_current_payload
from app.services.llm import cost_log
from app.services.llm.client import LLMClient
from app.services.targets import crud
from app.services.targets.fit_score import DEFAULT_PURPOSE as FIT_SCORE_PURPOSE
from app.services.targets.fit_score import derive_fit_score

logger = logging.getLogger(__name__)


def current_prose_doc_id(supabase: Client, user_id: str) -> str | None:
    """The id of the user's current experience master doc (the version a fresh
    fit score is stamped with), or None when they have no prose profile."""
    doc = prose.get_latest(supabase, user_id)
    return doc.id if doc is not None else None


def stale_target_ids(
    supabase: Client, *, user_id: str, current_prose_doc_id: str, limit: int
) -> list[str]:
    """Up to ``limit`` target ids whose cached fit_score is stale vs the current
    profile version.

    Stale = the link HAS a score (``fit_score`` not null) but its version marker
    differs from ``current_prose_doc_id`` — including a NULL marker (scored before
    version tracking). Unscored links (null ``fit_score``) are skipped: there is
    nothing to refresh yet (their initial derive is still pending or was never
    possible). ``limit <= 0`` disables the sweep.
    """
    if limit <= 0:
        return []
    resp = (
        supabase.table("user_targets")
        .select("target_id, fit_score, fit_score_prose_doc_id")
        .eq("user_id", user_id)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    stale = [
        tid
        for r in rows
        if r.get("fit_score") is not None
        and r.get("fit_score_prose_doc_id") != current_prose_doc_id
        and (tid := r.get("target_id"))
    ]
    return stale[:limit]


async def refresh_stale_fit_scores(
    supabase: Client, llm: LLMClient, *, user_id: str, target_ids: list[str]
) -> int:
    """Recompute + re-stamp the fit score for each (stale) target. Best-effort;
    returns the number actually refreshed.

    Resolves the current payload ONCE (shared across the batch), then for each
    target re-checks its marker right before spending an LLM call — a concurrent
    refresh from a second quick view may have already freshened it — and writes
    only the fit columns (never ``is_active``, so a background rescore can't flip
    the active flag or trip the cap). One target's failure never aborts the rest.

    Runs as a FastAPI ``BackgroundTask`` (an async fn awaited on the event loop),
    so every blocking supabase round-trip is off-loaded to the threadpool (#107).
    """
    payload, prose_doc_id = await resolve_current_payload(
        supabase, llm, cost_supabase=supabase, user_id=user_id
    )
    if payload is None or prose_doc_id is None:
        return 0

    refreshed = 0
    for target_id in target_ids:
        try:
            # Re-check staleness against the freshly-resolved version before
            # paying for the LLM — cheap guard against double-spend on rapid views.
            marker = await asyncio.to_thread(
                crud.get_fit_score_prose_doc_id, supabase, user_id=user_id, target_id=target_id
            )
            if marker == prose_doc_id:
                continue
            target = await asyncio.to_thread(crud.get, supabase, target_id)
            if target is None:
                continue

            fit_result, llm_result = await derive_fit_score(llm, payload=payload, target=target)
            await asyncio.to_thread(
                cost_log.record,
                supabase,
                user_id=user_id,
                purpose=FIT_SCORE_PURPOSE,
                result=llm_result,
                metadata={"target_id": target_id, "user_id": user_id, "reason": "lazy_refresh"},
            )
            await asyncio.to_thread(
                crud.update_fit_score,
                supabase,
                user_id=user_id,
                target_id=target_id,
                fit_score=fit_result.fit_score,
                fit_score_reasoning=fit_result.reasoning,
                fit_score_prose_doc_id=prose_doc_id,
            )
            refreshed += 1
        except Exception:
            logger.exception("lazy fit-score refresh failed for target %s", target_id)

    if refreshed:
        logger.info("lazy fit-score refresh: %d target(s) for user %s", refreshed, user_id)
    return refreshed
