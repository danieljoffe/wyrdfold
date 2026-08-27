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
from collections.abc import Awaitable, Callable
from typing import Any, cast

from supabase import AsyncClient

from app.config import settings
from app.models.experience import OptimizedPayload
from app.models.targets import JobTarget
from app.services.embeddings import get_default_client as get_embeddings_client
from app.services.embeddings.job_embeddings import ensure_job_vectors
from app.services.embeddings.prescan_gate import cosine_scores_batch
from app.services.fit.daily_cap import DEFAULT_DAILY_CAP, phase2_quota_remaining_async
from app.services.fit.score_persistence import score_with_phase2_and_persist
from app.services.fit.seniority_gate import passes_seniority_gate
from app.services.llm.client import LLMClient
from app.services.qualification.family_gate import passes_family_gate
from app.services.qualification.materialize import ensure_job_tags
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

# Chunk size for the scores-state lookup IN-query. Mirrors target_scoring's
# _BATCH_CHUNK_SIZE (100): a larger ``.in_`` overruns PostgREST's ~150-id
# URL-safe bound and 414s (the previous value, 500, did — despite this
# comment's claim that it stayed under the limit).
_STATE_CHUNK_SIZE = 100


def _passes_us_gate(job: dict[str, Any]) -> bool:
    """US-only corpus (#60), write side of the display read gate (``jobs.py``
    ``_gate_live_us``: is_us IS NOT FALSE). Keep true + null; drop only a
    CONFIRMED non-US verdict."""
    return job.get("is_us") is not False


def _passes_target_family_gate(target: JobTarget, job: dict[str, Any]) -> bool:
    """Family gate (#277/#278), write side. Same shared predicate as the read
    gates, keep-null both ways: an untagged job and an unclassified target both
    admit."""
    return not target.role_family or passes_family_gate(
        target.role_family, cast("str | None", job.get("role_family"))
    )


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


