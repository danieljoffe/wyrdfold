"""Per-user LLM budget guard (defense-in-depth on cost).

Reads recent rows from ``llm_costs`` for the user and refuses new LLM
calls when a rolling hourly/daily/monthly spend cap is exceeded. API-key
callers (cron/poller/batch) are NOT checked here — background work is
charged to the target's activator and gated in the poller against the
same monthly allowance.

The guard is advisory: a single in-flight call can still push spend
*past* the cap, since cost is recorded only after the LLM returns. The
hourly window bounds the worst-case overshoot.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple, cast

from fastapi import HTTPException
from supabase import Client

from app.services.llm import cost_log

logger = logging.getLogger(__name__)

MONTHLY_WINDOW_DAYS = 30

# Threshold at which we emit "approaching the cap" warnings (#26 F3).
# Below this we stay quiet; once a user crosses it we want a Sentry trail
# so operators see the run-up BEFORE the 429 spike, not after.
_APPROACHING_CAP_FRACTION = 0.8
"""Rolling window for the monthly allowance. Rolling beats calendar-month:
no first-of-month reset stampede, and it matches how Claude's own usage
limits behave."""


class LlmAccount(NamedTuple):
    """One `user_profiles` read's worth of budget-relevant account state."""

    monthly_override_usd: float | None
    llm_enabled: bool
    plan: str | None


def get_llm_account(supabase: Client, *, user_id: str) -> LlmAccount:
    """One profile read → (monthly override, llm_enabled, plan).

    ``llm_monthly_budget_usd`` is the manual "add credits" lever — NULL
    (or no profile row) means "no override". ``llm_enabled`` is the
    operator kill-switch; missing rows default to enabled. ``plan`` feeds
    the tier resolution in :func:`resolve_llm_quota` (saas mode only).
    """
    rows = cast(
        list[dict[str, Any]],
        supabase.table("user_profiles")
        .select("llm_monthly_budget_usd,llm_enabled,plan")
        .eq("user_id", user_id)
        .execute()
        .data
        or [],
    )
    override = rows[0].get("llm_monthly_budget_usd") if rows else None
    enabled = bool(rows[0].get("llm_enabled", True)) if rows else True
    plan = cast("str | None", rows[0].get("plan")) if rows else None
    return LlmAccount(
        monthly_override_usd=(float(cast(float, override)) if override is not None else None),
        llm_enabled=enabled,
        plan=plan,
    )


class ResolvedQuota(NamedTuple):
    """The effective monthly quota for one user (see resolve_llm_quota)."""

    monthly_cap_usd: float
    llm_enabled: bool
    #: Purposes EXCLUDED from the monthly sum (managed tiers count
    #: interactive spend only); None = count everything (legacy behavior).
    monthly_excluded_purposes: tuple[str, ...] | None


def resolve_llm_quota(supabase: Client, *, user_id: str) -> ResolvedQuota:
    """Resolve the user's effective monthly quota (Phase 3 tiers).

    self_host: exactly the pre-tier behavior — the per-user override or
    the `user_llm_monthly_budget_usd` default, counting ALL purposes.

    saas:
    - a user with a stored OpenRouter key pays their own interactive
      inference (BYOK always wins in ``get_client``) → NO managed monthly
      quota (0 disables that window); the hourly/daily runaway rails and
      the operator kill-switch still apply.
    - managed tiers (starter/pro) → the per-user override if set, else
      the plan's interactive budget; the monthly sum EXCLUDES background
      purposes (the ledger attributes catalog work to the triggering
      user — counting it would drain the quota while they sleep).
    - free/unknown plan without a key → the legacy default cap; moot in
      practice because ``get_client`` refuses (402) before any spend.
    """
    from app.config import settings as s
    from app.services import entitlements as ent
    from app.services.keys import store as keys_store

    account = get_llm_account(supabase, user_id=user_id)

    if s.deployment_mode != "saas":
        cap = (
            account.monthly_override_usd
            if account.monthly_override_usd is not None
            else s.user_llm_monthly_budget_usd
        )
        return ResolvedQuota(cap, account.llm_enabled, None)

    # "Who pays" must match what get_client will actually do: only a key
    # that exists AND decrypts routes spend to the user; a broken row
    # falls back to the host key and must stay metered.
    if keys_store.has_usable_key(supabase, user_id=user_id, provider="openrouter"):
        return ResolvedQuota(0.0, account.llm_enabled, None)

    entitlement = ent.entitlements_for(account.plan)
    if entitlement.llm_key_source == "host":
        cap = (
            account.monthly_override_usd
            if account.monthly_override_usd is not None
            else cast(float, entitlement.monthly_billable_budget_usd)
        )
        return ResolvedQuota(cap, account.llm_enabled, ent.NON_BILLABLE_PURPOSES)

    cap = (
        account.monthly_override_usd
        if account.monthly_override_usd is not None
        else s.user_llm_monthly_budget_usd
    )
    return ResolvedQuota(cap, account.llm_enabled, None)


