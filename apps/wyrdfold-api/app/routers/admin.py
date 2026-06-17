"""Operator-facing admin endpoints (api-key only).

The user-facing `/profile/llm-usage` answers "what have *I* spent."
This router answers "what has the whole instance spent" — the surface
the cost-alert breadcrumbs from #26 F3 point an operator at when they
investigate a warning.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from supabase import Client

from app.config import settings
from app.dependencies import get_supabase, verify_api_key
from app.services.llm import cost_log

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(verify_api_key)],
)


class CacheStats(BaseModel):
    """Prompt-cache token usage over a window (#73).

    The three buckets are Anthropic's input-token accounting:
    ``uncached_input_tokens`` is billed at the normal rate,
    ``cache_creation_tokens`` at ~1.25×, ``cache_read_tokens`` at ~0.1×.
    ``hit_rate_pct`` is the share of all input tokens served from cache —
    the number to watch so caching regressions are visible.
    """

    cache_read_tokens: int
    cache_creation_tokens: int
    uncached_input_tokens: int
    hit_rate_pct: float | None = Field(
        description=(
            "cache_read / (cache_read + cache_creation + uncached_input) × "
            "100. None when no input tokens were recorded in the window."
        )
    )

    @classmethod
    def from_buckets(cls, buckets: dict[str, int]) -> CacheStats:
        read = buckets["cache_read"]
        creation = buckets["cache_creation"]
        uncached = buckets["uncached_input"]
        total = read + creation + uncached
        return cls(
            cache_read_tokens=read,
            cache_creation_tokens=creation,
            uncached_input_tokens=uncached,
            hit_rate_pct=round(read / total * 100.0, 1) if total > 0 else None,
        )


class CostSummaryResponse(BaseModel):
    """Snapshot of LLM spend at the moment the request was served.

    All `*_usd` values are aggregated from `llm_costs.cost_usd` rounded
    to the cent. `today_usd` is the same window the global circuit
    breaker reads from — operators investigating a Sentry warning
    should expect this number to match the breaker's view.
    """

    generated_at: datetime
    global_daily_cap_usd: float = Field(
        description=(
            "Snapshot of GLOBAL_LLM_DAILY_BUDGET_USD. 0 means the global "
            "circuit breaker is disabled."
        )
    )
    today_usd: float = Field(
        description="Spend since UTC midnight (the breaker's window)."
    )
    today_usage_pct: float | None = Field(
        description=(
            "today_usd / global_daily_cap_usd × 100. None when the cap is "
            "0 (breaker disabled)."
        )
    )
    last_24h_usd: float
    last_7d_usd: float
    last_30d_usd: float
    by_purpose_today: dict[str, float]
    by_purpose_30d: dict[str, float]
    cache_today: CacheStats = Field(
        description="Prompt-cache hit rate + token buckets since UTC midnight."
    )
    cache_30d: CacheStats = Field(
        description="Prompt-cache hit rate + token buckets over the last 30 days."
    )


@router.get("/cost-summary", response_model=CostSummaryResponse)
def get_cost_summary(
    supabase: Client = Depends(get_supabase),
) -> CostSummaryResponse:
    """Operator drill-in for the LLM cost-alert breadcrumbs (#26 F4).

    No per-user partition — this is the cross-instance rollup the global
    circuit breaker reads from, plus per-purpose breakdowns the operator
    can use to spot a runaway prompt (e.g. "phase1_triage tripled
    yesterday").
    """
    now = datetime.now(UTC)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

    today_usd = cost_log.total_spend_all(supabase, since=midnight)
    last_24h = cost_log.total_spend_all(supabase, since=now - timedelta(hours=24))
    last_7d = cost_log.total_spend_all(supabase, since=now - timedelta(days=7))
    last_30d = cost_log.total_spend_all(supabase, since=now - timedelta(days=30))

    by_purpose_today: dict[str, float] = cost_log.spend_by_purpose_all(
        supabase, since=midnight
    )
    by_purpose_30d: dict[str, float] = cost_log.spend_by_purpose_all(
        supabase, since=now - timedelta(days=30)
    )

    cache_today = CacheStats.from_buckets(
        cost_log.cache_metrics_all(supabase, since=midnight)
    )
    cache_30d = CacheStats.from_buckets(
        cost_log.cache_metrics_all(supabase, since=now - timedelta(days=30))
    )

    cap = settings.global_llm_daily_budget_usd
    usage_pct: float | None
    if cap > 0:
        usage_pct = round(today_usd / cap * 100.0, 1)
    else:
        usage_pct = None

    return CostSummaryResponse(
        generated_at=now,
        global_daily_cap_usd=cap,
        today_usd=today_usd,
        today_usage_pct=usage_pct,
        last_24h_usd=last_24h,
        last_7d_usd=last_7d,
        last_30d_usd=last_30d,
        by_purpose_today=by_purpose_today,
        by_purpose_30d=by_purpose_30d,
        cache_today=cache_today,
        cache_30d=cache_30d,
    )
