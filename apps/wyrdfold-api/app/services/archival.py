"""Archival lifecycle sweep (UX/IA report §5 — Stage 1 + Stage 2 A/B).

Runs from the poll scheduler (throttled to ~6h in the poller wrapper),
flag-gated behind ``settings.archival_sweep_enabled``. Two bounded phases:

**Stage 1 — 30d soft-archive (reversible).** Jobs older than
``archival_archive_after_days`` with no user engagement get ``archived_at``
stamped. Engagement = any ``user_jobs`` row whose status is something other
than ``new``/``archived`` (absent row = 'new', the #75 rule) — a job anyone
saved, drafted against, applied to, or even rejected is never auto-archived.
Archived rows drop out of the default list views (they already gate on
``archived_at IS NULL``) but stay reachable via the existing
``status='archived'`` view and direct links.

**Stage 2 — 60d cleanup (A/B split, per the §5 decision).** Rows archived
longer than ``archival_purge_after_days`` are:

* **HARD-DELETED (option A)** when they are pure noise: no engagement, no
  graded history (no ``scores`` row with a real ``fit_reasoning``), and the
  source stopped listing them (no re-upsert within
  ``archival_delist_grace_days`` — a still-listed job would just be
  re-inserted next poll). Every FK child (scores, embeddings, shadow rows,
  status log) is ``ON DELETE CASCADE``; ``documents.job_posting_id`` is
  ``ON DELETE SET NULL`` so a tailored doc is never destroyed — though an
  engaged job (which is what a doc implies) never reaches deletion anyway.
* **TOMBSTONED (option B)** otherwise: ``purged_at`` stamped and the heavy
  payload (``description_html``) stripped. The row survives as a
  re-ingestion guard — the poller refuses to re-insert purged
  (source, external) pairs — and as the history stub Insights counts.

Option C (content-strip without tombstone semantics) was explicitly skipped.

Fail-soft and IO-polite: each phase touches at most
``archival_sweep_batch`` rows per run, reads are id-level, writes are
chunked, and any error aborts the sweep (never the poll cycle) — the next
run resumes where the data says to.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from supabase import Client

from app.config import settings

logger = logging.getLogger(__name__)

# Non-engaged per-user statuses: absent user_jobs rows read as 'new' (#75),
# and a user archiving a job is a dismissal, not engagement. EVERYTHING else
# (saved, resume_draft, resume_ready, applied, interviewing, offer, rejected)
# marks the job as touched — never auto-archived, never purged.
_UNENGAGED_STATUSES = ("new", "archived")

_WRITE_CHUNK = 200


def _iso_days_ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


async def _engaged_ids(supabase: Client, job_ids: list[str]) -> set[str]:
    """The subset of ``job_ids`` that ANY user has engaged with."""
    out: set[str] = set()
    for i in range(0, len(job_ids), _WRITE_CHUNK):
        chunk = job_ids[i : i + _WRITE_CHUNK]
        resp = await asyncio.to_thread(
            supabase.table("user_jobs")
            .select("job_posting_id")
            .in_("job_posting_id", chunk)
            .not_.in_("status", list(_UNENGAGED_STATUSES))
            .execute
        )
        out.update(
            cast(str, r["job_posting_id"]) for r in cast(list[dict[str, Any]], resp.data or [])
        )
    return out


async def _graded_ids(supabase: Client, job_ids: list[str]) -> set[str]:
    """The subset of ``job_ids`` with real graded history (fit_reasoning)."""
    out: set[str] = set()
    for i in range(0, len(job_ids), _WRITE_CHUNK):
        chunk = job_ids[i : i + _WRITE_CHUNK]
        resp = await asyncio.to_thread(
            supabase.table("scores")
            .select("job_posting_id")
            .in_("job_posting_id", chunk)
            .neq("fit_reasoning", "")
            .not_.is_("fit_reasoning", "null")
            .execute
        )
        out.update(
            cast(str, r["job_posting_id"]) for r in cast(list[dict[str, Any]], resp.data or [])
        )
    return out


async def _archive_stale(supabase: Client, *, batch: int) -> int:
    """Stage 1: stamp ``archived_at`` on old, untouched, live jobs."""
    cutoff = _iso_days_ago(settings.archival_archive_after_days)
    resp = await asyncio.to_thread(
        supabase.table("jobs")
        .select("id")
        .is_("archived_at", "null")
        .lt("cataloged_at", cutoff)
        .order("cataloged_at", desc=False)
        .limit(batch)
        .execute
    )
    ids = [cast(str, r["id"]) for r in cast(list[dict[str, Any]], resp.data or [])]
    if not ids:
        return 0

    engaged = await _engaged_ids(supabase, ids)
    to_archive = [i for i in ids if i not in engaged]
    now = datetime.now(UTC).isoformat()
    for i in range(0, len(to_archive), _WRITE_CHUNK):
        chunk = to_archive[i : i + _WRITE_CHUNK]
        await asyncio.to_thread(
            supabase.table("jobs").update({"archived_at": now}).in_("id", chunk).execute
        )
    if engaged:
        logger.info(
            "Archival sweep: left %d engaged job(s) live past the %dd cutoff",
            len(engaged),
            settings.archival_archive_after_days,
        )
    return len(to_archive)


async def _purge_old(supabase: Client, *, batch: int) -> tuple[int, int]:
    """Stage 2: hard-delete (A) or tombstone (B) long-archived rows.

    Returns ``(deleted, tombstoned)``.
    """
    cutoff = _iso_days_ago(settings.archival_purge_after_days)
    resp = await asyncio.to_thread(
        supabase.table("jobs")
        .select("id, updated_at")
        .is_("purged_at", "null")
        .lt("archived_at", cutoff)
        .order("archived_at", desc=False)
        .limit(batch)
        .execute
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    if not rows:
        return 0, 0
    ids = [cast(str, r["id"]) for r in rows]

    engaged = await _engaged_ids(supabase, ids)
    graded = await _graded_ids(supabase, ids)

    listed_floor = _iso_days_ago(settings.archival_delist_grace_days)
    delete_ids: list[str] = []
    tombstone_ids: list[str] = []
    for r in rows:
        jid = cast(str, r["id"])
        if jid in engaged:
            # Engaged jobs are never purged in either form — the user's
            # pipeline history keeps its full row.
            continue
        recently_listed = cast("str | None", r.get("updated_at")) or ""
        still_listed = recently_listed > listed_floor
        if jid in graded or still_listed:
            # Worth keeping as history (graded) or would re-appear on the
            # next poll (still listed) → tombstone (B).
            tombstone_ids.append(jid)
        else:
            # Pure noise: untouched, ungraded, delisted → delete (A).
            delete_ids.append(jid)

    for i in range(0, len(delete_ids), _WRITE_CHUNK):
        chunk = delete_ids[i : i + _WRITE_CHUNK]
        await asyncio.to_thread(supabase.table("jobs").delete().in_("id", chunk).execute)

    now = datetime.now(UTC).isoformat()
    for i in range(0, len(tombstone_ids), _WRITE_CHUNK):
        chunk = tombstone_ids[i : i + _WRITE_CHUNK]
        await asyncio.to_thread(
            supabase.table("jobs")
            .update({"purged_at": now, "description_html": None})
            .in_("id", chunk)
            .execute
        )
    return len(delete_ids), len(tombstone_ids)


async def run_archival_sweep(supabase: Client) -> dict[str, int]:
    """One bounded archive + purge pass. Never raises into the caller."""
    counts = {"archived": 0, "deleted": 0, "tombstoned": 0}
    batch = settings.archival_sweep_batch
    if batch <= 0:
        return counts
    try:
        counts["archived"] = await _archive_stale(supabase, batch=batch)
        deleted, tombstoned = await _purge_old(supabase, batch=batch)
        counts["deleted"] = deleted
        counts["tombstoned"] = tombstoned
        if any(counts.values()):
            logger.info(
                "Archival sweep: archived=%d deleted=%d tombstoned=%d",
                counts["archived"],
                counts["deleted"],
                counts["tombstoned"],
            )
    except Exception:
        logger.exception("Archival sweep failed — will retry next interval")
    return counts