def raise_if_llm_disabled(enabled: bool) -> None:
    """403 when the operator kill-switch is off for this account."""
    if not enabled:
        raise HTTPException(
            status_code=403,
            detail={"code": "llm_disabled"},
        )


def _raise_budget_429(scope: str, limit_usd: float, spent_usd: float, *, user_id: str) -> None:
    """Raise the 429 *and* capture a Sentry warning so per-user budget
    hits are observable (#26 F2). The 429 response is the user signal;
    the Sentry breadcrumb is the operator signal — a spike of cap hits
    across many users (prompt regression, cost shift) is invisible
    without it.
    """
    logger.warning(
        "llm_budget_exceeded user=%s scope=%s spent=%.4f limit=%.2f",
        user_id,
        scope,
        spent_usd,
        limit_usd,
    )
    try:
        import sentry_sdk

        sentry_sdk.capture_message(
            f"LLM budget exceeded ({scope}): ${spent_usd:.4f} >= ${limit_usd:.2f}",
            level="warning",
        )
    except ImportError:  # pragma: no cover
        pass
    raise HTTPException(
        status_code=429,
        detail={
            "code": "llm_budget_exceeded",
            "scope": scope,
            "limit_usd": limit_usd,
            "spent_usd": spent_usd,
        },
    )


# Per-process dedup so a chatty user doesn't fire a Sentry warning on
# every request once they cross 80% — first crossing per (user, scope)
# per process restart is enough signal.
_APPROACHING_FIRED: set[tuple[str, str]] = set()


def _maybe_warn_approaching(
    *, user_id: str, scope: str, limit_usd: float, spent_usd: float
) -> None:
    """Emit a Sentry warning when spend crosses 80% of a window (#26 F3).

    The trip itself is the wrong signal to react to — by then the user is
    already 429'd. The 80% breadcrumb is the actionable layer: operator
    has a window to investigate (bad prompt? batch loop? legitimate
    growth?) before the cap actually bites.
    """
    if limit_usd <= 0:
        return
    if spent_usd < limit_usd * _APPROACHING_CAP_FRACTION:
        return
    if spent_usd >= limit_usd:
        # Cap already exceeded — the 429 path handles its own telemetry.
        return
    key = (user_id, scope)
    if key in _APPROACHING_FIRED:
        return
    _APPROACHING_FIRED.add(key)
    logger.info(
        "llm_budget_approaching user=%s scope=%s spent=%.4f limit=%.2f",
        user_id,
        scope,
        spent_usd,
        limit_usd,
    )
    try:
        import sentry_sdk

        sentry_sdk.capture_message(
            f"LLM budget approaching cap ({scope}): "
            f"${spent_usd:.4f} / ${limit_usd:.2f} "
            f"({spent_usd / limit_usd * 100:.0f}%)",
            level="warning",
        )
    except ImportError:  # pragma: no cover
        pass


