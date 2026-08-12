"""Persistent Phase-1 negative-verdict store.

Durable replacement for the in-process ``_PHASE1_REJECTIONS`` dict
(#514) — see ``docs/plan-phase1-rejection-persistence.md`` for the full
diagnosis. Short version: a Phase-1-rejected title never ingests, so it
re-enters triage on every poll of its source. The dict remembered the
"no" for 24h at best and died on every deploy; with near-daily releases
the standing rejected corpus re-billed the LLM approximately daily —
measured 2026-08-12 as ~75-90% of Phase-1 volume and ~half of ALL LLM
spend. Postgres remembers across restarts; the TTL exists only to bound
staleness against prompt/model drift, not as the primary lifecycle.

Key semantics are IDENTICAL to the dict this replaces:
``(target_id, profile_version, title_norm)`` where ``title_norm`` is the
lowercase, whitespace-collapsed title. A profile edit bumps
``profile_version`` so every cached rejection misses and the target
re-judges everything under the new profile immediately. Only REJECTIONS
are stored: an admitted job ingests, and the known-external-id check
already keeps it out of future triage candidate sets.

``model`` and ``confidence`` are observability-only, NOT key columns — a
prompt or model change does not auto-invalidate. After a material change,
``delete from phase1_rejections`` is the manual reset (the corpus
re-warms over ~a day of polls).

Failure posture — the store must never break a poll cycle:
- ``fetch_rejected_titles`` fail-opens to the empty set (= every title
  is a miss and pays the LLM again). Cost, never correctness.
- ``record_rejections`` logs and swallows; a lost write re-pays one
  verdict next cycle.
Both go through the ``poll_db_read``/``poll_db_write`` seam, so they run
async-on-loop in prod, retry transient blips, and stay mockable against
the sync test client.

``phase1_rejection_ttl_hours <= 0`` disables the store entirely (both
functions become no-ops) — the same kill switch the dict honored.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from app.config import settings
from app.models.targets import JobTarget
from app.services.db_write import poll_db_read, poll_db_write

logger = logging.getLogger(__name__)

TABLE = "phase1_rejections"

# ``.in_()`` filters travel in the URL; ~150-200 UUIDs is where PostgREST
# starts returning 414 (#57). Titles are longer than UUIDs, so stay well
# under that.
_IN_CHUNK_SIZE = 50

# Rows per upsert payload. Request-body-bound (not URL-bound), so this can
# be larger than the read chunk; 500 keeps payloads comfortably small.
_UPSERT_CHUNK_SIZE = 500


def normalize_title(title: str) -> str:
    """Lowercase, whitespace-collapsed title — the cache key normalization.

    Byte-for-byte the same normalization the #514 dict used
    (``" ".join(title.lower().split())``), so persisted keys match what the
    dict would have held.
    """
    return " ".join(title.lower().split())


async def fetch_rejected_titles(
    supabase: Any, target: JobTarget, titles: Sequence[str]
) -> set[str]:
    """Normalized titles already LLM-rejected for this (target, profile).

    One chunked, indexed read per candidate batch: PK-prefix match on
    ``(target_id, profile_version)`` + ``title_norm IN (…)``, filtered to
    verdicts younger than the TTL. Callers test candidate membership via
    ``normalize_title(title) in result``.

    Fail-open: any store error returns the empty set — every candidate
    reads as a miss and is sent to the LLM, exactly as if the store did
    not exist.
    """
    if settings.phase1_rejection_ttl_hours <= 0 or not titles:
        return set()
    norms = sorted({normalize_title(t) for t in titles})
    cutoff = (
        datetime.now(UTC) - timedelta(hours=settings.phase1_rejection_ttl_hours)
    ).isoformat()
    rejected: set[str] = set()
    try:
        for i in range(0, len(norms), _IN_CHUNK_SIZE):
            chunk = norms[i : i + _IN_CHUNK_SIZE]
            resp = await poll_db_read(
                supabase,
                lambda c, chunk=chunk: (
                    c.table(TABLE)
                    .select("title_norm")
                    .eq("target_id", target.id)
                    .eq("profile_version", target.profile_version)
                    .gt("judged_at", cutoff)
                    .in_("title_norm", chunk)
                ),
                label=f"phase1 rejection read {target.id}",
            )
            rejected.update(
                row["title_norm"] for row in cast(list[dict[str, Any]], resp.data or [])
            )
    except Exception:
        logger.warning(
            "phase1 rejection store: read failed for target %s — "
            "treating %d candidate(s) as misses (re-pays the LLM)",
            target.id,
            len(titles),
            exc_info=True,
        )
        return set()
    return rejected


async def record_rejections(
    supabase: Any,
    target: JobTarget,
    rejections: Sequence[tuple[str, int | None]],
) -> None:
    """Persist raw ``promising=False`` verdicts as ``(title, confidence)``.

    Upsert keyed on the PK; a re-judgment (post-TTL or post-reset)
    refreshes ``judged_at`` — supplied explicitly because the column
    default only applies on INSERT, and a conflict-update that kept the
    original timestamp would expire the rejection on the wrong clock.

    Errors are logged, never raised: a lost write costs one re-verdict on
    the next cycle.
    """
    if settings.phase1_rejection_ttl_hours <= 0 or not rejections:
        return
    judged_at = datetime.now(UTC).isoformat()
    # Dedupe by normalized title — one source batch can carry near-duplicate
    # titles that normalize to the same key, and PostgREST rejects duplicate
    # keys within a single upsert payload.
    rows_by_key: dict[str, dict[str, Any]] = {}
    for title, confidence in rejections:
        norm = normalize_title(title)
        rows_by_key[norm] = {
            "target_id": target.id,
            "profile_version": target.profile_version,
            "title_norm": norm,
            "confidence": confidence,
            "model": settings.phase1_triage_model,
            "judged_at": judged_at,
        }
    rows = list(rows_by_key.values())
    try:
        for i in range(0, len(rows), _UPSERT_CHUNK_SIZE):
            batch = rows[i : i + _UPSERT_CHUNK_SIZE]
            await poll_db_write(
                supabase,
                lambda c, batch=batch: c.table(TABLE).upsert(
                    batch, on_conflict="target_id,profile_version,title_norm"
                ),
                label=f"phase1 rejection write {target.id}",
            )
    except Exception:
        logger.warning(
            "phase1 rejection store: write failed for target %s — "
            "%d rejection(s) not persisted (re-pays those verdicts next cycle)",
            target.id,
            len(rows),
            exc_info=True,
        )
