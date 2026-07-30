"""One-off 30-day retention sweep: delist old listings, dump their dead weight.

Operator-run acceleration of the archival lifecycle (``services/archival.py``)
with harder edges, for the 2026-07-30 "search should only show fresh listings"
pass. NOT a policy change — the scheduled sweep keeps its own env-configured
windows; this script just catches the backlog up in one run:

**Phase A — archive** every LIVE listing whose ``created_at`` is older than
``--days`` (default 30). Unlike the scheduled Stage 1, engaged jobs are
archived too ("anything older should not render in search") — archive is
reversible and archived rows stay reachable in the archived view. Pass
``--keep-engaged`` for the scheduled sweep's exemption instead.

**Phase B — tombstone** every archived, not-yet-purged listing older than
``--days`` (by ``created_at``, the listing's age — the scheduled Stage 2 waits
on ``archived_at`` age): ``purged_at`` stamped + ``description_html`` (the
heavy payload) dropped. Protected rows are skipped entirely: user engagement
(``user_jobs`` beyond new/archived) or user artifacts (``analyses``,
``job_feedback``, ``documents``). Deliberately NO option-A hard delete: with
``updated_at`` never bumped by re-polls there is no trustworthy delist signal,
and deleting a still-listed job's row drops the poller's re-ingestion guard —
the "old" posting would come back on the next poll cycle wearing a fresh
``created_at`` and rank as new in search. Tombstones keep the guard.

**Phase C — cascade for tombstoned rows** (the gap this script exists to
close: FK ``ON DELETE CASCADE`` covers hard deletes, but nothing cleans the
children of tombstoned/archived rows): delete their ``job_embeddings``
(regenerable caches) and their UNGRADED ``scores`` rows (no ``fit_reasoning``
— evaluation shells with no history value). Graded scores stay (eval/bake-off
datasets read them). ``prescan_shadow`` rows stay unless ``--include-shadow``
(the #90 keyword-vs-cosine disagreement analysis hasn't run yet).

Dry-run by default; ``--execute`` writes. Idempotent and resumable: every
phase selects by current state, so a re-run picks up where the data says.

Usage::

    cd apps/wyrdfold-api
    uv run python scripts/retention_sweep_30d.py             # dry-run report
    uv run python scripts/retention_sweep_30d.py --execute

Env required: ``SUPABASE_URL`` + ``SUPABASE_SERVICE_ROLE_KEY``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from postgrest.exceptions import APIError
from supabase import Client

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.services.archival import _engaged_ids
from app.supabase_pool import get_supabase_pool, init_supabase

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("retention_sweep_30d")

PAGE_SIZE = 1000
# in_() URL-encodes ~36 bytes per UUID; keep chunks small (#57 lesson).
IN_CHUNK = 100
# job_embeddings deletes touch the 800MB HNSW-indexed table — after the bulk
# tombstone/score writes ahead of them, 100-job chunks crossed the 8s
# statement timeout (57014, prod 2026-07-30). Quarter-size + retry instead.
EMB_CHUNK = 25
WRITE_SLEEP_S = 0.15


async def _write_with_retry(fn: Any, *, what: str, attempts: int = 4) -> Any:
    """Run a blocking PostgREST write, backing off on 57014 statement
    timeouts (the small-instance breaker lesson) instead of aborting the
    whole resumable sweep."""
    for attempt in range(attempts):
        try:
            return await asyncio.to_thread(fn)
        except APIError as e:
            if getattr(e, "code", None) != "57014" or attempt == attempts - 1:
                raise
            wait = 2 * (2**attempt)
            logger.info("  %s hit 57014 — backing off %ds", what, wait)
            await asyncio.sleep(wait)
    return None  # unreachable

# Tables whose mere reference to a job marks it a user artifact (protected
# from tombstoning; engagement via user_jobs is checked separately).
_ARTIFACT_TABLES = ("analyses", "job_feedback", "documents")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _cutoff_iso(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def _page_ids(supabase: Client, *, build: Any) -> list[dict[str, Any]]:
    """Drain a jobs query via range() pagination. ``build(offset)`` returns an
    executable PostgREST request for that page."""
    out: list[dict[str, Any]] = []
    offset = 0
    while True:
        resp = build(offset).execute()
        page = cast(list[dict[str, Any]], resp.data or [])
        out.extend(page)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return out


async def _artifact_ids(supabase: Client, job_ids: list[str]) -> set[str]:
    """Jobs referenced by any user artifact table."""
    out: set[str] = set()
    for table in _ARTIFACT_TABLES:
        for i in range(0, len(job_ids), IN_CHUNK):
            chunk = job_ids[i : i + IN_CHUNK]
            resp = await asyncio.to_thread(
                supabase.table(table)
                .select("job_posting_id")
                .in_("job_posting_id", chunk)
                .execute
            )
            out.update(
                cast(str, r["job_posting_id"])
                for r in cast(list[dict[str, Any]], resp.data or [])
                if r.get("job_posting_id")
            )
    return out


async def _phase_a_archive(
    supabase: Client, *, cutoff: str, keep_engaged: bool, execute: bool
) -> list[str]:
    """Archive live rows older than the cutoff. Returns the archived ids."""
    rows = _page_ids(
        supabase,
        build=lambda off: (
            supabase.table("jobs")
            .select("id, title, company_name, created_at")
            .is_("archived_at", "null")
            .is_("purged_at", "null")
            .lt("created_at", cutoff)
            .order("created_at", desc=False)
            .range(off, off + PAGE_SIZE - 1)
        ),
    )
    ids = [cast(str, r["id"]) for r in rows]
    engaged = await _engaged_ids(supabase, ids) if ids else set()

    logger.info("Phase A — live rows older than cutoff: %d (engaged: %d)", len(ids), len(engaged))
    for r in rows[:50]:
        marker = " [ENGAGED]" if r["id"] in engaged else ""
        logger.info(
            "  %s  %s — %s (%s)%s",
            cast(str, r["created_at"])[:10],
            r.get("title"),
            r.get("company_name"),
            cast(str, r["id"])[:8],
            marker,
        )
    if len(rows) > 50:
        logger.info("  … and %d more", len(rows) - 50)

    to_archive = [i for i in ids if not (keep_engaged and i in engaged)]
    if keep_engaged and engaged:
        logger.info("  --keep-engaged: leaving %d engaged row(s) live", len(engaged))
    if not execute or not to_archive:
        return to_archive

    now = _now_iso()
    for i in range(0, len(to_archive), IN_CHUNK):
        chunk = to_archive[i : i + IN_CHUNK]
        await _write_with_retry(
            supabase.table("jobs").update({"archived_at": now}).in_("id", chunk).execute,
            what="archive",
        )
        await asyncio.sleep(WRITE_SLEEP_S)
    logger.info("  archived %d row(s)", len(to_archive))
    return to_archive


async def _phase_b_tombstone(
    supabase: Client, *, cutoff: str, execute: bool, pending_archived: list[str]
) -> list[str]:
    """Tombstone archived rows older than the cutoff. Returns tombstoned ids.
    On dry-run, ``pending_archived`` (Phase A's would-do set) joins the
    candidate pool — on execute those rows are already stamped by the time
    this phase queries."""
    rows = _page_ids(
        supabase,
        build=lambda off: (
            supabase.table("jobs")
            .select("id")
            .not_.is_("archived_at", "null")
            .is_("purged_at", "null")
            .lt("created_at", cutoff)
            .order("created_at", desc=False)
            .range(off, off + PAGE_SIZE - 1)
        ),
    )
    ids = list(dict.fromkeys([cast(str, r["id"]) for r in rows] + pending_archived))
    engaged = await _engaged_ids(supabase, ids) if ids else set()
    artifacts = await _artifact_ids(supabase, ids) if ids else set()
    protected = engaged | artifacts
    to_tombstone = [i for i in ids if i not in protected]

    logger.info(
        "Phase B — archived rows to tombstone: %d (protected: %d = engaged %d + artifact %d)",
        len(to_tombstone),
        len(protected),
        len(engaged),
        len(artifacts),
    )
    if not execute or not to_tombstone:
        return to_tombstone

    now = _now_iso()
    done = 0
    for i in range(0, len(to_tombstone), IN_CHUNK):
        chunk = to_tombstone[i : i + IN_CHUNK]
        await _write_with_retry(
            supabase.table("jobs")
            .update({"purged_at": now, "description_html": None})
            .in_("id", chunk)
            .execute,
            what="tombstone",
        )
        done += len(chunk)
        if done % 2000 < IN_CHUNK:
            logger.info("  tombstoned %d/%d", done, len(to_tombstone))
        await asyncio.sleep(WRITE_SLEEP_S)
    logger.info("  tombstoned %d row(s)", len(to_tombstone))
    return to_tombstone


async def _phase_c_cascade(
    supabase: Client,
    *,
    pending_tombstone: list[str],
    include_shadow: bool,
    execute: bool,
) -> dict[str, int]:
    """Delete embeddings + ungraded scores (+ shadow, opt-in) of ALL
    tombstoned jobs — this run's and any earlier ones. On dry-run,
    ``pending_tombstone`` (Phase B's would-do set) stands in for the rows
    Phase B hasn't actually stamped, so counts reflect the real plan."""
    rows = _page_ids(
        supabase,
        build=lambda off: (
            supabase.table("jobs")
            .select("id")
            .not_.is_("purged_at", "null")
            .order("created_at", desc=False)
            .range(off, off + PAGE_SIZE - 1)
        ),
    )
    tomb_ids = list(dict.fromkeys([cast(str, r["id"]) for r in rows] + pending_tombstone))
    counts = {"embeddings_deleted": 0, "scores_deleted": 0, "shadow_deleted": 0}
    logger.info("Phase C — tombstoned jobs to cascade: %d", len(tomb_ids))
    if not tomb_ids:
        return counts

    # Ungraded scores: fetch per job-chunk, filter in Python with the exact
    # inverse of archival._graded_ids' "real fit_reasoning" test. Half-size
    # chunks: a job can carry one scores row per target, and a full chunk's
    # rows must stay under PostgREST's 1000-row response cap.
    ungraded_score_ids: list[str] = []
    graded_kept = 0
    scan_chunk = IN_CHUNK // 2
    for i in range(0, len(tomb_ids), scan_chunk):
        chunk = tomb_ids[i : i + scan_chunk]
        resp = await asyncio.to_thread(
            supabase.table("scores")
            .select("id, fit_reasoning")
            .in_("job_posting_id", chunk)
            .execute
        )
        for s in cast(list[dict[str, Any]], resp.data or []):
            if (s.get("fit_reasoning") or "").strip():
                graded_kept += 1
            else:
                ungraded_score_ids.append(cast(str, s["id"]))
    logger.info(
        "  scores on tombstoned jobs: %d ungraded (delete), %d graded (keep)",
        len(ungraded_score_ids),
        graded_kept,
    )

    if execute:
        done = 0
        for i in range(0, len(ungraded_score_ids), IN_CHUNK):
            chunk = ungraded_score_ids[i : i + IN_CHUNK]
            await _write_with_retry(
                supabase.table("scores").delete().in_("id", chunk).execute,
                what="scores delete",
            )
            done += len(chunk)
            if done % 5000 < IN_CHUNK:
                logger.info("  scores deleted %d/%d", done, len(ungraded_score_ids))
            await asyncio.sleep(WRITE_SLEEP_S)
        counts["scores_deleted"] = done
    else:
        counts["scores_deleted"] = len(ungraded_score_ids)

    for table, key, enabled in (
        ("job_embeddings", "embeddings_deleted", True),
        ("prescan_shadow", "shadow_deleted", include_shadow),
    ):
        if not enabled:
            logger.info("  %s: skipped (opt-in via --include-shadow)", table)
            continue
        done = 0
        for i in range(0, len(tomb_ids), EMB_CHUNK):
            chunk = tomb_ids[i : i + EMB_CHUNK]
            if execute:
                resp = await _write_with_retry(
                    supabase.table(table)
                    .delete(count="exact")
                    .in_("job_posting_id", chunk)
                    .execute,
                    what=f"{table} delete",
                )
                await asyncio.sleep(WRITE_SLEEP_S)
            else:
                resp = await asyncio.to_thread(
                    supabase.table(table)
                    .select("job_posting_id", count="exact", head=True)
                    .in_("job_posting_id", chunk)
                    .execute
                )
            done += resp.count or 0
            if done and done % 5000 < EMB_CHUNK * 2:
                logger.info("  %s progress: %d", table, done)
        counts[key] = done
        logger.info(
            "  %s %s: %d", table, "deleted" if execute else "would delete", done
        )
    return counts


async def _verify(supabase: Client, *, cutoff: str) -> None:
    """Post-run invariants, printed as PASS/FAIL."""

    def _count(build: Any) -> int:
        resp = build.execute()
        return cast(int, resp.count or 0)

    live_old = _count(
        supabase.table("jobs")
        .select("id", count="exact", head=True)
        .is_("archived_at", "null")
        .is_("purged_at", "null")
        .lt("created_at", cutoff)
    )
    logger.info(
        "  %s live rows older than cutoff remain: %d",
        "PASS" if live_old == 0 else "CHECK",
        live_old,
    )
    emb = _count(
        supabase.table("job_embeddings").select("id", count="exact", head=True)
    )
    logger.info("  job_embeddings rows remaining: %d", emb)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30, help="Listing-age cutoff (default 30).")
    parser.add_argument("--execute", action="store_true", help="Write. Default is dry-run.")
    parser.add_argument(
        "--keep-engaged",
        action="store_true",
        help="Phase A: leave engaged jobs live past the cutoff (scheduled-sweep behavior).",
    )
    parser.add_argument(
        "--include-shadow",
        action="store_true",
        help="Phase C: also delete prescan_shadow rows of tombstoned jobs (#90 dataset!).",
    )
    args = parser.parse_args()

    init_supabase()
    supabase = get_supabase_pool()
    if supabase is None:
        raise SystemExit(
            "ERROR: Supabase not configured — check SUPABASE_URL + "
            "SUPABASE_SERVICE_ROLE_KEY in apps/wyrdfold-api/.env"
        )

    cutoff = _cutoff_iso(args.days)
    logger.info(
        "retention sweep: days=%d cutoff=%s execute=%s keep_engaged=%s include_shadow=%s",
        args.days,
        cutoff[:19],
        args.execute,
        args.keep_engaged,
        args.include_shadow,
    )
    logger.info("---")
    archived = await _phase_a_archive(
        supabase, cutoff=cutoff, keep_engaged=args.keep_engaged, execute=args.execute
    )
    logger.info("---")
    tombstoned = await _phase_b_tombstone(
        supabase,
        cutoff=cutoff,
        execute=args.execute,
        pending_archived=[] if args.execute else archived,
    )
    logger.info("---")
    cascade = await _phase_c_cascade(
        supabase,
        pending_tombstone=[] if args.execute else tombstoned,
        include_shadow=args.include_shadow,
        execute=args.execute,
    )
    logger.info("---")
    if args.execute:
        await _verify(supabase, cutoff=cutoff)
    logger.info(
        "TOTALS: archived=%d tombstoned=%d %s",
        len(archived),
        len(tombstoned),
        cascade,
    )
    if not args.execute:
        logger.info("dry-run — nothing written (pass --execute to apply)")


if __name__ == "__main__":
    asyncio.run(main())
