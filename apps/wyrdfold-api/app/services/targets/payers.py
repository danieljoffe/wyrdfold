"""Payer resolution + budget gating for background LLM work.

Background grading (Phase-1 triage, Phase-2 fit) runs under the system
API key, outside the per-request budget gate. These helpers charge that
work to the user who activated the target (the "payer") and let the
poller skip targets whose payer has exhausted their monthly allowance.

Payer rule: the user whose ``user_targets`` link is active; if several,
the earliest-standing link wins (``created_at`` — NOT ``updated_at``,
which upserts stamp on every fit-score refresh). Tie-break ``user_id``
ascending for determinism.

A target with NO active link is not an error state — it is the app's own
catalog (the app-owned targets model): ``targets`` rows are shared catalog
entries; ``user_targets`` merely attributes them. Catalog targets' Phase-1
admission bills the INSTANCE key (``_resolve_payer_client(None)`` — the
qualification tagger's precedent), bounded by the global daily budget.
User-scoped spend (Phase 2 grading, alerts) never runs for them because
those paths key off ``user_targets`` links.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from supabase import AsyncClient

from app.config import settings
from app.services.llm import cost_log
from app.services.llm.budget import MONTHLY_WINDOW_DAYS

# Why LLM work was skipped, as a stable token the defer logs interpolate.
# These are log/diagnostic values, not a wire contract — but keep them stable,
# because grepping one out of Railway is how an operator answers "why did
# grading stop?" without a DB pass.
BlockReason = Literal[
    "llm_disabled",  # operator kill-switch on the profile
    "idle",  # unseen past settings.idle_defer_days
    "over_allowance",  # spend in the rolling window reached the cap
    "catalog_ungraded",  # no payer + grade_catalog_targets off
    "no_budget_snapshot",  # empty gate: breaker / build failure, fail-closed
]


async def resolve_target_payers(
    supabase: AsyncClient, target_ids: list[str]
) -> dict[str, str | None]:
    """Map each target id to its payer user id (or None if orphaned).

    Async on the pooled service client (#57 PR-G2e-1): the poll cycle awaits this
    on the loop instead of via a threadpool hop."""
    if not target_ids:
        return {}
    resp = await (
        supabase.table("user_targets")
        .select("target_id,user_id,created_at")
        .eq("is_active", True)
        .in_("target_id", target_ids)
        .order("created_at")
        .order("user_id")
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    payers: dict[str, str | None] = dict.fromkeys(target_ids)
    for row in rows:
        tid = row["target_id"]
        if payers.get(tid) is None:
            payers[tid] = row["user_id"]
    return payers


@dataclass(frozen=True)
class PayerBudgetGate:
    """Per-cycle snapshot of who pays for each target and which payers
    are blocked — over their monthly allowance OR idle past the defer
    threshold. Snapshot semantics: at most one cycle of drift if a
    payer's spend, links, or activity change mid-cycle — acceptable."""

    payer_by_target: dict[str, str | None] = field(default_factory=dict)
    over_budget_users: frozenset[str] = frozenset()
    idle_users: frozenset[str] = frozenset()
    disabled_users: frozenset[str] = frozenset()

    def payer_for(self, target_id: str) -> str | None:
        return self.payer_by_target.get(target_id)

    def target_blocked(self, target_id: str) -> bool:
        """True when this target's LLM work must be skipped this cycle.

        Blocked when the target HAS a payer and that payer is over
        budget, idle, or operator-disabled — grading resumes once their
        window frees up / they return / the operator re-enables them.

        A target with NO payer (no active ``user_targets`` link) is the
        app's catalog. Whether that is blocked depends on
        ``settings.grade_catalog_targets``:

        - **False (default)** — blocked. Grading a target nobody is
          pursuing bills the instance key for scores no one reads, and
          the activation fan-out re-derives them anyway
          (``bulk_title_score_for_target`` "runs at target activation so
          postings that pre-date the target still appear"). Polling is
          untouched, so the catalog keeps ingesting.
        - **True** — the pre-2026-08 behaviour: catalog admission bills
          the instance key (see the module docstring).

        HISTORY, because the naive version of this was reverted once: an
        earlier rule ("never spend money nobody will consume") starved the
        public /search corpus down to one sponsored target's family. That
        rule dropped catalog targets from the ACTIVE SET, which stopped
        their source polling — the corpus starved for lack of *ingestion*,
        not for lack of grading. This gate is narrower: it suppresses LLM
        work only, and public /search reads the ``jobs`` table while
        skipping ``scores`` entirely (``services/job_search.py``), so
        ``promising`` cannot gate the public corpus.

        EXCEPTION — the EMPTY gate stays fail-closed: ``PayerBudgetGate()``
        with no payer map is the sentinel the global circuit breaker and
        the build-failure fallback construct to refuse ALL spend for the
        cycle ("when we can't see budgets, don't spend"). Catalog
        semantics apply only within a healthy snapshot.

        Delegates to ``target_block_reason`` so the "is it blocked" and "why"
        answers are ONE branch set, not two that can drift apart. The branches
        (empty sentinel, post-snapshot activation, catalog-only, payer) live
        there.
        """
        return self.target_block_reason(target_id) is not None

    def user_blocked(self, user_id: str) -> bool:
        return self.user_block_reason(user_id) is not None

    def user_block_reason(self, user_id: str) -> BlockReason | None:
        """WHY this payer is blocked, or ``None`` when they are not.

        ``user_blocked`` answers *whether*; the defer logs need *which*. They
        used to hardcode one of the three reasons — "over monthly allowance" —
        onto a predicate that is also true for idle and operator-disabled
        payers, so an idle account was reported as out of budget and anyone
        reading the logs went hunting for a spend problem that did not exist.
        (Observed: a payer at 15% of cap, deferred purely for being unseen past
        ``idle_defer_days``, logged as over allowance.)

        Precedence mirrors ``build_budget_gate``'s own short-circuits exactly:
        disabled wins over idle, idle wins over spend — the builder skips the
        idle check for a disabled payer and skips the spend query for an idle
        one, so at most one set can hold a given user. Stated explicitly here
        so the two can't drift apart if that ever changes.
        """
        if user_id in self.disabled_users:
            return "llm_disabled"
        if user_id in self.idle_users:
            return "idle"
        if user_id in self.over_budget_users:
            return "over_allowance"
        return None

    def target_block_reason(self, target_id: str) -> BlockReason | None:
        """WHY this target's LLM work is skipped, or ``None`` when it is not.

        The reporting twin of ``target_blocked`` — same branches, in the same
        order, so the two cannot disagree about whether work is skipped. The
        two target-level reasons have no user-level equivalent: a catalog-only
        target (no payer) suppressed by ``grade_catalog_targets``, and the
        empty fail-closed sentinel the breaker / build-failure path constructs.
        """
        if not self.payer_by_target:
            return "no_budget_snapshot"
        if target_id not in self.payer_by_target:
            return None  # activated after the snapshot — unchanged fail-open
        payer = self.payer_by_target[target_id]
        if payer is None:
            return None if settings.grade_catalog_targets else "catalog_ungraded"
        return self.user_block_reason(payer)


async def build_budget_gate(supabase: AsyncClient, target_ids: list[str]) -> PayerBudgetGate:
    """Build the cycle snapshot: payers, overrides + activity, spends.

    Three queries total (payers IN, profiles IN, one spend RPC per
    distinct payer) — computed once per poll cycle, not per source/job.
    A monthly limit of 0 (global or override) disables budget gating;
    ``idle_defer_days=0`` disables idle gating. A NULL ``last_seen_at``
    (profile predating the column backfill, or no profile row) is
    treated as active — never punish missing data.

    Async on the pooled service client (#57 PR-G2e-1): payers/profiles reads and
    the per-payer spend meter all await on the loop. Logic (caps, reserves,
    idle/disabled gating) is byte-for-byte the sync original — only the DB hop
    model changed. Only the poll cycle calls it, so there is no sync twin.
    """
    payers = await resolve_target_payers(supabase, target_ids)
    distinct = sorted({p for p in payers.values() if p is not None})
    if not distinct:
        return PayerBudgetGate(payer_by_target=payers)

    overrides: dict[str, float | None] = {}
    last_seen: dict[str, str | None] = {}
    disabled: set[str] = set()
    resp = await (
        supabase.table("user_profiles")
        .select("user_id,llm_monthly_budget_usd,last_seen_at,llm_enabled")
        .in_("user_id", distinct)
        .execute()
    )
    for row in cast(list[dict[str, Any]], resp.data or []):
        overrides[row["user_id"]] = row.get("llm_monthly_budget_usd")
        last_seen[row["user_id"]] = row.get("last_seen_at")
        if row.get("llm_enabled", True) is False:
            disabled.add(row["user_id"])

    now = datetime.now(UTC)
    since = now - timedelta(days=MONTHLY_WINDOW_DAYS)
    over: set[str] = set()
    idle: set[str] = set()

    idle_cutoff = (
        now - timedelta(days=settings.idle_defer_days) if settings.idle_defer_days > 0 else None
    )
    for uid in distinct:
        if uid in disabled:
            # Operator kill-switch — skip idle/spend checks entirely.
            continue
        if idle_cutoff is not None:
            seen_raw = last_seen.get(uid)
            if seen_raw is not None:
                seen = datetime.fromisoformat(str(seen_raw).replace("Z", "+00:00"))
                if seen < idle_cutoff:
                    idle.add(uid)
                    # Idle already blocks — skip the spend query.
                    continue

        raw = overrides.get(uid)
        cap = float(raw) if raw is not None else settings.user_llm_monthly_budget_usd
        if cap <= 0:
            continue
        spent = await cost_log.total_spend_async(supabase, user_id=uid, since=since)
        if spent >= cap:
            over.add(uid)

    return PayerBudgetGate(
        payer_by_target=payers,
        over_budget_users=frozenset(over),
        idle_users=frozenset(idle),
        disabled_users=frozenset(disabled),
    )
