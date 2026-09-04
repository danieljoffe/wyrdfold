"""#470: enrich ``sources.domain`` for every row that lacks one.

Walks ``sources WHERE domain IS NULL`` in batches, guesses candidate
domains from company_name/board_token, probes each over the SSRF-safe
transport, and stores the first that answers HTTP (see
``app/services/company_domain.py`` for the semantics — links only, parked
domains degrade to the initials monogram client-side).

Idempotent and resumable: enriched rows leave the NULL set, so re-running
continues where the last run stopped — and picks up sources created since,
which makes this double as the refresh command until (unless) a scheduled
tick is added. Rows whose candidates all fail stay NULL and are cheaply
re-examined on the next run.

Run with the prod env (the probes need outbound network; the writes need
the service role):

    # smoke: what WOULD be probed, no network, no writes
    cd apps/wyrdfold-api && uv run python scripts/backfill_source_domains.py --dry-run
    # real run:
    cd apps/wyrdfold-api && railway run uv run python scripts/backfill_source_domains.py

Tunables:
    --dry-run        list NULL-domain sources + their candidate guesses; no probes, no writes.
    --batch N        rows fetched+probed per loop iteration (default 50).
    --max-rows N     stop after examining N rows (default: run to exhaustion).
    --concurrency N  parallel probes within a batch (default 4 — be a polite client).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from supabase import AsyncClientOptions, acreate_client

from app.config import settings
from app.services.company_domain import (
    candidate_domains,
    enrich_missing_source_domains,
)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backfill sources.domain (#470).")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--batch", type=int, default=50)
    p.add_argument("--max-rows", type=int, default=0, help="0 = exhaust the NULL set")
    p.add_argument("--concurrency", type=int, default=4)
    return p.parse_args()


async def main() -> None:
    args = _parse_args()
    if not (settings.supabase_url and settings.supabase_service_role_key):
        raise SystemExit("Supabase not configured (SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY)")

    sb = await acreate_client(
        settings.supabase_url,
        settings.supabase_service_role_key,
        AsyncClientOptions(),
    )

    if args.dry_run:
        resp = await (
            sb.table("sources")
            .select("id, company_name, board_token")
            .is_("domain", "null")
            .order("id")
            .limit(args.batch)
            .execute()
        )
        rows = resp.data or []
        print(f"[dry-run] first {len(rows)} NULL-domain source(s) and their candidates:")
        for r in rows:
            cands = candidate_domains(
                str(r.get("company_name") or ""), str(r.get("board_token") or "")
            )
            print(f"  {r['id']}  {r.get('company_name')!r:40} -> {cands or '(no candidates)'}")
        return

    examined_total = enriched_total = 0
    cursor: str | None = None
    while True:
        examined, enriched, cursor = await enrich_missing_source_domains(
            sb, limit=args.batch, concurrency=args.concurrency, after_id=cursor
        )
        examined_total += examined
        enriched_total += enriched
        print(
            f"batch: examined={examined} enriched={enriched} (totals {examined_total}/{enriched_total})"
        )
        if examined == 0 or cursor is None:
            break
        if args.max_rows and examined_total >= args.max_rows:
            print("reached --max-rows")
            break

    print(f"done: examined={examined_total} enriched={enriched_total}")


if __name__ == "__main__":
    asyncio.run(main())
