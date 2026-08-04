"""Per-target job scoring (#502).

Three-stage scoring pipeline:
  Stage 1: Title-only match (fast, inline during poll)
  Stage 2: Full JD match (async, after stage 1 passes)
  Stage 3: LLM analysis (async, for top stage-2 scores)

Stores target-specific scores in `scores`. The global score
on `jobs` = average across active targets (updated after each stage).

Consumers:
- Poller: stage 1 title scoring during poll, stage 2+3 async after
- Manual entry: stages 1+2 on insert
- Re-score endpoint: bulk re-scores when a target's profile changes
- List endpoint: fetches target scores for overlay
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, cast

from supabase import AsyncClient, Client

from app.models.schemas import JobTargetScore, ScoreBreakdown, ScoreResult, ScoringStatus
from app.models.targets import JobTarget
from app.services.db_write import poll_db_write
from app.services.jd_parser import ParsedJD, parse_jd
from app.services.scoring import score_job_with_profile, score_title_against_profile
from app.services.supabase_retry import execute_with_retry, execute_with_retry_sync
from app.services.targets import crud
from app.supabase_pool import get_async_supabase

logger = logging.getLogger(__name__)

TABLE = "scores"


def _parse_score(row: dict[str, Any]) -> JobTargetScore:
    return JobTargetScore(
        id=row["id"],
        job_posting_id=row["job_posting_id"],
        target_id=row["target_id"],
        score=row["score"],
        score_breakdown=(
            ScoreBreakdown.model_validate(row["score_breakdown"])
            if row.get("score_breakdown")
            else None
        ),
        matched_keywords=row.get("matched_keywords") or [],
        excluded=row.get("excluded", False),
        scoring_status=row.get("scoring_status", "stage1"),
        scored_profile_version=row.get("scored_profile_version", 1),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _score_row_payload(
    *,
    job_posting_id: str,
    target_id: str,
    score: int,
    breakdown: ScoreBreakdown,
    matched_keywords: list[str],
    excluded: bool,
    scoring_status: ScoringStatus,
    scored_profile_version: int = 1,
    promising: bool | None = None,
    phase1_confidence: int | None = None,
) -> dict[str, Any]:
    """Build the ``scores`` upsert row. Pure; the single source of the upsert
    payload for :func:`_upsert_score` (#57 collapsed the sync/poll scoring
    twins into one async writer)."""
    row: dict[str, Any] = {
        "job_posting_id": job_posting_id,
        "target_id": target_id,
        "score": score,
        "score_breakdown": breakdown.model_dump(),
        # matched_keywords is no longer persisted (write-only column dropped
        # in R2 — the breakdown JSONB already embeds match components).
        "excluded": excluded,
        "scoring_status": scoring_status,
        "scored_profile_version": scored_profile_version,
        # Initialise recency_score to the raw fit score (fresh-posting,
        # decay multiplier 1.0). Correct as-is when RECENCY_DECAY_ENABLED
        # is off; when on, the poller's ``refresh_recency_scores_poll`` pass
        # overwrites it with the age-decayed value later in the cycle.
        # Keeping it non-NULL means a flag flip is a pure sort change.
        "recency_score": score,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    if promising is not None:
        row["promising"] = promising
    if phase1_confidence is not None:
        row["phase1_confidence"] = phase1_confidence
    return row


def _parse_upsert_response(resp: Any) -> JobTargetScore:
    """Parse an upsert response's first row, raising when the write landed
    nothing. Shared by the sync and poll upsert paths."""
    rows = cast(list[dict[str, Any]], resp.data or [])
    if not rows:
        raise RuntimeError("Failed to upsert scores row")
    return _parse_score(rows[0])


async def _upsert_score(
    supabase: Client,
    *,
    job_posting_id: str,
    target_id: str,
    score: int,
    breakdown: ScoreBreakdown,
    matched_keywords: list[str],
    excluded: bool,
    scoring_status: ScoringStatus,
    scored_profile_version: int = 1,
    promising: bool | None = None,
    phase1_confidence: int | None = None,
    gated: bool = False,
) -> JobTargetScore:
    """Upsert a score row and return the parsed result.

    ``promising`` is the Phase 1 LLM triage verdict for this (job, target)
    pair (see ``app/services/relevance/title_triage.py``). Pass ``None``
    to leave the column untouched on re-upserts; pass ``True`` / ``False``
    to set explicitly. The default ``None`` means legacy keyword-scoring
    callsites don't need to know about Phase 1.

    ``phase1_confidence`` (0-100) is the model's certainty in its Phase 1
    verdict. Same None-leaves-untouched semantics. Used by
    ``phase2_runner`` to order candidates by phase1_confidence DESC so
    the daily Sonnet cap goes to highest-likelihood-promising jobs first.

    Idempotent upsert (``on_conflict`` matches the unique constraint), so
    retrying a Supabase HTTP/2 stream drop is safe. Two write paths, unified
    here from the #57 poll/interactive twins:

    - ``gated`` (the user-facing manual-add path, #6 R2) routes through the
      ``user_upsert_score`` SECURITY DEFINER RPC so Postgres enforces target
      ownership. Interactive + single-call, so it runs a bare async retry on
      the pooled async client — deliberately NOT the poll write-herd
      semaphore, which would head-of-line-block an interactive write behind a
      mid-cycle poll burst.
    - non-gated (poller / service-role rescore) takes the direct upsert
      through :func:`app.services.db_write.poll_db_write`, bounded by the
      cycle's write-herd semaphore.

    Both pick the pooled async client with a sync-in-thread fallback;
    ``supabase`` is that fallback client (in prod the async client always
    serves the write).
    """
    row = _score_row_payload(
        job_posting_id=job_posting_id,
        target_id=target_id,
        score=score,
        breakdown=breakdown,
        matched_keywords=matched_keywords,
        excluded=excluded,
        scoring_status=scoring_status,
        scored_profile_version=scored_profile_version,
        promising=promising,
        phase1_confidence=phase1_confidence,
    )
    resp: Any
    if gated:
        async_sb = get_async_supabase()
        if async_sb is not None:
            resp = await execute_with_retry(
                async_sb.rpc("user_upsert_score", {"p_row": row}).execute,
                label="scores upsert (gated)",
            )
        else:
            resp = await asyncio.to_thread(
                lambda: execute_with_retry_sync(
                    supabase.rpc("user_upsert_score", {"p_row": row}).execute,
                    label="scores upsert (gated)",
                )
            )
    else:
        resp = await poll_db_write(
            supabase,
            lambda c: c.table(TABLE).upsert(row, on_conflict="job_posting_id,target_id"),
            label="scores upsert",
        )
    return _parse_upsert_response(resp)


# ---- Stage 1: Title-only scoring ------------------------------------------


def _title_score_result(title: str, target: JobTarget) -> ScoreResult | None:
    """Stage 1 compute shared by :func:`score_title_and_upsert` and its poll
    twin: score the title against the target, or return ``None`` when nothing
    matched and nothing excluded (skip — no row is written)."""
    result = score_title_against_profile(
        title,
        target.scoring_profile,
        search_keywords=target.search_keywords,
    )
    if not result.matched_keywords and not result.excluded:
        return None
    return result


async def score_title_and_upsert(
    supabase: Client,
    *,
    job_posting_id: str,
    title: str,
    target: JobTarget,
) -> JobTargetScore | None:
    """Stage 1: score a job title against a target and upsert if any match.

    Returns the upserted score, or None if no keywords matched (skip).
    Service-role only — Stage 1 never gates (the ``user_upsert_score`` RPC
    path is the manual-add flow, #6 R2, which starts at Stage 2).

    The keyword compute runs in the executor, NOT on the event loop: the poll
    fans this out per (row x target), and at cycle scale that is tens of
    seconds of pure-Python scoring, which on the loop would tax every
    interactive request (measured ~+45ms p50 in the #57 load test). Only the
    IO awaits on the loop.
    """
    result = await asyncio.to_thread(_title_score_result, title, target)
    if result is None:
        return None

    return await _upsert_score(
        supabase,
        job_posting_id=job_posting_id,
        target_id=target.id,
        score=result.score,
        breakdown=result.breakdown,
        matched_keywords=result.matched_keywords,
        excluded=result.excluded,
        scoring_status="stage1",
        scored_profile_version=target.profile_version,
    )


# ---- Stage 2: Full JD scoring (existing) ----------------------------------


def _stage2_score_result(
    title: str,
    description_html: str,
    target: JobTarget,
    *,
    parsed_jd: ParsedJD | None = None,
    excluded_by_prefilter: bool = False,
) -> ScoreResult:
    """Stage 2 compute shared by :func:`score_and_upsert` and its poll twin:
    full-JD keyword scoring with the caller's prefilter exclusion OR-ed into
    ``excluded`` (see :func:`score_and_upsert` for why the OR matters)."""
    result = score_job_with_profile(
        title,
        description_html,
        target.scoring_profile,
        parsed_jd=parsed_jd,
        search_keywords=target.search_keywords,
    )
    result.excluded = result.excluded or excluded_by_prefilter
    return result


async def score_and_upsert(
    supabase: Client,
    *,
    job_posting_id: str,
    title: str,
    description_html: str,
    target: JobTarget,
    parsed_jd: ParsedJD | None = None,
    excluded_by_prefilter: bool = False,
    promising: bool | None = None,
    phase1_confidence: int | None = None,
    gated: bool = False,
) -> JobTargetScore:
    """Stage 2: score one job's full JD against one target and upsert.

    Pass ``parsed_jd`` to reuse a pre-parsed JD across multiple targets.

    ``excluded_by_prefilter`` is OR-ed with the scorer's own ``excluded``
    flag. The poller pre-computes the Phase 1 LLM verdict per
    (target, job) pair and passes it through here so a re-score triggered
    by anything (cron, deploy, learner) preserves the gate the same way
    the ingestion path does. Without this, the scorer's negative-keyword-
    only ``excluded`` overwrites prefilter exclusions on every re-score
    and the noise floor walks back up.

    ``promising`` mirrors the same verdict for persistence on
    ``scores.promising`` — Stage 2 candidate selection in Phase 2 reads
    that column. Pass ``None`` to leave the column unchanged on re-
    upserts (the default — keyword-only callers don't need to know about
    Phase 1).

    ``gated`` routes the write through the ``user_upsert_score`` ownership
    RPC (the manual-add path, #6 R2); the poller / rescore paths leave it
    False. See :func:`_upsert_score` for the write-path split.

    Compute in the executor, IO on the loop: full-JD scoring is the heaviest
    per-row compute in the cycle and the poll fans it out per (row x target),
    so running it on the loop would tax every interactive request.
    """
    result = await asyncio.to_thread(
        _stage2_score_result,
        title,
        description_html,
        target,
        parsed_jd=parsed_jd,
        excluded_by_prefilter=excluded_by_prefilter,
    )

    return await _upsert_score(
        supabase,
        job_posting_id=job_posting_id,
        target_id=target.id,
        score=result.score,
        breakdown=result.breakdown,
        matched_keywords=result.matched_keywords,
        excluded=result.excluded,
        scoring_status="stage2",
        scored_profile_version=target.profile_version,
        promising=promising,
        phase1_confidence=phase1_confidence,
        gated=gated,
    )


async def score_and_upsert_async(
    supabase: AsyncClient,
    *,
    job_posting_id: str,
    title: str,
    description_html: str,
    target: JobTarget,
    parsed_jd: ParsedJD | None = None,
    excluded_by_prefilter: bool = False,
    promising: bool | None = None,
    phase1_confidence: int | None = None,
) -> JobTargetScore:
    """Async-client twin of :func:`score_and_upsert` for the interactive
    manual-add path on the pooled async service client (#57 PR-G2d-a).

    Always **gated** — the write goes through the ``user_upsert_score`` ownership
    RPC (SEC-H2; with a service-role client ``auth.uid()`` is NULL so it takes the
    function's service-role-exempt branch). The heavy full-JD scoring stays off
    the loop in the executor; only the single RPC round-trip lands on it, natively
    on the passed async client. The sync :func:`score_and_upsert` stays for the
    poller/rescore bulk paths (non-gated, write-herd-bounded), which this twin
    deliberately does not cover.
    """
    result = await asyncio.to_thread(
        _stage2_score_result,
        title,
        description_html,
        target,
        parsed_jd=parsed_jd,
        excluded_by_prefilter=excluded_by_prefilter,
    )
    row = _score_row_payload(
        job_posting_id=job_posting_id,
        target_id=target.id,
        score=result.score,
        breakdown=result.breakdown,
        matched_keywords=result.matched_keywords,
        excluded=result.excluded,
        scoring_status="stage2",
        scored_profile_version=target.profile_version,
        promising=promising,
        phase1_confidence=phase1_confidence,
    )
    resp = await execute_with_retry(
        supabase.rpc("user_upsert_score", {"p_row": row}).execute,
        label="scores upsert (gated)",
    )
    return _parse_upsert_response(resp)


# Page size for streaming stale ``scores`` rows in ``bulk_score_for_target``.
# Kept as a module constant so peak memory is bounded to one page and so tests
# can shrink it to exercise the multi-page streaming path cheaply.
_RESCORE_BATCH_SIZE = 500


def _rescore_page_rows(
    jobs: list[dict[str, Any]],
    target: JobTarget,
    existing_promising_by_job: dict[str, bool | None],
) -> list[dict[str, Any]]:
    """Deterministically re-score one page of ``jobs`` for ``target`` and build
    the ``scores`` upsert rows.

    The shared pure compute of :func:`bulk_score_for_target` and its async twin
    :func:`bulk_score_for_target_async`, so the scoring math + the promising /
    prefilter-exclusion carry-over can never drift between the two. No IO — the
    caller owns the read/write round-trips (and the async twin off-loads this to
    a thread, #107)."""
    rows_to_upsert: list[dict[str, Any]] = []
    now = datetime.now(UTC).isoformat()
    for job in jobs:
        description_html = job.get("description_html") or ""
        parsed = parse_jd(description_html)
        result = score_job_with_profile(
            job["title"],
            description_html,
            target.scoring_profile,
            parsed_jd=parsed,
            search_keywords=target.search_keywords,
        )
        existing_promising = existing_promising_by_job.get(job["id"])
        excluded_by_prefilter = existing_promising is False
        row: dict[str, Any] = {
            "job_posting_id": job["id"],
            "target_id": target.id,
            "score": result.score,
            "score_breakdown": result.breakdown.model_dump(),
            "matched_keywords": result.matched_keywords,
            "excluded": result.excluded or excluded_by_prefilter,
            "scoring_status": "stage2",
            "scored_profile_version": target.profile_version,
            # Reset recency_score to the new raw score; the next poll
            # cycle's refresh pass re-applies age decay (see
            # ``_upsert_score`` and ``app/services/recency.py``).
            "recency_score": result.score,
            "updated_at": now,
        }
        # Pass-through ``promising`` only when it's set on the
        # existing row; ``None`` leaves the column unchanged on
        # this upsert (preserving the legacy/null state).
        if existing_promising is not None:
            row["promising"] = existing_promising
        rows_to_upsert.append(row)
    return rows_to_upsert


def bulk_score_for_target(supabase: Client, target: JobTarget) -> int:
    """Re-score stale jobs for this target. Returns count scored.

    Only fetches jobs with existing ``scores`` rows whose
    ``scored_profile_version`` is less than the target's current
    ``profile_version`` (lazy re-scoring). Used by the re-score endpoint
    when a target's profile changes.

    Skips non-pipeline-active targets entirely. Pipeline-active =
    ``app_active`` (instance floor) OR any active membership — the
    derived predicate from ``crud.is_pipeline_active`` (the
    trigger-cached flag is gone, schema audit P0). If neither arm holds,
    the re-score would just burn LLM/CPU on rows nobody will see.

    Streams the stale rows page-by-page (peak memory O(page), not
    O(catalog)) — see the loop below for why each iteration re-reads the
    first page (audit #29).
    """
    if not crud.is_pipeline_active(supabase, target.id):
        logger.info(
            "bulk_score_for_target: skipping non-pipeline-active target %s (%s)",
            target.id,
            target.label,
        )
        return 0

    batch_size = _RESCORE_BATCH_SIZE
    total_scored = 0

    # Stream the stale ``scores`` rows page-by-page and fully process each
    # page (fetch JDs → score → upsert → recompute global scores) before
    # reading the next, so peak memory is O(batch) rather than O(catalog)
    # — we never hold the entire catalog's stale ids in one list (audit
    # #29). Each page's upsert bumps ``scored_profile_version`` up to the
    # target's current version, so those rows immediately drop out of the
    # ``.lt(scored_profile_version, profile_version)`` predicate. We
    # therefore always re-read the *first* page (offset 0): the next page
    # of still-stale rows shifts into its place, and every stale row is
    # processed exactly once with no ever-growing offset and no rows
    # skipped. The ``promising`` verdict rides along in the same select so
    # the Phase 1 floor is preserved without a separate per-batch lookup.
    while True:
        resp = (
            supabase.table(TABLE)
            .select("job_posting_id, promising")
            .eq("target_id", target.id)
            .lt("scored_profile_version", target.profile_version)
            .range(0, batch_size - 1)
            .execute()
        )
        stale_rows = cast(list[dict[str, Any]], resp.data or [])
        if not stale_rows:
            break

        # Existing ``promising`` verdicts for this page, keyed by job id.
        # promising=False -> excluded stays True regardless of the scorer;
        # promising=True/None -> rely on the scorer's own ``excluded``.
        # Preserving this stops a ``profile_version`` bump (feedback
        # learner, manual /rescore) from re-admitting jobs Phase 1 dropped.
        existing_promising_by_job: dict[str, bool | None] = {
            r["job_posting_id"]: r.get("promising") for r in stale_rows
        }
        batch_ids = list(existing_promising_by_job.keys())

        # Chunk the id filter. A single ``.in_`` of up to _RESCORE_BATCH_SIZE
        # (500) ids builds a ~19 KB URL that 414s past PostgREST's URL-safe
        # bound — and here the 414 is swallowed, so a feedback thumbs-down or a
        # manual /rescore on a heavy target would silently fail to re-score.
        # Chunked .in_() reads — same bound the deleted aggregate loops used.
        jobs: list[dict[str, Any]] = []
        for i in range(0, len(batch_ids), _BATCH_CHUNK_SIZE):
            resp = (
                supabase.table("jobs")
                .select("id, title, description_html")
                .in_("id", batch_ids[i : i + _BATCH_CHUNK_SIZE])
                .execute()
            )
            jobs.extend(cast(list[dict[str, Any]], resp.data or []))
        if not jobs:
            # No backing ``jobs`` rows for this page (e.g. deleted). Without
            # an upsert these stale ``scores`` rows would keep matching the
            # ``.lt()`` filter and the first-page re-read would loop forever.
            # They aren't scorable, so stop.
            break

        rows_to_upsert = _rescore_page_rows(jobs, target, existing_promising_by_job)
        if not rows_to_upsert:
            break

        supabase.table(TABLE).upsert(
            rows_to_upsert, on_conflict="job_posting_id,target_id"
        ).execute()
        total_scored += len(rows_to_upsert)

    return total_scored


async def _is_pipeline_active_async(supabase: AsyncClient, target_id: str) -> bool:
    """Async inline of ``crud.is_pipeline_active`` (#57 PR-G2e-3 — crud stays
    sync for its non-poll callers). Same two-arm rule: the ``app_active``
    instance floor OR any active membership, awaited on the loop."""
    t_resp = await (
        supabase.table("targets").select("app_active").eq("id", target_id).limit(1).execute()
    )
    t_rows = cast(list[dict[str, Any]], t_resp.data or [])
    if not t_rows:
        return False
    if bool(t_rows[0].get("app_active")):
        return True
    m_resp = await (
        supabase.table("user_targets")
        .select("id", count="exact", head=True)  # type: ignore[arg-type]
        .eq("target_id", target_id)
        .eq("is_active", True)
        .execute()
    )
    return bool(m_resp.count or 0)


async def bulk_score_for_target_async(supabase: AsyncClient, target: JobTarget) -> int:
    """Async twin of :func:`bulk_score_for_target` (#57 PR-G2e-3).

    For the interactive feedback learner's re-score on the pooled async service
    client — the sync version stays for the operator ``/rescore`` handler and the
    CLI/backfill paths. Identical streaming re-score contract (see
    :func:`bulk_score_for_target` for why each iteration re-reads the first page)
    and identical scoring math (both build rows via :func:`_rescore_page_rows`);
    the ``scores`` / ``jobs`` reads and the upsert are awaited on the loop, and
    the heavy per-page keyword compute is off-loaded to a thread (#107) rather
    than run inline."""
    if not await _is_pipeline_active_async(supabase, target.id):
        logger.info(
            "bulk_score_for_target_async: skipping non-pipeline-active target %s (%s)",
            target.id,
            target.label,
        )
        return 0

    batch_size = _RESCORE_BATCH_SIZE
    total_scored = 0
    while True:
        resp = await (
            supabase.table(TABLE)
            .select("job_posting_id, promising")
            .eq("target_id", target.id)
            .lt("scored_profile_version", target.profile_version)
            .range(0, batch_size - 1)
            .execute()
        )
        stale_rows = cast(list[dict[str, Any]], resp.data or [])
        if not stale_rows:
            break

        existing_promising_by_job: dict[str, bool | None] = {
            r["job_posting_id"]: r.get("promising") for r in stale_rows
        }
        batch_ids = list(existing_promising_by_job.keys())

        # Chunk the id filter (414-guard) — same bound the sync path uses.
        jobs: list[dict[str, Any]] = []
        for i in range(0, len(batch_ids), _BATCH_CHUNK_SIZE):
            resp = await (
                supabase.table("jobs")
                .select("id, title, description_html")
                .in_("id", batch_ids[i : i + _BATCH_CHUNK_SIZE])
                .execute()
            )
            jobs.extend(cast(list[dict[str, Any]], resp.data or []))
        if not jobs:
            break

        # Heavy per-page keyword scoring stays off the event loop (#107).
        rows_to_upsert = await asyncio.to_thread(
            _rescore_page_rows, jobs, target, existing_promising_by_job
        )
        if not rows_to_upsert:
            break

        await (
            supabase.table(TABLE)
            .upsert(rows_to_upsert, on_conflict="job_posting_id,target_id")
            .execute()
        )
        total_scored += len(rows_to_upsert)

    return total_scored


_BATCH_CHUNK_SIZE = 100  # chunk bound for .in_() reads (414-guard)

# Page size for the retro-score title sweep over the whole ``jobs`` table —
# same 500 as the re-score pager; a narrow ``id, title`` select, keyset-paged.
_RETRO_TITLE_BATCH_SIZE = 500


def _title_score_page_rows(rows: list[dict[str, Any]], target: JobTarget) -> list[dict[str, Any]]:
    """Stage-1 title-score one page of ``jobs`` rows for ``target`` and build the
    ``scores`` upsert rows. The shared pure compute of
    :func:`bulk_title_score_for_target` and its async twin
    :func:`bulk_title_score_for_target_async`, so the title-scoring math can never
    drift between them. No IO — the caller owns the read/write round-trips (and
    the async twin off-loads this to a thread, #107)."""
    rows_to_upsert: list[dict[str, Any]] = []
    for row in rows:
        result = _title_score_result(row["title"], target)
        if result is None:
            continue
        rows_to_upsert.append(
            _score_row_payload(
                job_posting_id=row["id"],
                target_id=target.id,
                score=result.score,
                breakdown=result.breakdown,
                matched_keywords=result.matched_keywords,
                excluded=result.excluded,
                scoring_status="stage1",
                scored_profile_version=target.profile_version,
            )
        )
    return rows_to_upsert


def bulk_title_score_for_target(supabase: Client, target: JobTarget) -> int:
    """Stage-1 title-score every posting in ``jobs`` against ``target``,
    writing matches in bulk. Returns the number of score rows written.

    Used at target activation so postings that pre-date the target still
    appear under it. Replaces the per-row-upsert N+1 the activation path used
    to run: matched rows are accumulated per page and written with a SINGLE
    upsert per page. (The legacy global ``jobs.score`` recompute that used to
    follow was retired with the column — R2, schema audit Group A.)

    Keyset-paginated by ``id ASC``, not OFFSET: the old ``.range(offset, …)``
    had no ``ORDER BY``, so a concurrent poller insert could shift the window
    and make it skip / re-visit rows, and OFFSET deepens to O(N²/page) scan
    cost. Mirrors the batch shape :func:`bulk_score_for_target` already uses
    (upsert the page, then one global-score recompute per page).
    """
    written = 0
    after_id: str | None = None
    while True:
        query = (
            supabase.table("jobs").select("id, title").order("id").limit(_RETRO_TITLE_BATCH_SIZE)
        )
        if after_id is not None:
            query = query.gt("id", after_id)
        resp = query.execute()
        rows = cast(list[dict[str, Any]], resp.data or [])
        if not rows:
            break

        rows_to_upsert = _title_score_page_rows(rows, target)
        if rows_to_upsert:
            supabase.table(TABLE).upsert(
                rows_to_upsert, on_conflict="job_posting_id,target_id"
            ).execute()
            written += len(rows_to_upsert)

        if len(rows) < _RETRO_TITLE_BATCH_SIZE:
            break
        after_id = rows[-1]["id"]

    return written


async def bulk_title_score_for_target_async(supabase: AsyncClient, target: JobTarget) -> int:
    """Async twin of :func:`bulk_title_score_for_target` (#57 PR-G2e-4).

    For the target-activation pipeline on the pooled async service client — the
    sync version stays for its tests / any not-yet-async caller. Identical
    keyset-paged retro-score contract and identical scoring math (both build rows
    via :func:`_title_score_page_rows`); the ``jobs`` read and the ``scores``
    upsert are awaited on the loop, and the per-page keyword compute is off-loaded
    to a thread (#107)."""
    written = 0
    after_id: str | None = None
    while True:
        query = (
            supabase.table("jobs").select("id, title").order("id").limit(_RETRO_TITLE_BATCH_SIZE)
        )
        if after_id is not None:
            query = query.gt("id", after_id)
        resp = await query.execute()
        rows = cast(list[dict[str, Any]], resp.data or [])
        if not rows:
            break

        rows_to_upsert = await asyncio.to_thread(_title_score_page_rows, rows, target)
        if rows_to_upsert:
            await (
                supabase.table(TABLE)
                .upsert(rows_to_upsert, on_conflict="job_posting_id,target_id")
                .execute()
            )
            written += len(rows_to_upsert)

        if len(rows) < _RETRO_TITLE_BATCH_SIZE:
            break
        after_id = rows[-1]["id"]

    return written


async def get_target_scores(
    supabase: AsyncClient,
    target_id: str,
    job_posting_ids: list[str] | None = None,
) -> dict[str, JobTargetScore]:
    """Return target scores keyed by job_posting_id.

    When ``job_posting_ids`` is supplied it can be large (a caller may pass
    a full page of the list-view overlay), so the ids ride in the
    ``get_target_scores_by_ids`` RPC's ``p_ids`` jsonb body rather than an
    ``id=in.(...)`` URL filter — no URL-length limit, one round-trip (#93).
    The RPC returns SETOF scores (the same columns the old ``select("*")``
    returned); the dict is keyed by ``job_posting_id``, identical to the
    ``.in_()`` read. ``None`` keeps the unfiltered all-targets-scores read.
    An empty list returns an empty dict without relaxing to an unbounded
    SELECT.
    """
    if job_posting_ids is None:
        resp = await supabase.table(TABLE).select("*").eq("target_id", target_id).execute()
        rows = cast(list[dict[str, Any]], resp.data or [])
        return {r["job_posting_id"]: _parse_score(r) for r in rows}

    if not job_posting_ids:
        return {}

    resp = await supabase.rpc(
        "get_target_scores_by_ids",
        {"p_target_id": target_id, "p_ids": job_posting_ids},
    ).execute()
    return {
        r["job_posting_id"]: _parse_score(r) for r in cast(list[dict[str, Any]], resp.data or [])
    }


# ---- Global score aggregation ----------------------------------------------

# A row is a real (LLM) fit grade only once its scoring_status is "complete";
# stage1/stage2 rows carry a cheap keyword *placeholder* on a different scale.
# Mirrors the read-side split in routers/jobs.py (`_is_pending`).
def mark_complete(supabase: Client, job_posting_id: str) -> None:
    """Mark all target scores for a job as scoring_status='complete'.

    Called after stage 3 (LLM scoring) finishes for a job.
    """
    supabase.table(TABLE).update({"scoring_status": "complete"}).eq(
        "job_posting_id", job_posting_id
    ).execute()


async def mark_complete_poll(supabase: Client, job_posting_id: str) -> None:
    """The poller's seam twin of :func:`mark_complete` (#57 slice 2): the
    same stable-WHERE UPDATE, routed through
    :func:`app.services.db_write.poll_db_write` — see that docstring for
    backend selection. Slice 4 (#57) collapses the pair once the sync
    callers migrate.
    """
    await poll_db_write(
        supabase,
        lambda c: (
            c.table(TABLE)
            .update({"scoring_status": "complete"})
            .eq("job_posting_id", job_posting_id)
        ),
        label="scores mark-complete",
    )
