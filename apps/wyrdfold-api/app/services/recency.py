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

from supabase import AsyncClient

from app.config import settings
from app.services.db_write import poll_db_read, poll_db_write

logger = logging.getLogger(__name__)

# Decay parameters. Kept as module constants so the migration comment,
# the helper, and the tests all point at one source of truth.
RECENCY_GRACE_DAYS = 7
RECENCY_DAILY_DECAY = 0.015
RECENCY_FLOOR = 0.3

# Chunk size for the bulk recency RPC payload — it rides in a JSONB body,
# so it only needs to stay under PostgREST's request-size limits.
_RECENCY_CHUNK_SIZE = 500

# Chunk size for the ``.in_(...)`` ID reads. These IDs travel in the request
# URL (not a body), and 500 UUIDs build a ~19KB query string that Kong
# rejects with 414 "URI too long" — the #57 load test caught the refresh
# silently failing (fail-soft) on any cycle bigger than a few hundred rows.
# 150 IDs ≈ 5.7KB stays comfortably under the ~8KB default limit; matches
# the 100-200 sizing of the other in_-chunked reads (target_scoring, purge
# guard).
_RECENCY_READ_CHUNK_SIZE = 150

# Batch size for the set-based sweep RPC (#604): bounds each UPDATE's
# transaction/lock window, nothing else — the response is a single summary
# row regardless. 10k keeps a batch in the low hundreds of ms on the prod
# instance while finishing the full corpus in a few dozen calls.
_SWEEP_BATCH_SIZE = 10_000

# Keyset start for the sweep's uuid cursor.
_SWEEP_CURSOR_START = "00000000-0000-0000-0000-000000000000"


def compute_recency_multiplier(age_days: float) -> float:
    """Return the age-decay multiplier in ``[RECENCY_FLOOR, 1.0]``.

    ``age_days`` is the posting's age in days — since R2, now minus the
    provider posted date (``source_posted_at``), falling back to
    ``cataloged_at`` when the provider gave none.
    Negative ages (clock skew on a just-ingested row) clamp to the full
    multiplier. The floor means a very old posting never drops to zero —
    a strong match stays findable, just demoted.
    """
    decay_days = max(0.0, age_days - RECENCY_GRACE_DAYS)
    return max(RECENCY_FLOOR, 1.0 - decay_days * RECENCY_DAILY_DECAY)


def compute_recency_score(score: int, age_days: float, *, enabled: bool) -> int:
    """Decay ``score`` by posting age. ``enabled=False`` is an identity
    (multiplier 1.0) so the column mirrors ``score`` when the flag is
    off."""
    if not enabled:
        return score
    return round(score * compute_recency_multiplier(age_days))


def display_recency_score(score: int, posted_at: Any, now: datetime) -> int:
    """Age-decay a *displayed* score from a raw posted-date value.

    Read-time counterpart to the stored ``recency_score``: the /jobs list
    shows this (so a stale posting visibly fades) while ``raw_score`` keeps
    the undecayed fit. Because it's derived from the posted date on each
    request, it never freezes — unlike the stored column, which only
    refreshes when the poller re-touches a job, so a posting that ages off
    the boards keeps whatever decay it had at its last refresh. ``now`` is
    passed in so a whole page shares one clock read. Always decays; callers
    gate on ``settings.recency_decay_enabled``.
    """
    return compute_recency_score(score, _age_days(posted_at, now), enabled=True)


def _age_days(posted_at: Any, now: datetime) -> float:
    """Days between the posted date and ``now``. Unparseable / missing
    timestamps return 0.0 (treat as fresh — no decay) so a bad row never
    crashes the refresh pass."""
    if not posted_at:
        return 0.0
    try:
        if isinstance(posted_at, str):
            seen = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
        else:
            seen = posted_at
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, (now - seen).total_seconds() / 86400.0)