async def _fetch_phase2_state(
    supabase: AsyncClient, target_id: str, job_ids: list[str]
) -> dict[str, tuple[bool | None, str | None, int | None, int | None]]:
    """Read (promising, scoring_status, scored_profile_version, phase1_confidence) per job.

    Keyed by ``job_posting_id`` for this one target. Jobs with no scores
    row (Phase 1 dropped them under this target) are simply absent from
    the result and never become candidates. ``phase1_confidence`` is None
    for rows triaged before the confidence column existed — the runner's
    ordering treats None as "lowest priority" so legacy rows still grade
    but newer high-confidence rows go first.

    Awaited on the pooled async service client (#57 PR-G2e-3); the chunked
    ``.in_()`` reads yield the loop rather than blocking it.
    """
    state: dict[str, tuple[bool | None, str | None, int | None, int | None]] = {}
    for i in range(0, len(job_ids), _STATE_CHUNK_SIZE):
        chunk = job_ids[i : i + _STATE_CHUNK_SIZE]
        resp = await (
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


async def run_phase2_for_jobs(
    supabase: AsyncClient,
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
    tag_budget_exhausted: Callable[[], Awaitable[bool]] | None = None,
) -> int:
    """Grade the promising, not-yet-current jobs in ``jobs`` for ``target``.

    ``jobs`` are job dicts carrying at least ``id`` and ``title`` (and
    ``description_html`` for the JD context; the posted date is used for
    ordering when present). Returns the number of jobs actually graded.

    Order of operations: gate + re-grade filter → order by fit-probability →
    trim to the remaining daily quota → materialize tags + vectors for the
    trimmed set → re-apply the tag gates → progressive batches with bounded
    concurrency. Per-job failures inside ``score_with_phase2_and_persist``
    are swallowed there (return ``None``), so one bad grade never sinks
    the batch.

    ``tag_budget_exhausted`` is the caller's global-spend predicate, handed to
    the lazy qualification tagger below (the poller owns the meter; this module
    can't import it — see ``qualification.materialize``). ``None`` leaves the
    tagger relying on the caller's own gating.
    """
    if not jobs:
        return 0

    # US-only corpus (#60): never (re-)grade a CONFIRMED non-US job — the write
    # side of the display read gate (``jobs.py`` ``_gate_live_us``: is_us IS NOT
    # FALSE). This PRE-trim pass filters on tags from EARLIER passes (tagging is
    # lazy now, so most first-time candidates are still NULL here and pass, as
    # they always did). The same gate is RE-APPLIED after the quota trim, once
    # ``ensure_job_tags`` has bought this run's verdicts — that is where a
    # first-time non-US job is caught. Running it here too is what keeps a
    # known-non-US job from consuming a quota slot in the first place.
    # Without this a re-polled non-US job burns a fresh LLM grade every cycle —
    # ``archived_at`` does NOT gate grading (134 non-US jobs were graded
    # *after* being archived in one 7-day window). Keep true + null (``is not
    # False``); drop only confirmed-false.
    us_jobs = [j for j in jobs if _passes_us_gate(j)]
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

    # Family gate (#277/#278, write side): never spend a grade on a pair the
    # read gates (`get_target_jobs` / `_gate_off_family` / the membership
    # badge) will hide from every surface anyway. Same shared predicate,
    # keep-null both ways: an untagged job (role_family NULL — lazily tagged,
    # so this is the normal state pre-trim) and an unclassified target both
    # admit. Like the US gate above, this runs BEFORE the trim on prior-pass
    # tags and is RE-APPLIED after it on the freshly-bought ones.
    # Before this gate, hard off-family pairs burned full grades — the
    # 2026-07-30 prod audit found 55% of all promising rows were off-family,
    # each graded one pure waste (e.g. a customer_experience listing graded
    # against a frontend-engineer target, scored 0).
    if target.role_family:
        in_family = [j for j in jobs if _passes_target_family_gate(target, j)]
        family_skipped = len(jobs) - len(in_family)
        if family_skipped:
            logger.info(
                "Phase 2 family gate: skipped %d off-family job(s) for target %s (family=%s)",
                family_skipped,
                target.id,
                target.role_family,
            )
        jobs = in_family
        if not jobs:
            return 0

    job_by_id = {j["id"]: j for j in jobs if j.get("id")}
    job_ids = list(job_by_id)
    if not job_ids:
        return 0

    state = await _fetch_phase2_state(supabase, target.id, job_ids)
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

    # Lazy vector materialization (Disk IO slim-down, 2026-07-30): embeddings
    # are no longer written at ingest — this grade-time candidate set is the
    # ONLY serving reader of job vectors, so they're ensured exactly here,
    # for exactly these jobs ("only a few will ever be read"). Best-effort by
    # construction: any id that fails to materialize is simply absent and the
    # cosine paths below fail open for it, same as any missing vector.
    if settings.prescan_embed_enabled and candidates:
        await ensure_job_vectors(
            supabase,
            get_embeddings_client(),
            [job_by_id[jid] for jid in candidates],
        )

    # Cosine(job, target) for ORDERING only — best-effort ({} on error): a
    # read failure degrades priority order but cannot change what gets
    # admitted or spent. (The #90 pre-scan admission GATE that used to fork
    # here was retired in R2: its shadow-vs-outcome join showed the armed
    # threshold dropped 61.5% of promising matches; the full analysis + the
    # threshold trade-off curve are preserved in docs/decisions.md
    # 2026-07-30 "retired by its own shadow data". The cosine ORDERING below
    # survives on its own evidence.)
    cosines: dict[str, float] = await cosine_scores_batch(supabase, target, candidates)

    # Order candidates so the Phase 2 daily cap goes to the highest-FIT-
    # PROBABILITY jobs first. cosine(job, target) is the strongest cheap
    # predictor of the eventual fit score — live grades show avg fit climbing
    # monotonically with cosine (≈7 → 67 across the FE band), whereas
    # phase1_confidence does NOT correlate (every confidence band grades to avg
    # fit < 20). So order by (#9):
    #   1) cosine DESC — the fit-predictive signal (missing → -1: sorts last,
    #      for an un-embedded job or an un-calibrated target).
    #   2) phase1_confidence DESC, then 3) posted date DESC — tie-breakers.
    # The posted date is an ISO-8601 string, sortable lexically; missing
    # values sort last.

    def _priority(jid: str) -> tuple[float, int, str]:
        cos = cosines.get(jid, -1.0)
        conf = state[jid][3]  # phase1_confidence
        c = int(conf) if conf is not None else -1
        seen = job_by_id[jid].get("source_posted_at") or job_by_id[jid].get("cataloged_at") or ""
        return (cos, c, seen)

    candidates.sort(key=_priority, reverse=True)

    # Daily cap: don't grade more than the target's remaining quota. This
    # is a soft, best-effort budget — concurrent poll cycles for the same
    # target can each read the same remaining count and slightly overshoot;
    # the cap exists to bound runaway spend, not to be exact.
    quota = await phase2_quota_remaining_async(supabase, target.id, cap)
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

    # ---- Lazy qualification tagging (the ingest tagger's replacement) -------
    # Ingest is $0 LLM now: a listing's intrinsic tags are bought HERE, for
    # exactly the rows about to be graded, because grade time is the only place
    # anything consumes them ("a job is tagged exactly when first needed" — the
    # same contract ``ensure_job_vectors`` above got in the 2026-07-30 Disk IO
    # slim-down).
    #
    # PLACEMENT IS THE WHOLE POINT: this sits AFTER the ordering and the daily-
    # cap trim. Tagging before the two pre-gates would tag the FULL candidate
    # set — up to thousands of rows — to grade at most ``cap`` of them. Here the
    # set is already ≤ quota.
    #
    # ``ensure_job_tags`` patches the fresh values back into these very dicts,
    # so the gates re-applied just below see this run's verdicts. Rows the
    # tagger couldn't tag (budget, provider outage, parse failure) stay NULL and
    # pass both gates, exactly as an untagged row always has.
    if settings.qualification_enabled and candidates:
        await ensure_job_tags(
            supabase,
            [job_by_id[jid] for jid in candidates],
            budget_exhausted=tag_budget_exhausted,
        )
        # Re-apply the tag gates to the now-tagged set. A row rejected here is
        # NOT backfilled from the next candidate, so a run can grade fewer than
        # the quota — deliberate: the alternative is tagging a second tranche to
        # refill the first, which re-opens the "tag more than we grade" hole
        # this change closes.
        tagged_ok = [
            jid
            for jid in candidates
            if _passes_us_gate(job_by_id[jid])
            and _passes_target_family_gate(target, job_by_id[jid])
        ]
        dropped = len(candidates) - len(tagged_ok)
        if dropped:
            logger.info(
                "Phase 2 post-tag gates: dropped %d/%d freshly-tagged candidate(s) "
                "for target %s (confirmed non-US or off-family)",
                dropped,
                len(candidates),
                target.id,
            )
        candidates = tagged_ok
        if not candidates:
            return 0

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
