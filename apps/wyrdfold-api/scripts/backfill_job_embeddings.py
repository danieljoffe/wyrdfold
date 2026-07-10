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
    JOB_EMBED_PURPOSE,
    content_hash,
    embed_text_for_job,
    upsert_job_embedding,
)
from app.services.llm import cost_log
from app.supabase_pool import get_supabase_pool, init_supabase

# Batched-mode chunk size: the Voyage client sub-batches at 128 internally;
# 96 keeps one script batch inside a single API call with headroom.
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


def _iter_jobs(
    sb: Any,
    *,
    page_size: int,
    limit: int,
    include_archived: bool,
    missing_only: bool,
) -> list[dict[str, Any]]:
    """Page through jobs to consider, oldest first.

    ``missing_only`` (the default) anti-joins ``job_embeddings`` server-side
    (``job_embeddings=is.null`` on the embedded resource — the same selection
    the poller's per-cycle sweep uses), so an already-populated corpus pages
    almost nothing. Without it, every selected job is fetched and the per-job
    content-hash check decides. Keyset would scale better, but a backfill over
    a beta-scale corpus is fine with range pagination + a stable ``created_at``
    order. Stops early once ``limit`` rows are collected (0 = no cap).

    NB with ``missing_only`` the predicate would shrink under range pagination
    if vectors were written between page fetches — safe here because ALL pages
    are collected up front and embedding only starts afterwards.
    """
    cols = _COLS + (",job_embeddings(job_posting_id)" if missing_only else "")
    out: list[dict[str, Any]] = []
    start = 0
    while True:
        end = start + page_size - 1
        q = sb.table("jobs").select(cols)
        if not include_archived:
            q = q.is_("archived_at", "null")
        if missing_only:
            # Model-aware: a stale row from a retired model (voyage-3) must
            # not mask the missing current-space vector — same predicate as
            # the poller's per-cycle sweep.
            q = q.eq("job_embeddings.model", DEFAULT_MODEL).is_(
                "job_embeddings", "null"
            )
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
        # row (server-side anti-join), so skip the per-job hash probe and
        # embed in BATCHES — one Voyage call + one bulk upsert per chunk.
        # ~100x fewer API round-trips than the per-job path; a 30k-job
        # re-embed (model migration) finishes in minutes instead of hours.
        for i in range(0, total, _EMBED_BATCH_SIZE):
            batch = jobs[i : i + _EMBED_BATCH_SIZE]
            texts: list[tuple[dict[str, Any], str]] = []
            for row in batch:
                text = embed_text_for_job(
                    row.get("title"), row.get("description_html")
                )
                if text.strip():
                    texts.append((row, text))
                else:
                    counts["skipped_empty"] = counts.get("skipped_empty", 0) + 1
            if texts:
                try:
                    result = await client.embed(
                        model=DEFAULT_MODEL,
                        inputs=[t for _, t in texts],
                        purpose=JOB_EMBED_PURPOSE,
                        input_type="document",
                    )
                    cost_log.record_embedding(
                        sb,
                        user_id=None,
                        purpose=JOB_EMBED_PURPOSE,
                        result=result,
                        metadata={"batched": len(texts), "model": DEFAULT_MODEL},
                    )
                    rows_to_write = [
                        {
                            "job_posting_id": row["id"],
                            "model": DEFAULT_MODEL,
                            "content_hash": content_hash(text),
                            "embedding": vec,
                        }
                        for (row, text), vec in zip(
                            texts, result.embeddings, strict=True
                        )
                    ]
                    sb.table("job_embeddings").upsert(
                        rows_to_write, on_conflict="job_posting_id,model"
                    ).execute()
                    counts["embedded"] = counts.get("embedded", 0) + len(rows_to_write)
                except Exception as exc:  # keep going — later batches may succeed
                    print(f"  batch failed ({len(texts)} jobs): {exc}")
                    counts["error"] = counts.get("error", 0) + len(texts)
            done += len(batch)
            elapsed = time.perf_counter() - started
            rate = done / elapsed if elapsed else 0.0
            print(f"  {done}/{total} ({rate:.1f}/s) ...")
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
