"""Plan tiers → entitlements (Phase 3 slice 2, deployment-modes epic #7).

One resolver for what a `user_profiles.plan` buys:

- **free** — BYOK: the user's own OpenRouter key pays inference (the #5
  cost firewall). No managed dollar quota (there is nothing of the host's
  to spend); the structural bound is the active-target cap.
- **trial** — managed but time-boxed (#841): host keys so a new account
  can actually use the product before paying, bounded by a total spend
  ceiling AND ``trial_days``. Counts ALL purposes against that ceiling,
  including background — see ``PlanEntitlements.quota_excluded_purposes``.
  Expiry is enforced separately by :func:`trial_expired`, upstream in the
  LLM client factory, not by the quota.
- **starter / pro** — managed: host keys, with a per-tier
  INTERACTIVE-dollar quota enforced by the existing budget guard and a
  larger active-target cap.

The plan binds only in `saas` deployment mode — self-host ignores it
entirely (the instance env key IS the owner's BYOK; behavior unchanged).

Quota accounting is interactive-only (pricing decision 2026-07-03): the
cost ledger attributes catalog/background work — title triage, per-job
fit-grading, poll scoring, embeddings — to the user whose target
triggered it, and counting that against a quota would drain it while the
user sleeps. Background cost is bounded structurally by the active-target
cap instead. `NON_BILLABLE_PURPOSES` is a BLOCKLIST (not an allowlist) on
purpose: a future purpose that nobody classifies defaults to billable —
the safe-for-cost direction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from app.config import settings

logger = logging.getLogger(__name__)

Plan = Literal["free", "trial", "starter", "pro"]

# Background/system purposes excluded from managed-tier quota accounting.
# Everything else — job_analysis, tailor.*, experience.*, target.*,
# conversation.* — is user-initiated and billable.
NON_BILLABLE_PURPOSES: tuple[str, ...] = (
    "relevance.title_triage",
    "fit.job",
    "poll_scoring",
    "qualification.tagger",
    "prescan.job_embed",
    "prescan.target_embed",
    "prescan.bootstrap_label",
)


@dataclass(frozen=True)
class PlanEntitlements:
    plan: Plan
    #: 'byok' = the user's own key is required for LLM features;
    #: 'host'  = the instance key pays, gated by the quota below.
    llm_key_source: Literal["byok", "host"]
    #: Managed interactive-dollar quota per rolling month; None = no
    #: managed quota (BYOK — their key, their bill).
    monthly_billable_budget_usd: float | None
    #: Structural bound on background cost (fit-grading scales with
    #: active targets, not with clicks).
    max_active_targets: int
    #: Purposes EXCLUDED from this plan's quota sum; None = count
    #: everything. Managed subscriptions exclude background because the
    #: subscription already pays for it; a TRIAL does not — it has no
    #: subscription behind it, so an idle trial polling a target for days
    #: must consume its own ceiling. Read straight through by
    #: ``budget.resolve_llm_quota`` so the rule lives here, next to the
    #: budget it qualifies, rather than as a branch in the resolver.
    quota_excluded_purposes: tuple[str, ...] | None = None


def entitlements_for(plan: str | None) -> PlanEntitlements:
    """Resolve a plan value (unknown/None → 'free', the safe-cost posture).

    Note 'trial' resolves here purely on the plan STRING — expiry is not
    this function's job. A lapsed trial is still entitled to what a trial
    buys; what changes is that :func:`trial_expired` refuses the LLM
    client upstream. Keeping the two apart means this stays a pure
    function of the plan and the caller decides whether the clock matters
    (the target-cap resolver, for instance, deliberately does not care).
    """
    if plan == "starter":
        return PlanEntitlements(
            plan="starter",
            llm_key_source="host",
            monthly_billable_budget_usd=settings.starter_monthly_billable_budget_usd,
            max_active_targets=settings.starter_max_active_targets,
            quota_excluded_purposes=NON_BILLABLE_PURPOSES,
        )
    if plan == "pro":
        return PlanEntitlements(
            plan="pro",
            llm_key_source="host",
            monthly_billable_budget_usd=settings.pro_monthly_billable_budget_usd,
            max_active_targets=settings.pro_max_active_targets,
            quota_excluded_purposes=NON_BILLABLE_PURPOSES,
        )
    if plan == "trial":
        return PlanEntitlements(
            plan="trial",
            llm_key_source="host",
            monthly_billable_budget_usd=settings.trial_billable_budget_usd,
            max_active_targets=settings.trial_max_active_targets,
            # None, NOT NON_BILLABLE_PURPOSES — background counts against a
            # trial. See PlanEntitlements.quota_excluded_purposes.
            quota_excluded_purposes=None,
        )
    return PlanEntitlements(
        plan="free",
        llm_key_source="byok",
        monthly_billable_budget_usd=None,
        max_active_targets=settings.free_max_active_targets,
        quota_excluded_purposes=None,
    )


def parse_trial_stamp(value: Any) -> datetime | None:
    """PostgREST timestamptz → aware ``datetime``, or None.

    Never raises. A stamp we cannot read degrades to None, which
    :func:`trial_expired` treats as "not expired" — failing closed on a
    formatting quirk would wall a user mid-evaluation, and the total spend
    ceiling still bounds them either way.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        logger.warning("unparseable trial_started_at: %r", value)
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def trial_expired(
    plan: str | None,
    trial_started_at: datetime | None,
    *,
    now: datetime | None = None,
) -> bool:
    """Has this account's trial window closed?

    False for every plan except 'trial' — a paying account has no clock,
    and 'free' is refused by the BYOK gate rather than by time.

    A NULL ``trial_started_at`` is treated as **not expired**. It is a data
    anomaly (the column defaults to ``now()``), and the two failure
    directions are not symmetric: failing closed walls a legitimate user
    out of a product they are mid-way through evaluating, while failing
    open leaves them bounded by the total spend ceiling, which is the
    control that actually matters. The duration exists to stop BACKGROUND
    spend on an account that has stopped converting, not to be the
    primary cap.
    """
    if plan != "trial" or trial_started_at is None:
        return False
    moment = now or datetime.now(UTC)
    if trial_started_at.tzinfo is None:  # tolerate a naive stamp
        trial_started_at = trial_started_at.replace(tzinfo=UTC)
    return moment - trial_started_at > timedelta(days=settings.trial_days)
