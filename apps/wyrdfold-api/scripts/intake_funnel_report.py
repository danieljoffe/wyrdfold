"""Intake-funnel report: is the fresh-only search surface staying full?

Read-only ops report for the 30-day retention world (2026-07-30): once the
retention sweep caps listing age, the searchable corpus is purely a function
of intake — and intake is a product of source coverage x poll throughput x
ADMISSION breadth (titles must prematch an ACTIVE target to ingest at all).
This prints the whole funnel in one command:

  1. INTAKE      — jobs ingested per day (14d detail, 7d/30d rates).
  2. CORPUS      — live searchable count (the /search predicate:
                   archived_at/purged_at NULL, is_us IS NOT FALSE), age
                   histogram, and the aging-out forecast: how many rows the
                   30d window loses in the next 1/3/7/14/30 days vs the
                   intake needed to replace them.
  3. ADMISSION   — the active targets (label + family). ONE narrow target
                   here explains a starved corpus regardless of sources
                   (2026-07-30: a single customer_experience target admitted
                   ~25 jobs/day across 1,145 polled boards).
  4. SOURCES     — enabled boards, 24h poll coverage (the #526 per-cycle cap
                   shows up here), boards producing candidates in 7d.
  5. DEMAND      — searches + zero-result rate (7d) and the zero-result
                   queries themselves (coverage gaps users actually hit).

All reads are id-level or small-column fetches aggregated in Python — no
heavy SQL on the small instance. Pure read-only: safe against prod any time.

Usage::

    cd apps/wyrdfold-api
    uv run python scripts/intake_funnel_report.py            # full report
    uv run python scripts/intake_funnel_report.py --days 45  # wider intake window

Env required: ``SUPABASE_URL`` + ``SUPABASE_SERVICE_ROLE_KEY``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from supabase import Client

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.supabase_pool import get_supabase_pool, init_supabase

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("intake_funnel")

PAGE_SIZE = 1000
RETENTION_DAYS = 30


def _page(supabase: Client, *, build: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    offset = 0
    while True:
        resp = build(offset).execute()
        page = cast(list[dict[str, Any]], resp.data or [])
        out.extend(page)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return out


def _count(supabase: Client, table: str, *, build: Any = None) -> int:
    q = supabase.table(table).select("id", count="exact", head=True)
    if build is not None:
        q = build(q)
    return cast(int, q.execute().count or 0)


def _day(iso: str) -> str:
    return iso[:10]


def _age_days(iso: str, now: datetime) -> float:
    ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (now - ts).total_seconds() / 86400.0


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=35, help="Intake window (default 35).")
    args = parser.parse_args()

    init_supabase()
    supabase = get_supabase_pool()
    if supabase is None:
        raise SystemExit(
            "ERROR: Supabase not configured — check SUPABASE_URL + "
            "SUPABASE_SERVICE_ROLE_KEY in apps/wyrdfold-api/.env"
        )

    now = datetime.now(UTC)
    since = (now - timedelta(days=args.days)).isoformat()

    # ---- 1. INTAKE ----------------------------------------------------
    ingested = _page(
        supabase,
        build=lambda off: (
            supabase.table("jobs")
            .select("cataloged_at")
            .gte("cataloged_at", since)
            .order("cataloged_at", desc=False)
            .range(off, off + PAGE_SIZE - 1)
        ),
    )
    by_day = Counter(_day(cast(str, r["cataloged_at"])) for r in ingested)
    last7 = sum(n for d, n in by_day.items() if _age_days(d + "T00:00:00+00:00", now) < 7)
    last30 = sum(n for d, n in by_day.items() if _age_days(d + "T00:00:00+00:00", now) < 30)
    logger.info("== 1. INTAKE (last %dd) ==", args.days)
    for d in sorted(by_day)[-14:]:
        logger.info("  %s  %5d", d, by_day[d])
    logger.info("  rate: %.1f/day (7d)   %.1f/day (30d)", last7 / 7, last30 / 30)

    # ---- 2. CORPUS ----------------------------------------------------
    live = _page(
        supabase,
        build=lambda off: (
            supabase.table("jobs")
            .select("cataloged_at")
            .is_("archived_at", "null")
            .is_("purged_at", "null")
            .not_.is_("is_us", "false")
            .order("cataloged_at", desc=False)
            .range(off, off + PAGE_SIZE - 1)
        ),
    )
    ages = sorted(_age_days(cast(str, r["cataloged_at"]), now) for r in live)
    buckets = Counter()
    for a in ages:
        edge = next((e for e in (7, 14, 21, 30) if a <= e), 31)
        buckets[edge] += 1
    logger.info("== 2. CORPUS (live searchable) ==")
    logger.info("  total: %d", len(ages))
    labels = {7: "0-7d", 14: "8-14d", 21: "15-21d", 30: "22-30d", 31: ">30d (sweep pending)"}
    for edge in (7, 14, 21, 30, 31):
        logger.info("  %-22s %5d", labels[edge], buckets.get(edge, 0))
    logger.info("  aging-out forecast (rows the %dd window loses):", RETENTION_DAYS)
    for horizon in (1, 3, 7, 14, 30):
        leaving = sum(1 for a in ages if RETENTION_DAYS - horizon < a <= RETENTION_DAYS)
        needed = leaving / horizon
        logger.info(
            "    next %2dd: -%5d  (replacement intake needed: %.0f/day; current 7d rate %.0f/day)",
            horizon,
            leaving,
            needed,
            last7 / 7,
        )

    # ---- 3. ADMISSION -------------------------------------------------
    # Pipeline-active = app_active floor OR any active membership — the same
    # derived predicate the poller iterates (crud.get_active, P0 2026-07-31).
    from app.services.targets.crud import get_active as get_pipeline_active_targets

    targets = [
        {"label": t.label, "role_family": t.role_family}
        for t in get_pipeline_active_targets(supabase)
    ]
    logger.info("== 3. ADMISSION (pipeline-active targets — titles must prematch these to ingest) ==")
    if not targets:
        logger.info("  NONE — nothing can ingest (free gates drop everything)")
    for t in targets:
        logger.info("  %-55s %s", t.get("label"), t.get("role_family"))

    # ---- 4. SOURCES ---------------------------------------------------
    day_ago = (now - timedelta(hours=24)).isoformat()
    week_ago = (now - timedelta(days=7)).isoformat()
    enabled = _count(supabase, "sources", build=lambda q: q.eq("enabled", True))
    polled_24h = _count(
        supabase,
        "sources",
        build=lambda q: q.eq("enabled", True).gte("last_polled_at", day_ago),
    )
    producing_7d = _count(
        supabase,
        "sources",
        build=lambda q: q.eq("enabled", True).gte("last_candidate_at", week_ago),
    )
    logger.info("== 4. SOURCES ==")
    logger.info("  enabled: %d", enabled)
    logger.info(
        "  polled in 24h: %d (%.0f%% coverage — the #526 per-cycle cap governs this)",
        polled_24h,
        100.0 * polled_24h / enabled if enabled else 0.0,
    )
    logger.info("  produced a candidate in 7d: %d", producing_7d)

    # ---- 5. DEMAND ----------------------------------------------------
    searches = cast(
        list[dict[str, Any]],
        supabase.table("search_events")
        .select("query, result_count")
        .eq("event_type", "search")
        .gte("occurred_at", week_ago)
        .execute()
        .data
        or [],
    )
    zero = [s for s in searches if (s.get("result_count") or 0) == 0]
    logger.info("== 5. DEMAND (7d) ==")
    logger.info(
        "  searches: %d   zero-result: %d (%.0f%%)",
        len(searches),
        len(zero),
        100.0 * len(zero) / len(searches) if searches else 0.0,
    )
    for s in zero[:10]:
        logger.info("    zero-result query: %r", s.get("query"))


if __name__ == "__main__":
    asyncio.run(main())
