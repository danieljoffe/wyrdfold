"""Operator-facing admin endpoints (api-key only).

The user-facing `/profile/llm-usage` answers "what have *I* spent."
This router answers "what has the whole instance spent" — the surface
the cost-alert breadcrumbs from #26 F3 point an operator at when they
investigate a warning.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from supabase import AsyncClient

from app.config import settings
from app.dependencies import get_async_service_supabase, verify_api_key
from app.services.llm import cost_log
from app.services.qualification.skill_growth import (
    backfill_dictionary_skills,
    vocabulary_candidates,
)
from app.services.retention import purge_expired_records
from app.supabase_pool import get_async_supabase

logger = logging.getLogger(__name__)

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
    today_usd: float = Field(description="Spend since UTC midnight (the breaker's window).")
    today_usage_pct: float | None = Field(
        description=(
            "today_usd / global_daily_cap_usd × 100. None when the cap is 0 (breaker disabled)."
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


# `async def` (#57 PR-G2c): the three ``cost_log.*_all`` aggregates now have async
# twins (``total_spend_all_async`` / ``spend_by_purpose_all_async`` /
# ``cache_metrics_all_async``), so every read runs natively on the pooled async
# service client — no ``asyncio.to_thread`` offload, no sync service client. The
# sync ``total_spend_all`` stays for the poller/ingestion-health callers (it
# imports the sync form); the sync by-purpose/cache aggregates stay for their unit
# tests.
@router.get("/cost-summary", response_model=CostSummaryResponse)
async def get_cost_summary(
    supabase: AsyncClient = Depends(get_async_service_supabase),
) -> CostSummaryResponse:
    """Operator drill-in for the LLM cost-alert breadcrumbs (#26 F4).

    No per-user partition — this is the cross-instance rollup the global
    circuit breaker reads from, plus per-purpose breakdowns the operator
    can use to spot a runaway prompt (e.g. "phase1_triage tripled
    yesterday").
    """
    now = datetime.now(UTC)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

    today_usd = await cost_log.total_spend_all_async(supabase, since=midnight)
    last_24h = await cost_log.total_spend_all_async(supabase, since=now - timedelta(hours=24))
    last_7d = await cost_log.total_spend_all_async(supabase, since=now - timedelta(days=7))
    last_30d = await cost_log.total_spend_all_async(supabase, since=now - timedelta(days=30))

    by_purpose_today: dict[str, float] = await cost_log.spend_by_purpose_all_async(
        supabase, since=midnight
    )
    by_purpose_30d: dict[str, float] = await cost_log.spend_by_purpose_all_async(
        supabase, since=now - timedelta(days=30)
    )

    cache_today = CacheStats.from_buckets(
        await cost_log.cache_metrics_all_async(supabase, since=midnight)
    )
    cache_30d = CacheStats.from_buckets(
        await cost_log.cache_metrics_all_async(supabase, since=now - timedelta(days=30))
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


class RetentionPurgeResult(BaseModel):
    """Rows deleted per operational log by a retention purge (#29 P3)."""

    llm_costs: int = Field(description="Rows deleted from llm_costs.")
    notifications_sent: int = Field(description="Rows deleted from notifications_sent.")
    search_events: int = Field(description="Rows deleted from search_events.")
    phase1_rejections: int = Field(description="Rows deleted from phase1_rejections.")


@router.post("/retention/purge", response_model=RetentionPurgeResult)
async def purge_retention() -> RetentionPurgeResult:
    """Delete operational-log rows past their retention window (#29 P3).

    Idempotent; safe to call repeatedly. Mirrors the in-process scheduler
    job (gated on ``RETENTION_PURGE_ENABLED``), exposed so external cron
    (pg_cron, GitHub Actions) can drive the purge without running
    APScheduler. Windows come from settings; a 0-day window keeps that
    log indefinitely. Runs on the async service-role client (#57 slice 1),
    same as the scheduler job.
    """
    aclient = get_async_supabase()
    if aclient is None:
        raise HTTPException(status_code=503, detail="async supabase client not initialized")
    report = await purge_expired_records(
        aclient,
        llm_costs_days=settings.llm_costs_retention_days,
        notifications_sent_days=settings.notifications_sent_retention_days,
        search_events_days=settings.search_events_retention_days,
        phase1_rejections_days=settings.phase1_rejections_retention_days,
    )
    return RetentionPurgeResult(**report)


# ---- Waitlist → invite funnel (Phase 3 slice 4) -----------------------------


class WaitlistEntry(BaseModel):
    email: str
    created_at: datetime
    #: NULL = still pending conversion.
    invited_at: datetime | None = None


class WaitlistListResponse(BaseModel):
    entries: list[WaitlistEntry]


class WaitlistInviteRequest(BaseModel):
    email: str = Field(max_length=254)


class WaitlistInviteResult(BaseModel):
    email: str
    invited: bool
    #: Whether the address came from the waitlist (an operator may also
    #: invite an address directly; the response makes the distinction
    #: auditable either way).
    from_waitlist: bool


# Module-level async helper so the handler holds no inline ``.execute()`` on the
# loop (#57 slice 4) — the CI guard scans only router handlers.
async def _fetch_waitlist(supabase: AsyncClient, *, pending: bool) -> list[Any]:
    query = (
        supabase.table("waitlist_signups").select("email,created_at,invited_at").order("created_at")
    )
    if pending:
        query = query.is_("invited_at", "null")
    resp = await query.execute()
    return resp.data or []


# Native async handler (#57 slice 4): the read runs on the pooled async service
# client instead of a threadpool worker.
@router.get("/waitlist", response_model=WaitlistListResponse)
async def list_waitlist(
    pending: bool = False,
    supabase: AsyncClient = Depends(get_async_service_supabase),
) -> WaitlistListResponse:
    """The operator's funnel view; ``?pending=true`` = not yet invited."""
    rows = await _fetch_waitlist(supabase, pending=pending)
    return WaitlistListResponse(entries=[WaitlistEntry.model_validate(r) for r in rows])


# Module-level async helpers so the handler holds no inline ``.execute()`` on the
# loop (#57 PR-G2a) — the CI guard scans only router handlers.
async def _email_on_waitlist(supabase: AsyncClient, email: str) -> bool:
    resp = await supabase.table("waitlist_signups").select("id").eq("email", email).execute()
    return bool(resp.data or [])


async def _upsert_beta_invite(supabase: AsyncClient, email: str) -> None:
    await (
        supabase.table("wyrdfold_beta_invites")
        .upsert({"email": email}, on_conflict="email", ignore_duplicates=True)
        .execute()
    )


async def _stamp_waitlist_invited(supabase: AsyncClient, email: str) -> None:
    await (
        supabase.table("waitlist_signups")
        .update({"invited_at": datetime.now(UTC).isoformat()})
        .eq("email", email)
        .execute()
    )


# Native async handler (#57 PR-G2a): the reads/writes run on the pooled async
# service client and ``auth.admin.invite_user_by_email`` is awaited (the installed
# gotrue ships the admin API as ``async def``).
@router.post("/waitlist/invite", response_model=WaitlistInviteResult)
async def invite_from_waitlist(
    body: WaitlistInviteRequest,
    supabase: AsyncClient = Depends(get_async_service_supabase),
) -> WaitlistInviteResult:
    """Convert a signup into a beta invite (Phase 3 slice 4).

    Three effects, in a deliberately safe order:
    1. upsert the address into ``wyrdfold_beta_invites`` — the
       before-user-created hook's allowlist stays the source of truth
       even though the admin invite below bypasses the hook;
    2. ``auth.admin.invite_user_by_email`` — creates the user and sends
       Supabase's invite email (magic link into ``/auth/callback``).
       Re-inviting a PENDING (unaccepted) address RESENDS the invite
       (200 — the operator's lost-email remedy); only a CONFIRMED
       account refuses, surfaced as 409 with the allowlist upsert
       already durable (harmless / idempotent);
    3. stamp ``waitlist_signups.invited_at`` when the address came from
       the waitlist, so the pending view shrinks — only after the
       invite actually went out.
    """
    from app.routers.waitlist import _EMAIL_RE  # same shape rule as signup

    email = body.email.strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=422, detail="Not a valid email address.")

    from_waitlist = await _email_on_waitlist(supabase, email)

    await _upsert_beta_invite(supabase, email)

    options: dict[str, str] = {}
    if settings.next_app_url:
        options["redirect_to"] = f"{settings.next_app_url}/auth/callback"
    try:
        await supabase.auth.admin.invite_user_by_email(email, options or None)  # type: ignore[arg-type]
    except Exception as exc:
        message = str(exc).lower()
        if "already" in message or "exists" in message or "registered" in message:
            raise HTTPException(
                status_code=409,
                detail="A user with that email is already registered.",
            ) from exc
        raise

    if from_waitlist:
        await _stamp_waitlist_invited(supabase, email)

    return WaitlistInviteResult(email=email, invited=True, from_waitlist=from_waitlist)


# ---- Signup-mode switch (Phase 3 slice 5) -----------------------------------


class SignupModeRequest(BaseModel):
    mode: Literal["closed", "open"]


class SignupModeResult(BaseModel):
    mode: str


# Module-level async helper so the handler holds no inline ``.execute()`` on the
# loop (#57 slice 4) — the CI guard scans only router handlers.
async def _upsert_signup_mode(supabase: AsyncClient, mode: str) -> None:
    await (
        supabase.table("app_settings")
        .upsert(
            {
                "key": "signup_mode",
                "value": mode,
                "updated_at": datetime.now(UTC).isoformat(),
            },
            on_conflict="key",
        )
        .execute()
    )


# Native async handler (#57 slice 4): the write runs on the pooled async service
# client instead of a threadpool worker.
@router.post("/signup-mode", response_model=SignupModeResult)
async def set_signup_mode(
    body: SignupModeRequest,
    supabase: AsyncClient = Depends(get_async_service_supabase),
) -> SignupModeResult:
    """The open-signup flip lever (Phase 3 slice 5).

    One operator call switches the before-user-created hook between the
    beta-invites allowlist ('closed') and public signup ('open') — no
    Supabase config push, no redeploy. The public `GET /signup-mode`
    and the FE follow the same row.
    """
    await _upsert_signup_mode(supabase, body.mode)
    logger.warning("signup_mode set to '%s' by operator", body.mode)
    return SignupModeResult(mode=body.mode)


@router.post("/skills/backfill")
async def skills_backfill(
    limit: int = Query(2000, ge=1, le=20000),
    only_missing: bool = Query(True),
) -> dict[str, int]:
    """Re-scan stored postings and write dictionary-extracted skills.

    FREE — no LLM, just regex over text already in the database. That is what
    makes it the "apply a vocabulary change retroactively" button: add a term
    to ``skill_dictionary``, call this with ``only_missing=false``, and every
    historical posting that mentions it becomes searchable. Idempotent.
    """
    aclient = get_async_supabase()
    if aclient is None:
        raise HTTPException(status_code=503, detail="async supabase client not initialized")
    return await backfill_dictionary_skills(aclient, limit=limit, only_missing=only_missing)


@router.get("/skills/candidates")
async def skills_candidates(limit: int = Query(40, ge=1, le=200)) -> dict[str, Any]:
    """What the skill dictionary should learn next (read-only, free).

    Three feeds: Phase-2 harvest terms the dictionary does not know (the LLM
    discovers vocabulary as a byproduct of grading we already pay for),
    unmatched single/two-word search queries (proven demand), and per-family
    coverage (the non-technical blind spot, as a monitored number).
    """
    aclient = get_async_supabase()
    if aclient is None:
        raise HTTPException(status_code=503, detail="async supabase client not initialized")
    return await vocabulary_candidates(aclient, limit=limit)


# Module-level async helper so the handler holds no inline ``.execute()`` on
# the loop (the AST guard in test_no_blocking_supabase_in_async_handlers scans
# handler bodies; this is the documented convention above).
async def _recent_catalog_health(supabase: AsyncClient, limit: int) -> list[dict[str, Any]]:
    resp = await (
        supabase.table("catalog_health_cycles")
        .select(
            "computed_at, window_started_at, new_jobs, relevant_jobs, "
            "window_truncated, live_total, "
            "pct_ungraded, pct_location_unknown, family_counts, "
            "median_admission_age_hours, top_title_tokens, "
            "tripwire_fired, tripwire_distance, tripwire_reason"
        )
        .order("computed_at", desc=True)
        .limit(limit)
        .execute()
    )
    return cast(list[dict[str, Any]], resp.data or [])


@router.get("/catalog-health")
async def catalog_health_rows(
    limit: int = Query(48, ge=1, le=500),
    supabase: AsyncClient = Depends(get_async_service_supabase),
) -> dict[str, Any]:
    """Recent catalog-health rows, newest first (#958).

    The product-level intake funnel without a DB console: window intake vs
    relevance, corpus quality percentages, the admitted-title token
    histogram, and whether the distribution tripwire fired. One row per
    recorded poll cycle; written by the poller, read here.
    """
    rows = await _recent_catalog_health(supabase, limit)
    return {"count": len(rows), "rows": rows}
