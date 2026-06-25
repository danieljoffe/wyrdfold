"""One-off backfill (#60, Phase 2): embed every active target into ``targets``.

The query side of the pre-scan. For each active target it embeds
``label + search_keywords`` as a Voyage *query* and writes the vector onto the
``targets`` row (``embedding`` + ``embedding_text_hash``) via
``upsert_target_embedding``. Idempotent + hash-guarded — a target whose text is
unchanged is skipped (``cache_hit``), so it's safe to resume after an
interruption. Cost-logged per target (purpose ``prescan.target_embed``); spend
is trivial (a few-token query per target, a handful of targets).

This does NOT depend on any feature flag (it calls the service directly). It
DOES depend on the embeddings provider: set ``EMBEDDINGS_PROVIDER=voyage`` +
``VOYAGE_API_KEY`` to embed for real; with the default mock provider it writes
deterministic fake vectors (use ``--dry-run`` for a structural smoke that
touches no real API and writes nothing).

Run with the prod env so it uses the real Voyage key + prod Supabase. Must run
from a checkout that HAS the pre-scan code (develop or main):

    git checkout develop && git pull
    # smoke (no writes, no API): list what WOULD be embedded
    cd apps/wyrdfold-api && uv run python scripts/backfill_target_embeddings.py --dry-run
    # real backfill (Voyage):
    cd apps/wyrdfold-api && railway run uv run python scripts/backfill_target_embeddings.py

(`railway run` injects the API service's env: EMBEDDINGS_PROVIDER + VOYAGE_API_KEY,
SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY. Or export those yourself and drop the
`railway run`.)

Tunables (flags):
    --dry-run        list targets + their embed text, write nothing, call no API.
    --concurrency N  parallel embeds (default 4 — there are few targets).
    --all            embed EVERY target, not just active ones.
"""

from __future__ import annotations

import argparse
import asyncio
import time

from app.services.embeddings import get_default_client
from app.services.embeddings.target_embeddings import (
    DEFAULT_MODEL,
    embed_text_for_target,
    upsert_target_embedding,
)
from app.services.targets.crud import get_active as get_active_targets
from app.services.targets.crud import get_all as get_all_targets
from app.supabase_pool import get_supabase_pool, init_supabase


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backfill target embeddings (#60, Phase 2).")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="List targets + embed text without calling the API or writing.",
    )
    p.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help="Parallel embed calls (default 4 — there are few targets).",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="Embed every target, not just active ones.",
    )
    return p.parse_args()


async def main() -> None:
    args = _parse_args()

    init_supabase()
    sb = get_supabase_pool()
    if sb is None:
        raise SystemExit("Supabase not configured (SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY)")

    from app.config import settings

    targets = get_all_targets(sb) if args.all else get_active_targets(sb)
    total = len(targets)
    scope = "all" if args.all else "active"
    print(
        f"Target embeddings (#60): {total} {scope} target(s), model={DEFAULT_MODEL}, "
        f"provider={settings.embeddings_provider}"
        f"{' [DRY RUN]' if args.dry_run else f', concurrency={args.concurrency}'}."
    )
    if total == 0:
        return

    if args.dry_run:
        # Show exactly what each target WOULD embed — the validated label+keywords
        # text — so a human can eyeball it before spending. No API, no write.
        for t in targets:
            text = embed_text_for_target(t)
            preview = text if len(text) <= 200 else text[:199] + "…"
            print(f"  {t.id}  {t.label!r}\n    embed_text: {preview!r}")
        print("\nDry run — nothing embedded, nothing written.")
        return

    client = get_default_client()
    sem = asyncio.Semaphore(args.concurrency)
    counts: dict[str, int] = {}
    started = time.perf_counter()

    async def _one(target: object) -> str:
        async with sem:
            return await upsert_target_embedding(sb, client, target)  # type: ignore[arg-type]

    results = await asyncio.gather(*(_one(t) for t in targets))
    for status in results:
        counts[status] = counts.get(status, 0) + 1

    elapsed = time.perf_counter() - started
    print(f"\nDone in {elapsed:.1f}s. " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if counts.get("error"):
        # Non-zero exit so a CI/cron wrapper notices partial failure.
        raise SystemExit(f"{counts['error']} target(s) failed to embed (see logs).")


if __name__ == "__main__":
    asyncio.run(main())
