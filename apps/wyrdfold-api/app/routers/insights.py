"""Insights router (#512).

Three GET endpoints return pre-aggregated analytics for the insights
dashboard.  Each accepts a ``?period=`` query param (7d/30d/90d/all)
and delegates to the corresponding service function.

All aggregations are scoped to the JWT subject. The router resolves the
caller's target_ids and passes them down to bound every query; since #88
Phase 3 the queries also run on the RLS-bound user client, so Postgres
scopes the per-user tables even if a service-layer filter slips.
"""

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from fastapi import APIRouter, Depends, Query
from supabase import AsyncClient

from app.cache import insights_cache, make_cache_key
from app.dependencies import (
    get_async_user_supabase,
    get_current_user_id,
    verify_supabase_jwt,
)
from app.models.insights import PipelineInsights, SkillsCostInsights, TargetInsights
from app.services.insights import compute_pipeline, compute_skills_cost, compute_targets

# JWT-only — insights are personal analytics. The api-key path would let a
# leaked operator key dump cross-tenant aggregates.
router = APIRouter(
    prefix="/insights",
    tags=["insights"],
    dependencies=[Depends(verify_supabase_jwt)],
)

_PERIOD_DAYS: dict[str, int | None] = {
    "7d": 7,
    "30d": 30,
    "90d": 90,
    "all": None,
}


def _since(period: str) -> datetime | None:
    days = _PERIOD_DAYS.get(period)
    if days is None:
        return None
    return datetime.now(UTC) - timedelta(days=days)


def _prior_window(period: str) -> tuple[datetime, datetime] | None:
    """Return ``(prior_since, prior_until)`` covering the period of equal length
    immediately before the current window. Returns None for ``'all'`` (no
    meaningful prior)."""
    days = _PERIOD_DAYS.get(period)
    if days is None:
        return None
    now = datetime.now(UTC)
    prior_until = now - timedelta(days=days)
    prior_since = now - timedelta(days=days * 2)
    return (prior_since, prior_until)


def _empty_pipeline() -> PipelineInsights:
    return PipelineInsights(
        total_applications=0,
        total_interviews=0,
        total_offers=0,
        response_rate=None,
        avg_days_to_response=None,
        velocity=[],
        funnel=[],
        previous=None,
    )


def _empty_targets() -> TargetInsights:
    return TargetInsights(
        targets=[],
        score_distribution=[],
        score_trend=[],
        unscored_count=0,
    )


def _empty_skills_cost() -> SkillsCostInsights:
    return SkillsCostInsights(
        top_skills=[],
        top_missing=[],
        cost_over_time=[],
        cost_by_purpose=[],
        total_cost=0,
        avg_cost_per_resume=None,
    )


async def _user_target_ids(supabase: AsyncClient, user_id: str) -> set[str]:
    """Any-status target ids for the user. An async inline of
    ``crud.get_user_target_ids`` (which stays sync for its sync callers) — an
    async handler holds the async user client and can't hand it to that sync
    helper (#57 slice 3)."""
    resp = (
        await supabase.table("user_targets").select("target_id").eq("user_id", user_id).execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    return {r["target_id"] for r in rows}


# Handlers are `async def`: their DB reads run natively on the event loop via the
# pooled async user client (#57 slice 3), so interactive requests no longer tie up
# a threadpool worker for the duration of each supabase call.


@router.get("/pipeline")
async def pipeline_insights(
    period: str = Query("30d", pattern=r"^(7d|30d|90d|all)$"),
    user_id: str = Depends(get_current_user_id),
    # #88 Phase 3: read-only over user_jobs/status_log/analyses/llm_costs
    # (self-SELECT policies) + the shared catalog (SELECT true) — RLS scopes
    # the per-user tables underneath the service-layer user_id filters.
    supabase: AsyncClient = Depends(get_async_user_supabase),
) -> PipelineInsights:
    # Cache first: the key is (user, period), so a hit needs no DB at all. The
    # user_targets lookup only runs on a miss — the empty-targets early-return
    # stays uncached so freshly-added targets show up immediately (Perf-F7).
    cache_key = make_cache_key(f"insights:pipeline:u={user_id}", period=period)
    cached: PipelineInsights | None = insights_cache.get(cache_key)
    if cached is not None:
        return cached
    target_ids = await _user_target_ids(supabase, user_id)
    if not target_ids:
        return _empty_pipeline()
    result = await compute_pipeline(
        supabase,
        _since(period),
        _prior_window(period),
        target_ids=target_ids,
        user_id=user_id,
    )
    insights_cache.set(cache_key, result)
    return result


@router.get("/targets")
async def target_insights(
    period: str = Query("30d", pattern=r"^(7d|30d|90d|all)$"),
    user_id: str = Depends(get_current_user_id),
    supabase: AsyncClient = Depends(get_async_user_supabase),  # #88 Phase 3: see /pipeline
) -> TargetInsights:
    cache_key = make_cache_key(f"insights:targets:u={user_id}", period=period)
    cached: TargetInsights | None = insights_cache.get(cache_key)
    if cached is not None:
        return cached
    target_ids = await _user_target_ids(supabase, user_id)
    if not target_ids:
        return _empty_targets()
    result = await compute_targets(
        supabase, _since(period), target_ids=target_ids, user_id=user_id
    )
    insights_cache.set(cache_key, result)
    return result


@router.get("/skills-cost")
async def skills_cost_insights(
    period: str = Query("30d", pattern=r"^(7d|30d|90d|all)$"),
    user_id: str = Depends(get_current_user_id),
    supabase: AsyncClient = Depends(get_async_user_supabase),  # #88 Phase 3: see /pipeline
) -> SkillsCostInsights:
    cache_key = make_cache_key(f"insights:skills-cost:u={user_id}", period=period)
    cached: SkillsCostInsights | None = insights_cache.get(cache_key)
    if cached is not None:
        return cached
    target_ids = await _user_target_ids(supabase, user_id)
    if not target_ids:
        return _empty_skills_cost()
    result = await compute_skills_cost(
        supabase, _since(period), user_id=user_id, target_ids=target_ids
    )
    insights_cache.set(cache_key, result)
    return result
