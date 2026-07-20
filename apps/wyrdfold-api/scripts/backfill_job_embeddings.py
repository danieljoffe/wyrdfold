"""One-off backfill (#60, Phase 1): embed every live job into ``job_embeddings``.

Idempotent + hash-guarded — re-running re-embeds nothing that hasn't changed
(``upsert_job_embedding`` skips a job whose stored content_hash still matches),
so it's safe to resume after an interruption. Cost-logged per job (purpose
``prescan.job_embed``). Full corpus ≈ 16M tokens @ voyage-3 ≈ ~$1.

This does NOT depend on the PRESCAN_EMBED_ENABLED flag (it calls
``upsert_job_embedding`` directly, like the poller) — the flag only gates the
on-ingest hook. It DOES depend on the embeddings provider: set
``EMBEDDINGS_PROVIDER=voyage`` + ``VOYAGE_API_KEY`` to embed for real; with the
default mock provider it writes deterministic fake vectors (use ``--limit`` for
a structural smoke run that touches no real API).

Run with the prod env so it uses the real Voyage key + prod Supabase. Must run
from a checkout that HAS the pre-scan code (develop or main):

    git checkout develop && git pull
    # smoke (mock provider, 20 jobs — touches no real API):
    cd apps/wyrdfold-api && uv run python scripts/backfill_job_embeddings.py --limit 20
    # real backfill (Voyage):
    cd apps/wyrdfold-api && railway run uv run python scripts/backfill_job_embeddings.py

(`railway run` injects the API service's env: EMBEDDINGS_PROVIDER + VOYAGE_API_KEY,
SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY. Or export those yourself and drop the
`railway run`.)

Tunables (env or flags):
    --limit N             cap total jobs (also reads BACKFILL_LIMIT). 0 = all.
    --concurrency N       parallel embeds (default 8, also reads BACKFILL_CONCURRENCY).
    --page-size N         DB fetch page size (default 1000).
    --include-archived    also cover archived jobs. Archived jobs stay Phase-2
                          gradeable (click-through re-grades ignore
                          ``archived_at``) and calibration reads their vectors,
                          so a vector-less archived candidate makes the cosine
                          gate fail OPEN — the original live-only sweep left
                          ~5.4k of them stranded (#21).
    --all-jobs            page every (selected) job and rely on the per-job
                          content-hash check, instead of the default
                          missing-only anti-join (``job_embeddings=is.null``).
                          Use to refresh vectors for CHANGED content; the
                          default only fills jobs with no vector at all.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time
from typing import Any

from app.services.embeddings import get_default_client
from app.services.embeddings.job_embeddings import (
    DEFAULT_MODEL,
    embed_jobs_batch,
    upsert_job_embedding,
)
from app.supabase_pool import get_supabase_pool, init_supabase

# Progress-reporting granularity for the batched path (embed_jobs_batch does
# its own Voyage batching + chunked writes internally).
_EMBED_BATCH_SIZE = 96

# Only the fields the embed text needs (id + title + description). Keep the
# select narrow so a large corpus fetch stays cheap.
_COLS = "id,title,description_html"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backfill job_embeddings (#60).")
    p.add_argument(
        "--limit",
        type=int,
        default=int(os.environ.get("BACKFILL_LIMIT", "0")),
        help="Max jobs to embed (0 = all live jobs).",
    )
    p.add_argument(
        "--concurrency",
        type=int,
        default=int(os.environ.get("BACKFILL_CONCURRENCY", "8")),
        help="Parallel embed calls.",
    )
    p.add_argument(
        "--page-size",
        type=int,
        default=1000,
        help="DB fetch page size.",
    )
    p.add_argument(
        "--include-archived",
        action="store_true",
        help="Also cover archived jobs (still Phase-2 gradeable; see docstring).",
    )
    p.add_argument(
        "--all-jobs",
        action="store_true",
        help="Page every job (hash-guarded re-check) instead of missing-only.",
    )
    return p.parse_args()


def _page_ids(
    sb: Any, *, table: str, cols: str, order_col: str, page_size: int, **filters: Any
) -> list[dict[str, Any]]:
    """Range-page a cheap id-level select (PostgREST caps un-paged responses
    at ~1000 rows, which would silently truncate the diff sets). ``filters``
    map onto ``eq``/``is_`` (value ``None`` → ``is.null``)."""
    out: list[dict[str, Any]] = []
    start = 0
    while True:
        q = sb.table(table).select(cols)
        for col, val in filters.items():
            q = q.is_(col, "null") if val is None else q.eq(col, val)
        resp = q.order(order_col, desc=False).range(start, start + page_size - 1).execute()
        rows = resp.data or []
        out.extend(rows)
        if len(rows) < page_size:
            return out
        start += page_size


def _iter_jobs(
    sb: Any,
    *,
    page_size: int,
    limit: int,
    include_archived: bool,
    missing_only: bool,
) -> list[dict[str, Any]]:
    """Collect the jobs to consider, oldest first.

    ``missing_only`` (the default) computes the missing set CLIENT-side: page
    all job ids, page all ``job_embeddings.job_posting_id`` for the CURRENT
    model (model-aware — a stale voyage-3 row must not mask the missing 3.5
    vector), set-difference in Python, then hydrate title/description for just
    the missing ids in keyed chunks. The obvious server-side anti-join
    (``job_embeddings=is.null``) is what the poller's LIMIT-bounded sweep
    uses, but UNBOUNDED over a 31k-row corpus it exceeds the Postgres
    statement timeout (observed on prod, error 57014) — id paging + client
    diff is three cheap scans instead. Stops once ``limit`` rows are
    collected (0 = no cap).
    """
    if not missing_only:
        out: list[dict[str, Any]] = []
        start = 0
        while True:
            end = start + page_size - 1
            q = sb.table("jobs").select(_COLS)
            if not include_archived:
                q = q.is_("archived_at", "null")
            resp = q.order("created_at", desc=False).range(start, end).execute()
            rows = resp.data or []
            if not rows:
                break
            out.extend(rows)
            if limit and len(out) >= limit:
                return out[:limit]
            if len(rows) < page_size:
                break
            start += page_size
        return out

    # Tombstoned rows (archival Stage 2) have their payload stripped —
    # nothing meaningful to embed, so always exclude them.
    job_filters: dict[str, Any] = {"purged_at": None}
    if not include_archived:
        job_filters["archived_at"] = None
    job_ids = _page_ids(
        sb,
        table="jobs",
        cols="id",
        order_col="created_at",
        page_size=page_size,
        **job_filters,
    )
    embedded_rows = _page_ids(
        sb,
        table="job_embeddings",
        cols="job_posting_id",
        order_col="job_posting_id",  # no created_at on this table
        page_size=page_size,
        model=DEFAULT_MODEL,
    )
    embedded = {r["job_posting_id"] for r in embedded_rows}
    missing = [r["id"] for r in job_ids if r["id"] not in embedded]
    if limit:
        missing = missing[:limit]

    out2: list[dict[str, Any]] = []
    for i in range(0, len(missing), 200):
        chunk = missing[i : i + 200]
        resp = sb.table("jobs").select(_COLS).in_("id", chunk).execute()
        out2.extend(resp.data or [])
    return out2


async def main() -> None:
    args = _parse_args()

    init_supabase()
    sb = get_supabase_pool()
    if sb is None:
        raise SystemExit("Supabase not configured (SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY)")

    from app.config import settings

    client = get_default_client()
    print(
        f"Backfilling job_embeddings (model={DEFAULT_MODEL}, "
        f"provider={settings.embeddings_provider}, concurrency={args.concurrency}"
        f"{f', limit={args.limit}' if args.limit else ''})..."
    )

    jobs = _iter_jobs(
        sb,
        page_size=args.page_size,
        limit=args.limit,
        include_archived=args.include_archived,
        missing_only=not args.all_jobs,
    )
    total = len(jobs)
    scope = "jobs" if args.include_archived else "live jobs"
    mode = "hash-guarded re-check" if args.all_jobs else "missing vectors only"
    print(f"Found {total} {scope} to consider ({mode}).\n")
    if total == 0:
        return

    counts: dict[str, int] = {}
    done = 0
    started = time.perf_counter()

    if not args.all_jobs:
        # Missing-only mode: every selected job provably has no current-model
        # row (server-side anti-join), so reuse the sweep's batched path —
        # one Voyage call per 96 texts, 8-row write chunks, and the write
        # circuit breaker (2 consecutive failed chunks abort the run rather
        # than re-buying embeds against a throttled disk; re-run to resume,
        # the anti-join makes it idempotent). ~100x fewer API round-trips
        # than the per-job path.
        for i in range(0, total, _EMBED_BATCH_SIZE):
            batch = jobs[i : i + _EMBED_BATCH_SIZE]
            batch_counts = await embed_jobs_batch(sb, client, batch)
            for key, val in batch_counts.items():
                counts[key] = counts.get(key, 0) + val
            done += len(batch)
            elapsed = time.perf_counter() - started
            rate = done / elapsed if elapsed else 0.0
            print(f"  {done}/{total} ({rate:.1f}/s) ...")
            if batch_counts.get("aborted"):
                print(
                    "  write circuit breaker tripped — stopping (DB is "
                    "IO-throttled; re-run later to resume where this left off)"
                )
                break
    else:
        sem = asyncio.Semaphore(args.concurrency)

        async def _one(row: dict[str, Any]) -> str:
            nonlocal done
            async with sem:
                status = await upsert_job_embedding(
                    sb,
                    client,
                    job_id=row["id"],
                    title=row.get("title"),
                    description_html=row.get("description_html"),
                )
            done += 1
            if done % 100 == 0 or done == total:
                elapsed = time.perf_counter() - started
                rate = done / elapsed if elapsed else 0.0
                print(f"  {done}/{total} ({rate:.1f}/s) ...")
            return status

        results = await asyncio.gather(*(_one(r) for r in jobs))
        for status in results:
            counts[status] = counts.get(status, 0) + 1

    elapsed = time.perf_counter() - started
    print(f"\nDone in {elapsed:.1f}s. " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if counts.get("error"):
        # Non-zero exit so a CI/cron wrapper notices partial failure.
        raise SystemExit(f"{counts['error']} job(s) failed to embed (see logs).")


if __name__ == "__main__":
    asyncio.run(main())