async def refresh_recency_scores_poll(supabase: AsyncClient, job_posting_ids: list[str]) -> int:
    """Recompute ``recency_score`` for every scores row of the given jobs.

    Reads each job's posted date (coalesced) to derive its age, then writes
    ``score * decay`` back to each (job, target) scores row via the
    ``bulk_update_recency_scores`` RPC. Called by the poller after the
    cycle's fit scores are settled (keyword and/or Phase 2), so the
    stored value tracks both the latest fit score and the current date.

    All queries route through the #57 seam (:func:`poll_db_read` /
    :func:`poll_db_write`), so they ride the pooled ``AsyncClient`` on the
    event loop when ``POLLER_ASYNC_DB`` is on and fall back to the sync
    client in a thread otherwise.

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
    for i in range(0, len(unique_ids), _RECENCY_READ_CHUNK_SIZE):
        chunk = unique_ids[i : i + _RECENCY_READ_CHUNK_SIZE]
        try:
            resp = await poll_db_read(
                supabase,
                lambda c, chunk=chunk: (
                    c.table("jobs").select("id, source_posted_at, cataloged_at").in_("id", chunk)
                ),
                label="recency jobs read",
            )
        except Exception:
            logger.exception("refresh_recency_scores_poll: jobs fetch failed")
            return 0
        for row in cast(list[dict[str, Any]], resp.data or []):
            posted = row.get("source_posted_at") or row.get("cataloged_at")
            age_by_job[row["id"]] = _age_days(posted, now)

    # 2. Per-(job, target) score rows → recency_score updates.
    updates: list[dict[str, Any]] = []
    for i in range(0, len(unique_ids), _RECENCY_READ_CHUNK_SIZE):
        chunk = unique_ids[i : i + _RECENCY_READ_CHUNK_SIZE]
        try:
            resp = await poll_db_read(
                supabase,
                lambda c, chunk=chunk: (
                    c.table("scores")
                    .select("id, job_posting_id, score")
                    .in_("job_posting_id", chunk)
                ),
                label="recency scores read",
            )
        except Exception:
            logger.exception("refresh_recency_scores_poll: scores fetch failed")
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
            await poll_db_write(
                supabase,
                lambda c, chunk_updates=chunk_updates: c.rpc(
                    "bulk_update_recency_scores", {"p_updates": chunk_updates}
                ),
                label="recency bulk update",
            )
            written += len(chunk_updates)
        except Exception:
            logger.exception("refresh_recency_scores_poll: bulk update failed")
    return written


async def refresh_all_recency_scores(supabase: AsyncClient) -> int:
    """Rewrite ``recency_score`` for every live (non-excluded) scores row from
    the current date — set-based, in the database (#604).

    ``refresh_recency_scores_poll`` only touches the jobs a poll cycle
    re-fetched, so a posting that ages off the boards freezes at its
    last-refresh decay while its true age keeps climbing — and the /jobs
    list sorts by that stored column, so stale rows drift out of order
    relative to the read-time displayed decay. This sweep keeps the sort key
    current for ALL live rows.

    It used to do that by walking BOTH tables through PostgREST (OFFSET-paged)
    and round-tripping every row back through ``bulk_update_recency_scores``
    — measured on prod as the DB's #1 and #2 statements by total time
    (~200k row-updates per night, most of them writing the value already
    stored). Now each batch is one ``sweep_recency_scores`` call: a
    keyset-paged, set-based UPDATE computed in SQL that skips rows whose
    stored value wouldn't change (grace-window and floored rows stop being
    rewritten nightly). The decay constants ride in as arguments, so this
    module stays their single source of truth; parity of the SQL arithmetic
    with :func:`compute_recency_score` is pinned by
    ``tests/integration/test_recency_sweep_parity.py``.

    Idempotent and safe to run on a schedule; a failed batch is logged and
    ends the sweep early (partial progress is kept — the next tick finishes
    the rest). Returns the number of rows whose stored value actually
    CHANGED — the no-op writes the old walk counted are no longer performed,
    so the scheduler's cache invalidation only fires when ordering moved.
    """
    enabled = settings.recency_decay_enabled
    written = 0
    cursor = _SWEEP_CURSOR_START
    while True:
        try:
            resp = await supabase.rpc(
                "sweep_recency_scores",
                {
                    "p_enabled": enabled,
                    "p_after_id": cursor,
                    "p_batch_size": _SWEEP_BATCH_SIZE,
                    "p_grace_days": float(RECENCY_GRACE_DAYS),
                    "p_daily_decay": RECENCY_DAILY_DECAY,
                    "p_floor": RECENCY_FLOOR,
                },
            ).execute()
        except Exception:
            logger.exception("refresh_all_recency_scores: sweep batch failed")
            break
        rows = cast(list[dict[str, Any]], resp.data or [])
        if not rows:
            break
        batch = rows[0]
        written += int(batch.get("written") or 0)
        scanned = int(batch.get("scanned") or 0)
        last_id = batch.get("last_id")
        # The function's LIMIT applies after its liveness join, so a short
        # batch means the id range is exhausted, not that dead rows thinned
        # this page.
        if scanned < _SWEEP_BATCH_SIZE or not last_id:
            break
        cursor = str(last_id)
    return written
