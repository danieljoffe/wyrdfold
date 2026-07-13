"""Phase 2 orchestration: which promising jobs to grade, and how (#6).

``score_with_phase2_and_persist`` grades one (user, target, job) tuple.
This module decides which tuples to spend Sonnet on in a given run and
in what order:

- **Gate on Phase 1.** Only jobs whose scores row is ``promising=true``
  reach the grader. ``promising IS NULL`` (Phase 1 disabled / legacy)
  and ``promising=false`` are skipped — Phase 2 inherits Phase 1's
  precision filter rather than re-deriving it.
- **Re-grade contract.** Skip a job already finished at the current
  profile version (``scoring_status == 'complete'`` AND
  ``scored_profile_version >= target.profile_version``). A profile bump
  resets the row to ``stage2`` (via ``bulk_score_for_target``), which
  re-admits it. This is the same lazy-rescore contract #502 established
  and the activate/deactivate-flicker protection depends on.
- **Daily cap.** Each target gets ``DEFAULT_DAILY_CAP`` automatic grades
  per UTC day (see ``daily_cap``). Candidates beyond the remaining quota
  stay ``promising=true`` with their keyword score — surfaced as
  "pending" and picked up on the next day's quota or on user click.
- **Progressive batching.** The first ``PHASE2_FIRST_BATCH`` candidates
  grade in one eager fan-out so the first list page fills fast; the rest
  catch up in ``PHASE2_BATCH_SIZE`` chunks. Concurrency inside a batch is
  bounded so we never open 50 Sonnet sockets at once.

This is the single entry point both the poller and the backfill script
call, so the gate / cap / batching policy lives in exactly one place.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

from supabase import Client

from app.config import settings
from app.models.experience import OptimizedPayload
from app.models.targets import JobTarget
from app.services.embeddings.prescan_gate import (
    cosine_gate_batch,
    cosine_scores_batch,
    in_prescan_holdout,
)
from app.services.fit.daily_cap import DEFAULT_DAILY_CAP, phase2_quota_remaining
from app.services.fit.score_persistence import score_with_phase2_and_persist
from app.services.fit.seniority_gate import passes_seniority_gate
from app.services.llm.client import LLMClient
from app.services.scoring import strip_html

logger = logging.getLogger(__name__)

# First batch is small so the first /jobs page renders with fresh Phase 2
# grades quickly; the remainder catches up in larger chunks. Matches the
# "FIRST batch = 20, subsequent = 50" policy in the migration plan.
PHASE2_FIRST_BATCH = 20
PHASE2_BATCH_SIZE = 50

# Cap concurrent Sonnet calls within a batch. A batch of 50 must not open
# 50 sockets at once — mirrors the poller's LLM_CONCURRENCY for Stage 3.
PHASE2_CONCURRENCY = 3

# Chunk size for the scores-state lookup IN-query (mirrors the sizing in
# target_scoring / recency so the request stays under PostgREST limits).
_STATE_CHUNK_SIZE = 500


def _needs_phase2(
    promising: bool | None,
    scoring_status: str | None,
    scored_profile_version: int | None,
    target_profile_version: int,
) -> bool:
    """Phase 2 candidacy for one (job, target) scores row.

    True iff the row is promising AND has not already been finished at the
    current-or-newer profile version. ``promising`` must be exactly
    ``True`` — ``None`` (Phase 1 off / legacy) and ``False`` are excluded.
    """
    if promising is not True:
        return False
    already_current = (
        scoring_status == "complete" and (scored_profile_version or 0) >= target_profile_version
    )
    return not already_current


def _fetch_phase2_state(
    supabase: Client, target_id: str, job_ids: list[str]
) -> dict[str, tuple[bool | None, str | None, int | None, int | None]]:
    """Read (promising, scoring_status, scored_profile_version, phase1_confidence) per job.

    Keyed by ``job_posting_id`` for this one target. Jobs with no scores
    row (Phase 1 dropped them under this target) are simply absent from
    the result and never become candidates. ``phase1_confidence`` is None
    for rows triaged before the confidence column existed — the runner's
    ordering treats None as "lowest priority" so legacy rows still grade
    but newer high-confidence rows go first.
    """
    state: dict[str, tuple[bool | None, str | None, int | None, int | None]] = {}
    for i in range(0, len(job_ids), _STATE_CHUNK_SIZE):
        chunk = job_ids[i : i + _STATE_CHUNK_SIZE]
        resp = (
            supabase.table("scores")
            .select(
                "job_posting_id, promising, scoring_status, "
                "scored_profile_version, phase1_confidence"
            )
            .eq("target_id", target_id)
            .in_("job_posting_id", chunk)
            .execute()
        )
        for row in cast(list[dict[str, Any]], resp.data or []):
            state[row["job_posting_id"]] = (
                row.get("promising"),
                row.get("scoring_status"),
                row.get("scored_profile_version"),
                row.get("phase1_confidence"),
            )
    return state


def _progressive_batches(items: list[str], first: int, rest: int) -> list[list[str]]:
    """Split ``items`` into a small first batch then larger chunks."""
    if not items:
        return []
    batches = [items[:first]]
    i = first
    while i < len(items):
        batches.append(items[i : i + rest])
        i += rest
    return batches


def _prescan_gate_applies(target_id: str) -> bool:
    """Whether the pre-scan cosine gate acts on this target (#90 staged rollout).

    The global ``prescan_gate_enabled`` must be on AND the target must be in the
    ``prescan_gate_target_ids`` allowlist. An EMPTY allowlist means **no
    targets** — NOT "all targets" — so the gate can never be silently applied to
    an unvalidated target. This matters because cosine gating is only safe
    per-target, calibrated against live Phase-2 scores: it's a coarse off-domain
    filter (0% recall loss on CX, an off-cluster exec target) that FAILS on an
    in-domain one (Frontend drops ~60% of good matches at its calibrated
    threshold, #90). Enabling the gate with an empty allowlist is therefore a
    no-op, never a gate-everything that would silently drop good matches.
    """
    if not settings.prescan_gate_enabled:
        return False
    return str(target_id) in settings.prescan_gate_target_ids_set


async def run_phase2_for_jobs(
    supabase: Client,
    llm: LLMClient,
    *,
    target: JobTarget,
    payload: OptimizedPayload,
    jobs: list[dict[str, Any]],
    user_id: str | None = None,
    cap: int = DEFAULT_DAILY_CAP,
    first_batch_size: int = PHASE2_FIRST_BATCH,
    batch_size: int = PHASE2_BATCH_SIZE,
    concurrency: int = PHASE2_CONCURRENCY,
) -> int:
    """Grade the promising, not-yet-current jobs in ``jobs`` for ``target``.

    ``jobs`` are job dicts carrying at least ``id`` and ``title`` (and
    ``description_html`` for the JD context; ``first_seen_at`` is used for
    ordering when present). Returns the number of jobs actually graded.

    Order of operations: gate + re-grade filter → order newest-first →
    trim to the remaining daily quota → progressive batches with bounded
    concurrency. Per-job failures inside ``score_with_phase2_and_persist``
    are swallowed there (return ``None``), so one bad grade never sinks
    the batch.
    """
    if not jobs:
        return 0

    # US-only corpus (#60): never (re-)grade a CONFIRMED non-US job — the write
    # side of the display read gate (``jobs.py`` ``_gate_live_us``: is_us IS NOT
    # FALSE). The L2 tagger sets ``is_us`` only AFTER a job's first grade, so
    # first grades still happen (is_us NULL passes); once a job is confirmed
    # non-US it's never re-graded. Without this a re-polled non-US job burns a
    # fresh LLM grade every cycle — ``archived_at`` does NOT gate grading (134
    # non-US jobs were graded *after* being archived in one 7-day window). Keep
    # true + null (``is not False``); drop only confirmed-false.
    us_jobs = [j for j in jobs if j.get("is_us") is not False]
    non_us_skipped = len(jobs) - len(us_jobs)
    if non_us_skipped:
        logger.info(
            "Phase 2 US gate: skipped %d confirmed-non-US job(s) for target %s",
            non_us_skipped,
            target.id,
        )
    jobs = us_jobs
    if not jobs:
        return 0

    job_by_id = {j["id"]: j for j in jobs if j.get("id")}
    job_ids = list(job_by_id)
    if not job_ids:
        return 0

    state = _fetch_phase2_state(supabase, target.id, job_ids)
    candidates = [
        jid
        for jid in job_ids
        if jid in state
        # Pass only the first 3 fields — confidence is for ordering, not gating.
        and _needs_phase2(*state[jid][:3], target.profile_version)
    ]
    if not candidates:
        return 0

    # Cheap seniority pre-gate (#902): Phase 1's promising verdict is
    # domain-oriented and permissive, so it forwards many roles whose
    # *seniority* is well below the target's — Phase 2 then spends a real grade
    # to discover the mismatch. Drop clearly-below-level titles first. Flag-off
    # by default; shadow-measured via scripts/shadow_seniority_gate.py before
    # enforcing (per the prompt/scoring-change rollout rule).
    if settings.phase2_seniority_gate_enabled and target.seniority_hint:
        kept = [
            jid
            for jid in candidates
            if passes_seniority_gate(
                job_by_id[jid].get("title") or "",
                target.seniority_hint,
                tolerance=settings.phase2_seniority_gate_tolerance,
            )
        ]
        skipped = len(candidates) - len(kept)
        if skipped:
            logger.info(
                "Phase 2 seniority gate: skipped %d/%d below-level candidate(s) "
                "for target %s (hint=%s)",
                skipped,
                len(candidates),
                target.id,
                target.seniority_hint,
            )
        candidates = kept
        if not candidates:
            return 0

    # Pre-scan cosine gate (#90 flip): admit to the LLM grade only where
    # cosine(job, target) >= the calibrated threshold, replacing the permissive
    # keyword admission (shadow data: keyword admits ~100%, cosine ~5%).
    # DATA absence fails open — a None verdict (target uncalibrated / job
    # unvectored) is admitted, so an unpopulated spine never drops jobs.
    # INFRA errors fail CLOSED — a failed vector read DEFERS the whole batch
    # to the next cycle instead of grading it ungated ("defer, never
    # admit-blind", the same contract as the Phase-1 budget gate). The
    # 2026-07-12 gated-cycle audit is why: the old error→admit-all path
    # converted IO timeouts into ~20% of a 48h sample being graded ungated.
    # A deterministic exploration holdout keeps a small slice of would-drop
    # jobs FOR grading, so the gate's false-negative rate stays measurable.
    # Flag-off by default; enabled per-target (``prescan_gate_target_ids``)
    # after calibration (#89) so a zero-loss target flips before a lossier
    # one (#90 rollout).
    cosines: dict[str, float]
    if _prescan_gate_applies(target.id):
        try:
            gate = await cosine_gate_batch(supabase, target, candidates)
        except Exception:
            logger.warning(
                "Phase 2 pre-scan gate: vector read failed for target %s — "
                "deferring %d candidate(s) to the next cycle (fail-closed; "
                "an IO error must not buy ungated grades)",
                target.id,
                len(candidates),
                exc_info=True,
            )
            return 0
        holdout = settings.prescan_gate_holdout_fraction
        kept = []
        dropped = held = 0
        for jid in candidates:
            if gate.admit(jid) is False:
                if in_prescan_holdout(jid, target.id, holdout):
                    held += 1
                    kept.append(jid)  # exploration holdout — grade to measure FN
                else:
                    dropped += 1
                continue
            kept.append(jid)  # True (>= threshold) or None (data absence) → admit
        if dropped or held:
            logger.info(
                "Phase 2 pre-scan gate: dropped %d, held %d (holdout), kept %d/%d for target %s",
                dropped,
                held,
                len(kept),
                len(candidates),
                target.id,
            )
        candidates = kept
        if not candidates:
            return 0
        # Reuse the gate's cosines for ordering — no second vector fetch.
        cosines = gate.cosines
    else:
        # Gate not enforced for this target: cosines are fetched for ORDERING
        # only, best-effort ({} on error) — a read failure here degrades
        # priority order but cannot change what gets admitted or spent.
        cosines = await cosine_scores_batch(supabase, target, candidates)

    # Order candidates so the Phase 2 daily cap goes to the highest-FIT-
    # PROBABILITY jobs first. cosine(job, target) is the strongest cheap
    # predictor of the eventual fit score — live grades show avg fit climbing
    # monotonically with cosine (≈7 → 67 across the FE band), whereas
    # phase1_confidence does NOT correlate (every confidence band grades to avg
    # fit < 20). So order by (#9):
    #   1) cosine DESC — the fit-predictive signal (missing → -1: sorts last,
    #      for an un-embedded job or an un-calibrated target).
    #   2) phase1_confidence DESC, then 3) first_seen_at DESC — tie-breakers.
    # ``first_seen_at`` is an ISO-8601 string, sortable lexically; missing
    # values sort last.

    def _priority(jid: str) -> tuple[float, int, str]:
        cos = cosines.get(jid, -1.0)
        conf = state[jid][3]  # phase1_confidence
        c = int(conf) if conf is not None else -1
        seen = job_by_id[jid].get("first_seen_at") or ""
        return (cos, c, seen)

    candidates.sort(key=_priority, reverse=True)

    # Daily cap: don't grade more than the target's remaining quota. This
    # is a soft, best-effort budget — concurrent poll cycles for the same
    # target can each read the same remaining count and slightly overshoot;
    # the cap exists to bound runaway spend, not to be exact.
    quota = phase2_quota_remaining(supabase, target.id, cap)
    if quota <= 0:
        logger.info(
            "Phase 2: target %s at daily cap; deferring %d promising job(s)",
            target.id,
            len(candidates),
        )
        return 0
    if len(candidates) > quota:
        logger.info(
            "Phase 2: target %s quota %d < %d candidates; deferring the rest",
            target.id,
            quota,
            len(candidates),
        )
        candidates = candidates[:quota]

    sem = asyncio.Semaphore(concurrency)

    async def _grade_one(job_id: str) -> bool:
        job = job_by_id[job_id]
        async with sem:
            fit = await score_with_phase2_and_persist(
                supabase,
                llm,
                payload=payload,
                target=target,
                job_posting_id=job_id,
                title=job.get("title", ""),
                jd_text=strip_html(job.get("description_html", "")),
                user_id=user_id,
            )
        return fit is not None

    graded = 0
    for batch in _progressive_batches(candidates, first_batch_size, batch_size):
        results = await asyncio.gather(*(_grade_one(jid) for jid in batch), return_exceptions=True)
        graded += sum(1 for r in results if r is True)
    return graded
