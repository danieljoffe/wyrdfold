"""One-shot backfill of ``jobs.title_display`` (title_display design, 2026-08-14).

Run AFTER the ``20260814060000_jobs_title_display`` migration is applied:

    cd apps/wyrdfold-api
    railway run -- uv run python scripts/backfill_title_display.py [--dry-run]

Reads every jobs row's ``(id, title)`` in keyset pages, computes
``clean_title_display`` locally (deterministic, no LLM), and PATCHes only the
rows whose cleaned form differs from raw — for most of the corpus the column
stays NULL and nothing is written. Idempotent: re-running skips rows whose
stored ``title_display`` already matches. One-shot analytics discipline: pure
keyset reads, batched writes, no vector math, safe to Ctrl-C and resume.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import httpx

PAGE = 1000


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    sys.path.insert(0, str(Path(__file__).parent / ".."))
    from app.services.titles import clean_title_display

    base = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}

    scanned = repaired = 0
    last_id = ""
    async with httpx.AsyncClient(timeout=60) as client:
        while True:
            params = {
                "select": "id,title,title_display",
                "order": "id.asc",
                "limit": str(PAGE),
            }
            if last_id:
                params["id"] = f"gt.{last_id}"
            resp = await client.get(f"{base}/rest/v1/jobs", params=params, headers=headers)
            resp.raise_for_status()
            rows = resp.json()
            if not rows:
                break
            for row in rows:
                scanned += 1
                cleaned = clean_title_display(row.get("title"))
                if cleaned == row.get("title_display"):
                    continue
                repaired += 1
                if args.dry_run:
                    print(f"  would set {row['id']}: {row.get('title')!r} -> {cleaned!r}")
                    continue
                patch = await client.patch(
                    f"{base}/rest/v1/jobs",
                    params={"id": f"eq.{row['id']}"},
                    headers={**headers, "Prefer": "return=minimal"},
                    json={"title_display": cleaned},
                )
                patch.raise_for_status()
            last_id = rows[-1]["id"]
            print(f"scanned {scanned:,} (repaired {repaired:,})…", flush=True)
            if len(rows) < PAGE:
                break

    print(f"done: {scanned:,} scanned, {repaired:,} repaired{' (dry run)' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
