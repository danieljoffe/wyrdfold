"""One-shot backfill of jobs.(salary_min, salary_max, salary_currency,
salary_period) from the stored ``salary_text``.

Ingestion writes the structured parts alongside the text going forward
(``salary_columns`` at every write site); this script structures the rows
ingested before that. Pure regex over the already-extracted string — no LLM,
no board fetches — validated against every distinct salary_text prod has
stored (5,834 formats).

Idempotent and resumable: only rows with ``salary_text`` and ALL FOUR parts
null are touched; an id-cursor guarantees forward progress over rows whose
parse yields nothing (they are counted and left null — display-only). Run
against prod via ``railway run``:

    cd apps/wyrdfold-api
    railway run uv run python scripts/backfill_salary_parts.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from supabase import create_client

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.services.extract import parse_salary_text

BATCH = 200
PAUSE_SECONDS = 0.5


def main() -> None:
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    supabase = create_client(url, key)

    structured = 0
    unparsed = 0
    cursor = "00000000-0000-0000-0000-000000000000"
    while True:
        try:
            resp = (
                supabase.table("jobs")
                .select("id, salary_text")
                .not_.is_("salary_text", "null")
                .is_("salary_min", "null")
                .is_("salary_max", "null")
                .is_("salary_currency", "null")
                .is_("salary_period", "null")
                .gt("id", cursor)
                .order("id")
                .limit(BATCH)
                .execute()
            )
            rows = resp.data or []
            if not rows:
                break

            for row in rows:
                cursor = row["id"]
                parts = parse_salary_text(row.get("salary_text"))
                if parts.is_empty:
                    unparsed += 1
                    continue
                supabase.table("jobs").update(
                    {
                        "salary_min": parts.min,
                        "salary_max": parts.max,
                        "salary_currency": parts.currency,
                        "salary_period": parts.period,
                    }
                ).eq("id", row["id"]).execute()
                structured += 1
        except Exception as exc:
            # Supabase edge caps one HTTP/2 connection at ~20k streams; the
            # loop is idempotent and cursor-resumable, so reconnect and go on.
            print(f"reconnecting after {type(exc).__name__}: {exc}", flush=True)
            supabase = create_client(url, key)
            time.sleep(2)
            continue

        print(f"structured {structured} rows ({unparsed} unparsed)…", flush=True)
        time.sleep(PAUSE_SECONDS)

    print(f"done — {structured} structured, {unparsed} left display-only")


if __name__ == "__main__":
    main()