def check_user_budget(
    supabase: Client,
    *,
    user_id: str,
    daily_limit_usd: float,
    hourly_limit_usd: float,
    monthly_limit_usd: float = 0.0,
    monthly_excluded_purposes: tuple[str, ...] | None = None,
    rail_excluded_purposes: tuple[str, ...] | None = None,
) -> None:
    """Raise 429 if the user has hit a rolling hourly/daily/monthly cap.

    Limits of ``0`` disable that window. Hourly is checked first so a
    spam burst trips the smaller window before exhausting the day; the
    monthly allowance is the overall ceiling.

    ``monthly_excluded_purposes`` scopes the MONTHLY sum to billable
    (interactive) spend for managed tiers (Phase 3).

    ``rail_excluded_purposes`` scopes the hourly/daily runaway rails the
    same way. The rails exist to catch loops — but the ledger attributes
    background catalog work (triage, grading, tagging) to the target's
    payer, so an all-purposes rail reliably locked the OWNER out of
    interactive features once the day's pipeline ran (2026-07-13:
    suggest-lateral 429'd at $5.12/$5 daily with almost none of it
    interactive). Interactive loops still trip the rails; background
    runaways are the GLOBAL daily budget's jurisdiction, where they are
    already caught.
    """
    now = datetime.now(UTC)

    def _window_spend(since: datetime) -> float:
        if rail_excluded_purposes:
            return cost_log.total_billable_spend(
                supabase,
                user_id=user_id,
                since=since,
                excluded_purposes=rail_excluded_purposes,
            )
        return cost_log.total_spend(supabase, user_id=user_id, since=since)

    if hourly_limit_usd > 0:
        spent_hour = _window_spend(now - timedelta(hours=1))
        if spent_hour >= hourly_limit_usd:
            _raise_budget_429("hourly", hourly_limit_usd, spent_hour, user_id=user_id)
        _maybe_warn_approaching(
            user_id=user_id,
            scope="hourly",
            limit_usd=hourly_limit_usd,
            spent_usd=spent_hour,
        )

    if daily_limit_usd > 0:
        spent_day = _window_spend(now - timedelta(hours=24))
        if spent_day >= daily_limit_usd:
            _raise_budget_429("daily", daily_limit_usd, spent_day, user_id=user_id)
        _maybe_warn_approaching(
            user_id=user_id,
            scope="daily",
            limit_usd=daily_limit_usd,
            spent_usd=spent_day,
        )

    if monthly_limit_usd > 0:
        month_since = now - timedelta(days=MONTHLY_WINDOW_DAYS)
        if monthly_excluded_purposes:
            spent_month = cost_log.total_billable_spend(
                supabase,
                user_id=user_id,
                since=month_since,
                excluded_purposes=monthly_excluded_purposes,
            )
        else:
            spent_month = cost_log.total_spend(supabase, user_id=user_id, since=month_since)
        if spent_month >= monthly_limit_usd:
            _raise_budget_429("monthly", monthly_limit_usd, spent_month, user_id=user_id)
        _maybe_warn_approaching(
            user_id=user_id,
            scope="monthly",
            limit_usd=monthly_limit_usd,
            spent_usd=spent_month,
        )


def check_daily_count(
    supabase: Client,
    *,
    user_id: str,
    purpose: str,
    limit: int,
    in_flight: int = 0,
) -> None:
    """Raise 429 if the user already has ``limit`` llm_costs rows for
    ``purpose`` in the rolling 24h window.

    Count-based companion to the $-budget: bounds chatty features (deep
    job analysis) regardless of per-call price. Cache hits never write a
    cost row, so they neither count nor get blocked. ``limit=0`` disables.

    ``in_flight`` is added to the persisted count before the comparison.
    A backgrounded run (#459) writes its cost row only ~26s later, when the
    LLM returns, so a burst of concurrent kicks would otherwise all pass a
    gate that only sees already-persisted rows and blow past ``limit``.
    Counting the runs currently in flight closes that window.
    """
    if limit <= 0:
        return
    since = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
    used = (
        supabase.table("llm_costs")
        # head=True → count only, no rows shipped (HEAD request).
        .select("id", count="exact", head=True)  # type: ignore[arg-type]
        .eq("user_id", user_id)
        .eq("purpose", purpose)
        .gte("created_at", since)
        .execute()
        .count
        or 0
    )
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
