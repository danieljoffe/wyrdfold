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
    "over_daily_allowance",  # spend in the rolling 24h reached the daily cap
    "catalog_ungraded",  # no payer + grade_catalog_targets off
    "no_budget_snapshot",  # empty gate: breaker / build failure, fail-closed
]

# Which of these clear on their own, and which do not.
#
# TRANSIENT — the block lifts without anyone doing anything, so "skip it now,
# retry next cycle" is a real plan and a pipeline step may safely DROP work
# while it waits:
#   * ``over_allowance`` — the payer's rolling 30-day window frees up.
#   * ``no_budget_snapshot`` — an infrastructure failure, NOT a business state.
#     The gate is rebuilt from scratch every cycle, so the next one very likely
#     succeeds. Classifying it as persistent (an earlier draft of this did)
#     would make INGESTION fail OPEN precisely while the budget-control plane
#     is unhealthy, quietly contradicting the fail-closed doctrine the empty
#     ``PayerBudgetGate()`` sentinel exists to enforce ("when we can't see
#     budgets, don't spend"). If the snapshot fails cycle after cycle that is a
#     real outage and should surface as one — via the ingestion-health alert —
#     not be absorbed by opening admission.
#
# PERSISTENT — nothing in the poll cycle changes these. An idle payer stays
# idle until they sign in; a catalog target stays unsponsored until an operator
# flips ``grade_catalog_targets``; a disabled account stays disabled. "Retry
# next cycle" is not a plan here, it is an infinite loop — and a step that drops
# work while waiting drops it forever (prod: 50h of zero ingestion).
#
# ``over_daily_allowance`` sits here even though it is a ROLLING window like
# ``over_allowance``, which sits above. That asymmetry is deliberate, and it is
# about ADMISSION, not about how fast the window frees:
#
#   * A wrongly-ADMITTED row costs one cheap insert, now itself bounded by
#     ``intake_max_new_jobs_per_hour``.
#   * A wrongly-VETOED row is a permanent invisible loss — the listing is gone
#     from the board by the time the payer unblocks — and on a small instance
#     where one payer covers every target, it stalls the whole catalog.
#
# The costs are not symmetric, so the tie does not get broken by which label is
# more literally accurate. It is the same inversion that retired the embedding
# admission gate: tuned as a SPEND gate the threshold looked right, but as an
# ADMISSION gate a false negative is unrecoverable while a false positive is a
# rounding error. A daily ceiling is also far likelier to bind than the monthly
# one — that is the entire point of adding it — so it would be the more
# frequent vetoer of the two if classified transient.
#
# NB this leaves the two spend reasons classified differently. Left that way
# ON PURPOSE: re-classifying ``over_allowance`` would be a silent behaviour
# change to a path that is not what this setting is about, and the argument for
# doing so (spend blocks should never veto ingestion at all) deserves its own
# change with its own validation rather than riding along here.
TRANSIENT_BLOCK_REASONS: frozenset[str] = frozenset({"over_allowance", "no_budget_snapshot"})
PERSISTENT_BLOCK_REASONS: frozenset[str] = frozenset(
    {"idle", "llm_disabled", "catalog_ungraded", "over_daily_allowance"}
)


def block_is_persistent(reason: BlockReason | None) -> bool:
    """True when ``reason`` will not clear on its own within the poll cycle."""
    return reason in PERSISTENT_BLOCK_REASONS


# Persistent reasons that must NEVER veto ingestion, whatever the staged
# rollout says.
#
# ``persistent_block_admits_ingestion`` is a RELEASE valve, not a safety one:
# it ships dark because opening admission for the pre-existing persistent
# reasons releases a measured ~14,800-row backlog at once, so it wants a ramp
# and a deliberate flip. That reasoning is about VOLUME.
#
# ``over_daily_allowance`` has no backlog behind it. It is a brand-new
# condition that has never blocked anything, so admitting on it releases
# nothing and cannot surge — there is simply no staging problem to solve. What
# there IS, is the opposite risk: a daily ceiling is expected to bind far more
# often than the monthly one (that is the entire point of adding it), so
# leaving it behind the flag would make a SPEND control into the pipeline's
# most frequent ingestion veto, on a flag that is off by default. A listing
# vetoed this way is gone from the board before the payer's window frees.
#
# So the spend rail suppresses paid work and never costs a listing, on its own,
# without depending on an unrelated rollout being switched on first.
ALWAYS_ADMITS_INGESTION: frozenset[str] = frozenset({"over_daily_allowance"})


