"""One-off backfill (#928): re-assert the board's own "Remote" from the stored
location string onto rows whose ``jobs.is_remote`` is NULL.

WHY, and what this does NOT recover. #928 let a heterogeneous bulk upsert blank
``is_remote`` on the postings that omitted the key. Two different populations
ended up NULL, and only one of them is recoverable for free:

1. RECOVERABLE (this script). Rows whose stored ``location`` parses as remote.
   ``board_columns`` supplies ``is_remote = True`` for exactly these, from the
   board's own words — so writing it here re-derives a board-stated fact, it
   does not infer one. Deterministic, no LLM, no board re-fetch. Most of these
   predate #846 and have been skipped as byte-identical (#642) ever since, so
   they never converge on their own.

2. NOT RECOVERABLE. Rows the board was silent about, whose ``is_remote`` came
   from the qualification tagger and was then blanked. The board never said
   anything about them — that is why the key was omitted — so the only way back
   is to re-run the tagger (LLM spend, an operator decision, not a backfill).
   They do self-heal whenever the posting's content changes and it re-tags.

Idempotent: it only ever writes ``is_remote = TRUE`` onto rows that are NULL,
so re-running converges and never overwrites an established value. It also
never writes FALSE — a location that does not say "Remote" proves nothing
(``board_columns``' deliberate asymmetry; treating silence as on-site is the
same mistake #928 was).

    # dry run against prod (READ ONLY — prints the counts, writes nothing):
    cd apps/wyrdfold-api && railway run -- uv run python \
        scripts/backfill_board_remote_from_location.py
    # apply:
    cd apps/wyrdfold-api && railway run -- uv run python \
        scripts/backfill_board_remote_from_location.py --apply

Flags:
    --apply               actually write (default is a dry run)
    --include-archived    also cover archived rows (default: live only)
    --page-size N         DB fetch page size (default 1000)
"""

from __future__ import annotations

import argparse
import os

from supabase import create_client

from app.services.location_parse import parse_location

# PostgREST 414s on an over-long ``in_`` filter; 150 ids is the bound the rest
# of the codebase uses.
_UPDATE_CHUNK = 150


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--include-archived", action="store_true")
    ap.add_argument("--page-size", type=int, default=1000)
    args = ap.parse_args()

    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])

    scanned = 0
    to_fix: list[str] = []
    start = 0
    while True:
        q = (
            sb.table("jobs")
            .select("id, location")
            .is_("is_remote", "null")
            .order("id")
            .range(start, start + args.page_size - 1)
        )
        if not args.include_archived:
            q = q.is_("archived_at", "null")
        rows = q.execute().data or []
        scanned += len(rows)
        to_fix.extend(r["id"] for r in rows if parse_location(r.get("location")).remote)
        if len(rows) < args.page_size:
            break
        start += args.page_size

    print(f"scanned {scanned} row(s) with is_remote IS NULL")
    print(f"location says remote -> would set is_remote = TRUE on {len(to_fix)}")
    if not args.apply:
        print("dry run — nothing written (pass --apply to write)")
        return

    written = 0
    for i in range(0, len(to_fix), _UPDATE_CHUNK):
        chunk = to_fix[i : i + _UPDATE_CHUNK]
        # Re-assert the NULL predicate in the WHERE clause so a concurrent poll
        # that established a value between the read and this write wins.
        sb.table("jobs").update({"is_remote": True}).in_("id", chunk).is_(
            "is_remote", "null"
        ).execute()
        written += len(chunk)
        print(f"  {written}/{len(to_fix)}")
    print(f"done — {written} row(s) submitted")


if __name__ == "__main__":
    main()
