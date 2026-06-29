"""Recency decay for job list ordering (#5).

The fit score (``scores.score``) measures match quality; it says nothing
about whether a posting is still live. ``recency_score`` is the value the
/jobs list sorts and paginates by — the fit score multiplied by an age
decay so stale postings drift down without being archived.

    final = score * max(0.3, 1 - max(0, age_days - 7) * 0.015)

- 7-day grace window at full score.
- Loses 1.5% of the multiplier per day after the grace window.
- Floors at 30% of the fit score around ~54 days old.

Daniel's call (see plan-llm-scoring-migration.md, "Recency decay
deferred"): STORE the decayed score in a column and refresh it in the
poller, rather than computing it at read time. Read-time decay breaks
the list sort (a high-fit old job sorts above a fresh one by raw score
even though its visible decayed score is lower) and a query-time
expression forces a full-table scan. A stored, indexed column lets the
list page server-side.

Feature flag ``RECENCY_DECAY_ENABLED`` (default off): when off the
multiplier is 1.0, so ``recency_score == score`` and ordering is
unchanged. The column is always written (never NULL for live rows) so
flipping the flag on is a pure sort change with no backfill gap.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, cast

from supabase import Client

from app.config import settings

logger = logging.getLogger(__name__)

# Decay parameters. Kept as module constants so the migration comment,
# the helper, and the tests all point at one source of truth.
RECENCY_GRACE_DAYS = 7
RECENCY_DAILY_DECAY = 0.015
RECENCY_FLOOR = 0.3

# Chunk size for the bulk recency RPC payload. Matches the IN-chunk
# sizing used elsewhere (target_scoring, jobs router) — keeps the JSONB
# argument well under PostgREST's request limits.
_RECENCY_CHUNK_SIZE = 500

# Page size for the full sweep's table walks. PostgREST caps a single
# response at 1000 rows by default, so the sweep pages with ``.range()``.
_RECENCY_SWEEP_PAGE_SIZE = 1000


def compute_recency_multiplier(age_days: float) -> float:
    """Return the age-decay multiplier in ``[RECENCY_FLOOR, 1.0]``.

    ``age_days`` is the posting's age (now - first_seen_at) in days.
    Negative ages (clock skew on a just-ingested row) clamp to the full
    multiplier. The floor means a very old posting never drops to zero —
    a strong match stays findable, just demoted.
    """
    decay_days = max(0.0, age_days - RECENCY_GRACE_DAYS)
    return max(RECENCY_FLOOR, 1.0 - decay_days * RECENCY_DAILY_DECAY)


def compute_recency_score(
    score: int, age_days: float, *, enabled: bool
) -> int:
    """Decay ``score`` by posting age. ``enabled=False`` is an identity
    (multiplier 1.0) so the column mirrors ``score`` when the flag is
    off."""
    if not enabled:
        return score
    return round(score * compute_recency_multiplier(age_days))


def display_recency_score(score: int, first_seen_at: Any, now: datetime) -> int:
    """Age-decay a *displayed* score from a raw ``first_seen_at`` value.

    Read-time counterpart to the stored ``recency_score``: the /jobs list
    shows this (so a stale posting visibly fades) while ``raw_score`` keeps
    the undecayed fit. Because it's derived from ``first_seen_at`` on each
    request, it never freezes — unlike the stored column, which only
    refreshes when the poller re-touches a job, so a posting that ages off
    the boards keeps whatever decay it had at its last refresh. ``now`` is
    passed in so a whole page shares one clock read. Always decays; callers
    gate on ``settings.recency_decay_enabled``.
    """
    return compute_recency_score(score, _age_days(first_seen_at, now), enabled=True)


def _age_days(first_seen_at: Any, now: datetime) -> float:
    """Days between ``first_seen_at`` and ``now``. Unparseable / missing
    timestamps return 0.0 (treat as fresh — no decay) so a bad row never
    crashes the refresh pass."""
    if not first_seen_at:
        return 0.0
    try:
        if isinstance(first_seen_at, str):
            seen = datetime.fromisoformat(first_seen_at.replace("Z", "+00:00"))
        else:
            seen = first_seen_at
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, (now - seen).total_seconds() / 86400.0)


def refresh_recency_scores(
    supabase: Client, job_posting_ids: list[str]
) -> int:
    """Recompute ``recency_score`` for every scores row of the given jobs.

    Reads each job's ``first_seen_at`` to derive its age, then writes
    ``score * decay`` back to each (job, target) scores row via the
    ``bulk_update_recency_scores`` RPC. Called by the poller after the
    cycle's fit scores are settled (keyword and/or Phase 2), so the
    stored value tracks both the latest fit score and the current date.

    Idempotent and side-effect-light: a no-op when the recency flag is
    off would still be correct (recency_score == score), but the poller
    only calls this when the flag is on to avoid the extra writes. Errors
    are logged and swallowed — a failed recency refresh must never fail a
    poll cycle. Returns the number of rows written.
    """
    if not job_posting_ids:
        return 0

    unique_ids = list(set(job_posting_ids))
    enabled = settings.recency_decay_enabled
    now = datetime.now(UTC)

    # 1. Job ages (one property per posting, shared across its targets).
    age_by_job: dict[str, float] = {}
    for i in range(0, len(unique_ids), _RECENCY_CHUNK_SIZE):
        chunk = unique_ids[i : i + _RECENCY_CHUNK_SIZE]
        try:
            resp = (
                supabase.table("jobs")
                .select("id, first_seen_at")
                .in_("id", chunk)
                .execute()
            )
        except Exception:
            logger.exception("refresh_recency_scores: jobs fetch failed")
            return 0
        for row in cast(list[dict[str, Any]], resp.data or []):
            age_by_job[row["id"]] = _age_days(row.get("first_seen_at"), now)

    # 2. Per-(job, target) score rows → recency_score updates.
    updates: list[dict[str, Any]] = []
    for i in range(0, len(unique_ids), _RECENCY_CHUNK_SIZE):
        chunk = unique_ids[i : i + _RECENCY_CHUNK_SIZE]
        try:
            resp = (
                supabase.table("scores")
                .select("id, job_posting_id, score")
                .in_("job_posting_id", chunk)
                .execute()
            )
        except Exception:
            logger.exception("refresh_recency_scores: scores fetch failed")
            return 0
        for row in cast(list[dict[str, Any]], resp.data or []):
            age = age_by_job.get(row["job_posting_id"], 0.0)
            updates.append(
                {
                    "id": row["id"],
                    "recency_score": compute_recency_score(
                        row.get("score") or 0, age, enabled=enabled
                    ),
                }
            )

    if not updates:
        return 0

    written = 0
    for i in range(0, len(updates), _RECENCY_CHUNK_SIZE):
        chunk_updates = updates[i : i + _RECENCY_CHUNK_SIZE]
        try:
            supabase.rpc(
                "bulk_update_recency_scores", {"p_updates": chunk_updates}
            ).execute()
            written += len(chunk_updates)
        except Exception:
            logger.exception("refresh_recency_scores: bulk update failed")
    return written


def _flush_recency_updates(
    supabase: Client, updates: list[dict[str, Any]]
) -> int:
    """Push one ``bulk_update_recency_scores`` RPC chunk; return rows written.

    A failed chunk is logged and counted as zero — one bad batch must not
    abort the rest of a sweep.
    """
    if not updates:
        return 0
    try:
        supabase.rpc(
            "bulk_update_recency_scores", {"p_updates": updates}
        ).execute()
        return len(updates)
    except Exception:
        logger.exception("refresh recency bulk update failed")
        return 0


def refresh_all_recency_scores(supabase: Client) -> int:
    """Rewrite ``recency_score`` for every live (non-excluded) scores row from
    the current date.

    ``refresh_recency_scores`` only touches the jobs a poll cycle re-fetched,
    so a posting that ages off the boards freezes at its last-refresh decay
    while its true age keeps climbing — and the /jobs list sorts by that
    stored column, so stale rows drift out of order relative to the read-time
    displayed decay. This sweep walks ALL live jobs to build a current age
    map, then walks ALL live score rows and recomputes ``recency_score``,
    keeping the sort key consistent with what users see.

    Paginates both table walks with ``.range()`` (PostgREST caps a single
    response at 1000 rows) ordered by ``id`` for stable paging. Flushes
    updates in RPC-sized chunks as it goes so memory stays bounded on a large
    corpus. Idempotent and safe to run on a schedule; a page-fetch error is
    logged and ends that walk early (partial progress is kept — the next tick
    finishes the rest). Returns the number of score rows written.
    """
    enabled = settings.recency_decay_enabled
    now = datetime.now(UTC)

    # 1. Current age per live (non-archived) job.
    age_by_job: dict[str, float] = {}
    start = 0
    while True:
        try:
            resp = (
                supabase.table("jobs")
                .select("id, first_seen_at")
                .is_("archived_at", "null")
                .order("id")
                .range(start, start + _RECENCY_SWEEP_PAGE_SIZE - 1)
                .execute()
            )
        except Exception:
            logger.exception("refresh_all_recency_scores: jobs page fetch failed")
            return 0
        rows = cast(list[dict[str, Any]], resp.data or [])
        for row in rows:
            age_by_job[row["id"]] = _age_days(row.get("first_seen_at"), now)
        if len(rows) < _RECENCY_SWEEP_PAGE_SIZE:
            break
        start += _RECENCY_SWEEP_PAGE_SIZE

    # 2. Walk live score rows; recompute recency_score; flush in chunks.
    updates: list[dict[str, Any]] = []
    written = 0
    start = 0
    while True:
        try:
            resp = (
                supabase.table("scores")
                .select("id, job_posting_id, score")
                .eq("excluded", False)
                .order("id")
                .range(start, start + _RECENCY_SWEEP_PAGE_SIZE - 1)
                .execute()
            )
        except Exception:
            logger.exception("refresh_all_recency_scores: scores page fetch failed")
            break
        rows = cast(list[dict[str, Any]], resp.data or [])
        for row in rows:
            age = age_by_job.get(row["job_posting_id"])
            if age is None:
                # Score for an archived/absent job — never shown in a list,
                # so leave its stored recency_score untouched.
                continue
            updates.append(
                {
                    "id": row["id"],
                    "recency_score": compute_recency_score(
                        row.get("score") or 0, age, enabled=enabled
                    ),
                }
            )
        while len(updates) >= _RECENCY_CHUNK_SIZE:
            written += _flush_recency_updates(
                supabase, updates[:_RECENCY_CHUNK_SIZE]
            )
            del updates[:_RECENCY_CHUNK_SIZE]
        if len(rows) < _RECENCY_SWEEP_PAGE_SIZE:
            break
        start += _RECENCY_SWEEP_PAGE_SIZE

    written += _flush_recency_updates(supabase, updates)
    return written
