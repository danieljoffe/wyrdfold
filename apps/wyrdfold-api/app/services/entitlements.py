"""Plan tiers → entitlements (Phase 3 slice 2, deployment-modes epic #7).

One resolver for what a `user_profiles.plan` buys:

- **free** — BYOK: the user's own OpenRouter key pays inference (the #5
  cost firewall). No managed dollar quota (there is nothing of the host's
  to spend); the structural bound is the active-target cap.
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

from dataclasses import dataclass
from typing import Literal

from app.config import settings

Plan = Literal["free", "starter", "pro"]

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


def entitlements_for(plan: str | None) -> PlanEntitlements:
    """Resolve a plan value (unknown/None → 'free', the safe-cost posture)."""
    if plan == "starter":
        return PlanEntitlements(
            plan="starter",
            llm_key_source="host",
            monthly_billable_budget_usd=settings.starter_monthly_billable_budget_usd,
            max_active_targets=settings.starter_max_active_targets,
        )
    if plan == "pro":
        return PlanEntitlements(
            plan="pro",
            llm_key_source="host",
            monthly_billable_budget_usd=settings.pro_monthly_billable_budget_usd,
            max_active_targets=settings.pro_max_active_targets,
        )
    return PlanEntitlements(
        plan="free",
        llm_key_source="byok",
        monthly_billable_budget_usd=None,
        max_active_targets=settings.free_max_active_targets,
    )