def block_admits_ingestion(reason: BlockReason | None, *, staged_rollout: bool) -> bool:
    """Whether a blocked target should still let a NEW listing be admitted.

    One place for "does this block cost us a listing", so the answer cannot
    drift from :func:`block_is_persistent`'s classification. ``staged_rollout``
    is ``settings.persistent_block_admits_ingestion``.
    """
    if reason in ALWAYS_ADMITS_INGESTION:
        return True
    return staged_rollout and block_is_persistent(reason)


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
    are blocked — over their monthly allowance, over their DAILY allowance,
    idle past the defer threshold, or operator-disabled. Snapshot semantics:
    at most one cycle of drift if a payer's spend, links, or activity change
    mid-cycle — acceptable."""

    payer_by_target: dict[str, str | None] = field(default_factory=dict)
    over_budget_users: frozenset[str] = frozenset()
    over_daily_users: frozenset[str] = frozenset()
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
        disabled wins over idle, idle wins over spend, and MONTHLY spend wins
        over DAILY — the builder skips the idle check for a disabled payer,
        skips both spend queries for an idle one, and skips the daily query for
        a payer already over monthly. So at most one set can hold a given user.
        Monthly outranks daily because it is the more severe answer to "when
        does this clear": a daily ceiling frees within 24h, a monthly one may
        not free for weeks, and reporting the shallower block would send an
        operator looking for the wrong fix. Stated explicitly here so the two
        can't drift apart if that ever changes.
        """
        if user_id in self.disabled_users:
            return "llm_disabled"
        if user_id in self.idle_users:
            return "idle"
        if user_id in self.over_budget_users:
            return "over_allowance"
        if user_id in self.over_daily_users:
            return "over_daily_allowance"
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

    Two IN reads (payers, profiles) plus up to THREE aggregate spend reads per
    distinct payer: one monthly, then — only if monthly did not already block —
    the daily background figure, which is itself a subtraction of two reads
    (total minus interactive). A payer short-circuited by disabled/idle/monthly
    costs fewer. —
    computed once per poll cycle, not per source/job. A monthly limit of 0
    (global or override) disables monthly gating; ``payer_daily_budget_usd=0``
    disables daily gating; ``idle_defer_days=0`` disables idle gating. A NULL ``last_seen_at``
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
    over_daily: set[str] = set()
    idle: set[str] = set()
    # Rolling 24h, matching the interactive daily gate's window semantics
    # (``budget.check_user_budget`` uses ``now - 24h``, not a UTC-day cliff).
    day_since = now - timedelta(hours=24)
    daily_cap = settings.payer_daily_budget_usd

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
        if cap > 0:
            spent = await cost_log.total_spend_async(supabase, user_id=uid, since=since)
            if spent >= cap:
                over.add(uid)
                # Monthly already blocks — skip the daily query. Mirrors the
                # disabled/idle short-circuits above so at most one set holds a
                # given user, which is the invariant ``user_block_reason``'s
                # documented precedence depends on.
                continue

        # Daily ceiling on BACKGROUND work. The monthly allowance bounds the
        # total but not the RATE; without this a payer can burn a month in a
        # day, and Phase 1 has no other per-user bound at all.
        if daily_cap > 0:
            # BACKGROUND spend only. Metering this with the unfiltered total
            # (an earlier draft did, and review caught it) let a user's own
            # INTERACTIVE spend exhaust the background ceiling: tailoring and
            # analysis already have their own, far larger gate in
            # ``user_llm_daily_budget_usd``, so one afternoon of tailoring
            # would silently stop that user's grading for a day. The two
            # ceilings meter disjoint sets of purposes and cannot fight.
            spent_day = await cost_log.total_background_spend_async(supabase, uid, since=day_since)
            if spent_day >= daily_cap:
                over_daily.add(uid)

    return PayerBudgetGate(
        payer_by_target=payers,
        over_budget_users=frozenset(over),
        over_daily_users=frozenset(over_daily),
        idle_users=frozenset(idle),
        disabled_users=frozenset(disabled),
    )
