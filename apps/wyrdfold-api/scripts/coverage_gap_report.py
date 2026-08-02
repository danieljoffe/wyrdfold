"""Coverage-gap report: turn search demand into catalog seed priorities (#467).

The supply-side of the search flywheel. The public `/search` surface is live
and un-personalized; every search is logged to `search_events` (query +
result_count, no PII). This report mines that demand signal and cross-references
it against the current ADMISSION space (pipeline-active targets — titles must
prematch one to ingest at all) to answer one question:

    Which roles are people searching for that our catalog does not cover?

It is decision support, not an auto-seeder (the epic's guardrails keep seeding
human-gated). The owner reads the ranked gaps and decides which catalog targets
/ keywords to add via `seed_catalog_targets.py`. Adding a catalog target widens
admission → the poller ingests those roles → the shared corpus deepens → search
improves for everyone, logged-in or not. No public signup required.

Two gap classes, because the fix differs:

  * NO ADMISSION PATH — a ZERO-RESULT query whose tokens match no active
    target's label/keywords, so nothing admits the role. Fix: add a catalog
    target.
  * THIN CORPUS — a target admits the role (the corpus already returned some
    results, or the query's tokens match a target) but results are still few.
    Fix: nothing to add; poll coverage / freshness / time will fill it (or the
    query is genuinely niche). Surfaced separately so a demonstrably-served
    role is never mistaken for a missing target.

All reads are small-column fetches aggregated in Python — safe against prod any
time. Pure read-only.

Usage::

    cd apps/wyrdfold-api
    uv run python scripts/coverage_gap_report.py                 # 30d window
    uv run python scripts/coverage_gap_report.py --days 14 --thin 3
    railway run uv run python scripts/coverage_gap_report.py     # against prod

Env required: ``SUPABASE_URL`` + ``SUPABASE_SERVICE_ROLE_KEY``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from supabase import Client

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.services.targets.crud import get_active, normalize_label
from app.supabase_pool import get_supabase_pool, init_supabase

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("coverage_gap")

PAGE_SIZE = 1000
DEFAULT_THIN = 5

# Query tokenizer for the admission-path heuristic. Lowercase alphanumerics
# minus a stoplist of NON-role words only — seniority, level markers, and
# filler. Role nouns (engineer, developer, manager, designer, …) are LEFT IN:
# they are exactly what decides admission, so stopping them made single-word
# role queries like "engineer" falsely read as "no target" (caught on real
# prod data 2026-07-31). "Senior Data Scientist" still reduces to
# {data, scientist} and matches the Data Scientist target.
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOP = {
    "the", "a", "an", "of", "and", "or", "for", "in", "to", "with", "at", "on",
    "senior", "junior", "staff", "lead", "principal", "sr", "jr",
    "i", "ii", "iii", "iv", "remote", "role", "roles", "job", "jobs",
    "position", "positions", "level", "levels",
}


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall(text.lower()) if len(t) > 1 and t not in _STOP}


@dataclass
class Gap:
    """One clustered search query and its coverage verdict."""

    query: str  # representative (most common) raw query in the cluster
    n_searches: int
    zero_searches: int  # how many of those returned 0 results
    best_results: int  # highest result_count the cluster ever returned
    has_admission_path: bool
    severity: str  # 'zero' | 'thin' | 'covered'

    @property
    def priority(self) -> tuple[int, int, int]:
        # Sort key (desc): no-path gaps first, then severity, then demand.
        sev_rank = {"zero": 2, "thin": 1, "covered": 0}[self.severity]
        return (0 if self.has_admission_path else 1, sev_rank, self.n_searches)


def _target_token_space(targets: list[Any]) -> set[str]:
    """Union of tokens across active targets' labels, keywords and families —
    the set of role tokens the pipeline can currently admit."""
    space: set[str] = set()
    for t in targets:
        space |= _tokens(t.label or "")
        for kw in getattr(t, "search_keywords", None) or []:
            space |= _tokens(str(kw))
        fam = getattr(t, "role_family", None)
        if fam:
            space |= _tokens(str(fam))
    return space


def analyze_coverage_gaps(
    searches: list[dict[str, Any]],
    targets: list[Any],
    *,
    thin_threshold: int = DEFAULT_THIN,
) -> list[Gap]:
    """Cluster searches by normalized query and classify each cluster.

    Pure: no DB, no clock. ``searches`` are ``{query, result_count}`` rows;
    ``targets`` expose ``.label`` / ``.search_keywords`` / ``.role_family``.
    Returns gaps sorted most-actionable first.
    """
    token_space = _target_token_space(targets)

    # Cluster by normalized label so "front-end" / "frontend" collapse.
    clusters: dict[str, list[dict[str, Any]]] = defaultdict(list)
    raw_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in searches:
        q = (row.get("query") or "").strip()
        if not q:
            continue  # browse / filter-only event, no query text
        key = normalize_label(q) or q.lower()
        clusters[key].append(row)
        raw_counts[key][q] += 1

    gaps: list[Gap] = []
    for key, rows in clusters.items():
        results = [int(r.get("result_count") or 0) for r in rows]
        zero = sum(1 for c in results if c == 0)
        best = max(results) if results else 0
        # Representative raw spelling = the most-searched variant.
        rep = max(raw_counts[key].items(), key=lambda kv: kv[1])[0]
        # A cluster that ever returned results DEMONSTRABLY has an admission
        # path — those jobs were ingested and are searchable. The token
        # heuristic only has to adjudicate the zero-result clusters: no token
        # overlap → likely no target admits the role (add one); overlap →
        # a target exists but the corpus is empty/stale (poll/freshness, not a
        # missing target). This ordering matters — it keeps demonstrably-served
        # roles out of the "add a target" bucket (caught on prod data).
        has_path = best > 0 or bool(_tokens(rep) & token_space)

        if best == 0:
            severity = "zero"
        elif best < thin_threshold:
            severity = "thin"
        else:
            severity = "covered"

        gaps.append(
            Gap(
                query=rep,
                n_searches=len(rows),
                zero_searches=zero,
                best_results=best,
                has_admission_path=has_path,
                severity=severity,
            )
        )

    gaps.sort(key=lambda g: g.priority, reverse=True)
    return gaps


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


def _print(gaps: list[Gap], *, thin: int) -> None:
    no_path = [g for g in gaps if not g.has_admission_path and g.severity != "covered"]
    thin_covered = [g for g in gaps if g.has_admission_path and g.severity != "covered"]

    logger.info("== COVERAGE GAPS — NO ADMISSION PATH (add a catalog target) ==")
    if not no_path:
        logger.info("  none — every demanded role maps to an active target")
    for g in no_path[:25]:
        logger.info(
            "  %-40s  searches=%-4d zero=%-4d best=%-4d [%s]",
            g.query[:40],
            g.n_searches,
            g.zero_searches,
            g.best_results,
            g.severity,
        )

    logger.info("")
    logger.info("== THIN CORPUS — has a target, few results (poll/freshness, not a new target) ==")
    if not thin_covered:
        logger.info("  none")
    for g in thin_covered[:25]:
        logger.info(
            "  %-40s  searches=%-4d best=%-4d",
            g.query[:40],
            g.n_searches,
            g.best_results,
        )

    covered = sum(1 for g in gaps if g.severity == "covered")
    logger.info("")
    logger.info(
        "== SUMMARY == clusters=%d  no-path=%d  thin=%d  covered(>=%d)=%d",
        len(gaps),
        len(no_path),
        len(thin_covered),
        thin,
        covered,
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30, help="Demand window (default 30).")
    parser.add_argument(
        "--thin",
        type=int,
        default=DEFAULT_THIN,
        help=f"Result count below which a cluster is 'thin' (default {DEFAULT_THIN}).",
    )
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

    searches = _page(
        supabase,
        build=lambda off: (
            supabase.table("search_events")
            .select("query, result_count")
            .eq("event_type", "search")
            .gte("occurred_at", since)
            .range(off, off + PAGE_SIZE - 1)
        ),
    )
    targets = get_active(supabase)

    logger.info("Demand window: %dd   searches: %d   active targets: %d", args.days, len(searches), len(targets))
    logger.info("")
    gaps = analyze_coverage_gaps(searches, targets, thin_threshold=args.thin)
    _print(gaps, thin=args.thin)


if __name__ == "__main__":
    asyncio.run(main())
