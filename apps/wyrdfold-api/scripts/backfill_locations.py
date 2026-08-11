"""One-shot backfill of jobs.(city, state, country, location_remote) (#518).

Live board-polled rows converge organically — the poller re-parses
``location`` from the fresh payload every cycle (#514). This script exists
for the rows no cycle ever touches again: manual / from-url ingests and
archived rows, plus a fast first pass over the whole corpus so the UI
doesn't wait poll-cadence time for the initial convergence.

Idempotent and resumable: only rows where ``city IS NULL AND state IS NULL
AND country IS NULL AND location_remote IS NULL`` and ``location`` is
non-null are touched; parses that yield nothing write ``location_remote =
FALSE`` so the row is not re-selected forever. Run against prod via
``railway run`` (env supplies SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY), or
locally against the local stack:

    cd apps/wyrdfold-api
    SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... uv run python scripts/backfill_locations.py

Writes are small (4 scalar columns) and batched with a pause between
batches — small-instance IO discipline.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from supabase import create_client

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.services.location_parse import parse_location

BATCH = 200
PAUSE_SECONDS = 0.5


def main() -> None:
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    supabase = create_client(url, key)

    total = 0
    while True:
        try:
            resp = (
                supabase.table("jobs")
                .select("id, location")
                .not_.is_("location", "null")
                .is_("city", "null")
                .is_("state", "null")
                .is_("country", "null")
                .is_("location_remote", "null")
                # Newest first: fresh postings are the ones users see on
                # /search, so they get the canonical display soonest.
                .order("created_at", desc=True)
                .limit(BATCH)
                .execute()
            )
            rows = resp.data or []
            if not rows:
                break

            for row in rows:
                loc = parse_location(row.get("location"))
                supabase.table("jobs").update(
                    {
                        "city": loc.city,
                        "state": loc.state,
                        "country": loc.country,
                        # Always non-null after a parse attempt — this is the
                        # "already processed" marker that keeps the select loop
                        # converging even for unparseable strings.
                        "location_remote": loc.remote,
                    }
                ).eq("id", row["id"]).execute()
                total += 1
        except Exception as exc:
            # The Supabase edge terminates a single HTTP/2 connection after
            # ~20k streams (observed: RemoteProtocolError ConnectionTerminated
            # at last_stream_id 19999) and the sync client holds ONE
            # connection for its lifetime. The loop is idempotent, so a fresh
            # client resumes exactly where the dead one stopped.
            print(f"reconnecting after {type(exc).__name__}: {exc}", flush=True)
            supabase = create_client(url, key)
            time.sleep(2)
            continue

        print(f"backfilled {total} rows…", flush=True)
        time.sleep(PAUSE_SECONDS)

    print(f"done — {total} rows backfilled")


if __name__ == "__main__":
    main()
