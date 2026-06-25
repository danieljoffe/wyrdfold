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
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time
from typing import Any

from app.services.embeddings import get_default_client
from app.services.embeddings.job_embeddings import DEFAULT_MODEL, upsert_job_embedding
from app.supabase_pool import get_supabase_pool, init_supabase

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
    return p.parse_args()


def _iter_live_jobs(sb: Any, *, page_size: int, limit: int) -> list[dict[str, Any]]:
    """Page through live (archived_at IS NULL) jobs, oldest first.

    Keyset would scale better, but a one-off backfill over a beta-scale corpus
    is fine with range pagination + a stable ``created_at`` order. Stops early
    once ``limit`` rows are collected (0 = no cap).
    """
    out: list[dict[str, Any]] = []
    start = 0
    while True:
        end = start + page_size - 1
        resp = (
            sb.table("jobs")
            .select(_COLS)
            .is_("archived_at", "null")
            .order("created_at", desc=False)
            .range(start, end)
            .execute()
        )
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

    jobs = _iter_live_jobs(sb, page_size=args.page_size, limit=args.limit)
    total = len(jobs)
    print(f"Found {total} live jobs to consider.\n")
    if total == 0:
        return

    sem = asyncio.Semaphore(args.concurrency)
    counts: dict[str, int] = {}
    done = 0
    started = time.perf_counter()

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
