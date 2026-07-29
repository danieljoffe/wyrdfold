"""One-shot heal of escaped-HTML job rows + salary re-extraction (#500/#503).

Greenhouse's Job Board API delivered ``content`` HTML-escaped; ingestion has
unescaped at the source since #502 shipped, but rows stored before that fix
hold ``&lt;div ...`` verbatim. The assumption that the poller heals them
per cycle turned out to be FALSE for most of the corpus (verified in prod,
2026-07-29): the conflict-update only refreshes a row when its source is
polled AND its title prematches TODAY'S active-target keywords AND it passes
the US gate AND the board still lists it. Rows outside the current target
set — most of a corpus accumulated under since-deactivated targets — never
converge, and they are publicly servable via /search (escaped tag soup in
the listing-detail body, ``salary_text`` forever null because the escaped
markup defeats the structural pay-range parse).

For each live row whose ``description_html`` starts with an escaped tag:
unescape the document (bounded, tag-shape-verified — see
``unescape_html_doc``) and re-run ``extract_salary_from_html`` on the healed
markup, which unlocks the structural Greenhouse ``pay-range`` parse that the
escaped form defeats. ``salary_text`` is only ever filled where it is null —
never clobbered. Rows the healer rejects are left untouched and reported.

Idempotent and resumable: healed rows stop matching the ``&lt;%`` predicate;
an id-cursor guarantees forward progress within a run even over rejected
rows. Archived rows are skipped — they are delisted (404 on /search) and
purged at 60d. Run against prod via ``railway run`` (env supplies
SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY):

    cd apps/wyrdfold-api
    railway run uv run python scripts/backfill_escaped_html.py

Writes are batched with a pause between batches — small-instance IO
discipline.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from supabase import create_client

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.services.extract import extract_salary_from_html, unescape_html_doc

BATCH = 100
PAUSE_SECONDS = 0.5


def main() -> None:
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    supabase = create_client(url, key)

    healed = 0
    salaries = 0
    rejected = 0
    # id-cursor pagination (uuid column — start below every real id).
    cursor = "00000000-0000-0000-0000-000000000000"
    while True:
        try:
            resp = (
                supabase.table("jobs")
                .select("id, description_html, salary_text")
                .like("description_html", "&lt;%")
                .is_("archived_at", "null")
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
                doc = unescape_html_doc(row.get("description_html"))
                if doc is None:
                    # Predicate matched but the healer refused (no real tag
                    # after unescaping) — leave the row alone, keep moving.
                    rejected += 1
                    continue
                update: dict[str, str] = {"description_html": doc}
                if not row.get("salary_text"):
                    salary = extract_salary_from_html(doc)
                    if salary:
                        update["salary_text"] = salary
                        salaries += 1
                supabase.table("jobs").update(update).eq("id", row["id"]).execute()
                healed += 1
        except Exception as exc:
            # The Supabase edge terminates a single HTTP/2 connection after
            # ~20k streams and the sync client holds ONE connection for its
            # lifetime. The loop is idempotent and cursor-resumable, so a
            # fresh client picks up exactly where the dead one stopped.
            print(f"reconnecting after {type(exc).__name__}: {exc}", flush=True)
            supabase = create_client(url, key)
            time.sleep(2)
            continue

        print(
            f"healed {healed} rows ({salaries} salaries filled, {rejected} rejected)…",
            flush=True,
        )
        time.sleep(PAUSE_SECONDS)

    print(f"done — {healed} healed, {salaries} salaries filled, {rejected} rejected")


if __name__ == "__main__":
    main()
