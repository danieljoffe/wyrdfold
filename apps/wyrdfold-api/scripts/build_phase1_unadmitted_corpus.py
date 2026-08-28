"""Build the Phase-1 bake-off corpus from the *unadmitted* stack — live boards.

Why not the database
--------------------
The obvious corpus for "which model triages titles best" is ``jobs``. It is the
wrong one. Every row in ``jobs`` already cleared the admission gate, so the
catalog is a biased sample of *already-promising* postings: every model scores
well on it and the ranking flatters whoever is being tested. The postings Phase 1
actually has to judge — the ones it drops — were never persisted at all.

So this script rebuilds that stack the only way it exists: by re-fetching live
boards and re-running the poller's own free gates over the results.

    listed on the board
      → minus what we already hold (``jobs.external_id`` per source)
      → through ``poller._passes_free_gates`` (title prematch AND US location)
      = the free-gate survivors Phase 1 is asked about and mostly never admits

``_passes_free_gates`` / ``_title_matches_any_target`` / ``is_us_location`` are
IMPORTED from the poller, not reimplemented. A corpus built on a paraphrase of
the gate is not the decision boundary Phase 1 faces, and the paraphrase would
drift the first time the real gate changed.

The cross product is the workload
---------------------------------
``poller._poll_one_source`` builds ONE ``triage_candidates`` list per source (the
free-gate survivors) and then grades it against EVERY unblocked active target. So
the real Phase-1 unit of work is (target × survivor), not (survivor) — which is
exactly why turning Phase 1 on for the shared catalog targets converts triage
from a per-user cost into a global one. This script emits that cross product.

Each case is tagged with a ``stratum``:

- ``own_gate``   — this specific target's free gate admits this title. The hard,
  genuinely-ambiguous pairs: the title looks like the target's kind of role on
  keywords, and only the LLM separates "Staff Web Engineer" from "Staff Data
  Engineer".
- ``cross_gate`` — the title only survived because a DIFFERENT target's gate
  admitted it. Usually an easy off-family reject.

Both are real prod traffic and both are billed, so the headline number is over
all of it; the eval also reports the ``own_gate`` subset separately, because a
model can post a great overall agreement by getting only the easy half right.

Output
------
An ``eval_set.json``-shaped fixture that ``eval_phase1_triage.py --fixture``
reads directly. Targets are written with ONLY the fields the Phase-1 prompt
consumes (``label`` + the two example pools) plus ``scoring_profile`` /
``search_keywords`` so the corpus build is reproducible. ``description`` is
DROPPED on purpose: it is second-person prose that names employers (#868), and
this repo is public.

Usage::

    cd apps/wyrdfold-api
    # plan the fetch without touching a board
    railway run -- uv run python -m scripts.build_phase1_unadmitted_corpus --dry-run
    # build it
    railway run -- uv run python -m scripts.build_phase1_unadmitted_corpus \
        --output tests/fixtures/phase1_unadmitted_corpus.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, cast

from app.models.targets import JobTarget
from app.services.poller import (
    FETCHERS,
    _call_fetcher,
    _is_us_location,
    _passes_free_gates,
    _title_matches_any_target,
)
from app.services.standard_job import StandardJob
from app.services.targets.crud import get_active as get_active_targets
from app.services.workday import KnownPosting
from app.supabase_pool import create_service_client

logging.basicConfig(level=logging.INFO, format="%(message)s")
# One line per board request × 155 boards × per-posting Workday details buries the
# funnel counters that are the actual output of this script.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("build_phase1_unadmitted_corpus")

# How many sources to poll per provider. Deliberately spread rather than
# proportional to fleet share: one board's naming conventions ("Software
# Engineer II, Platform — Remote (US)") must not become the corpus. Workday is
# held lower than its fleet share because it costs a per-posting detail request
# for every admissible list entry.
_DEFAULT_SOURCES_PER_PROVIDER: dict[str, int] = {
    "greenhouse": 45,
    "ashby": 40,
    "lever": 30,
    "workday": 25,
    "smartrecruiters": 15,
}

# Distinct titles to keep. Multiplied by the active-target count to get the case
# total (6 targets × 160 = 960 pairs), which lands in the 600-1,000 band the
# bake-off is budgeted for.
_DEFAULT_TITLES = 160

# Board fetches in flight. POLL_CONCURRENCY is 6 in prod for DB-write reasons
# that don't apply here, but staying near it keeps us civil to the boards.
_DEFAULT_CONCURRENCY = 8

# PostgREST pages at 1,000 rows. Any read that can exceed that pages explicitly
# — a silent truncation here would invent "not yet ingested" postings we
# actually hold, which is the one error this corpus cannot tolerate.
_PAGE = 1000

# `.in_()` builds a request URL; past ~150-200 ids it 414s.
_IN_CHUNK = 100


def _load_sources(sb: Any, rng: random.Random, per_provider: dict[str, int]) -> list[dict[str, Any]]:
    """Sample enabled sources, stratified by provider."""
    rows: list[dict[str, Any]] = []
    start = 0
    while True:
        resp = (
            sb.table("sources")
            .select("id, provider, board_token, company_name")
            .eq("enabled", True)
            .range(start, start + _PAGE - 1)
            .execute()
        )
        page = cast(list[dict[str, Any]], resp.data or [])
        rows.extend(page)
        if len(page) < _PAGE:
            break
        start += _PAGE

    by_provider: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_provider[str(r.get("provider"))].append(r)

    picked: list[dict[str, Any]] = []
    for provider, want in per_provider.items():
        pool = [r for r in by_provider.get(provider, []) if r.get("board_token")]
        if not pool:
            logger.warning("No enabled sources for provider %r — skipping.", provider)
            continue
        rng.shuffle(pool)
        picked.extend(pool[:want])
    rng.shuffle(picked)
    logger.info(
        "Sampled %d of %d enabled sources: %s",
        len(picked),
        len(rows),
        dict(Counter(r["provider"] for r in picked)),
    )
    return picked


def _known_by_source(sb: Any, source_ids: list[str]) -> dict[str, dict[str, KnownPosting]]:
    """``{source_id: {external_id: KnownPosting}}`` for the sampled sources.

    Mirrors the poller's ``known_external_ids`` read: EVERY row for the source,
    not the live-unengaged view — an archived or engaged posting was still
    admitted on a prior cycle, so it is not part of the unadmitted stack.
    """
    out: dict[str, dict[str, KnownPosting]] = defaultdict(dict)
    for i in range(0, len(source_ids), _IN_CHUNK):
        chunk = source_ids[i : i + _IN_CHUNK]
        start = 0
        while True:
            resp = (
                sb.table("jobs")
                .select("source_id, external_id, title, source_posted_at")
                .in_("source_id", chunk)
                .range(start, start + _PAGE - 1)
                .execute()
            )
            page = cast(list[dict[str, Any]], resp.data or [])
            for r in page:
                ext = r.get("external_id")
                if ext:
                    out[str(r["source_id"])][str(ext)] = KnownPosting(
                        title=r.get("title"),
                        posted_at_stored=r.get("source_posted_at"),
                    )
            if len(page) < _PAGE:
                break
            start += _PAGE
    return dict(out)


async def _fetch_one(
    source: dict[str, Any],
    known: dict[str, KnownPosting],
    active_targets: list[JobTarget],
) -> tuple[dict[str, Any], list[StandardJob], str | None]:
    """Fetch one source through the poller's own fetcher dispatch."""
    provider = str(source.get("provider"))
    fetcher = FETCHERS.get(provider)
    if fetcher is None:
        return source, [], f"unknown provider {provider!r}"

    def _admissible(title: str, location: str | None) -> bool:
        # The identical callback ``_poll_one_source`` hands the fetcher, so
        # Workday skips the same detail requests it skips in prod.
        return _title_matches_any_target(title, active_targets) and _is_us_location(location)

    try:
        jobs = await _call_fetcher(fetcher, str(source["board_token"]), known, _admissible)
    except Exception as exc:  # one dead board must not kill the whole build
        return source, [], f"{type(exc).__name__}: {exc}"
    return source, jobs, None


async def _gather_survivors(
    sources: list[dict[str, Any]],
    known_by_source: dict[str, dict[str, KnownPosting]],
    active_targets: list[JobTarget],
    concurrency: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Fetch every sampled source and keep the free-gate survivors we do NOT hold."""
    sem = asyncio.Semaphore(concurrency)
    stats: Counter[str] = Counter()
    survivors: list[dict[str, Any]] = []

    async def _bounded(source: dict[str, Any]) -> None:
        async with sem:
            src, jobs, err = await _fetch_one(
                source, known_by_source.get(str(source["id"]), {}), active_targets
            )
        provider = str(src.get("provider"))
        if err:
            stats["fetch_errors"] += 1
            logger.warning("fetch failed %s (%s): %s", src.get("company_name"), provider, err)
            return
        stats["sources_fetched"] += 1
        known = known_by_source.get(str(src["id"]), {})
        stats["listed"] += len(jobs)
        for job in jobs:
            if job.external_id in known:
                stats["already_held"] += 1
                continue
            stats["new_to_us"] += 1
            if not _passes_free_gates(job, active_targets):
                stats["free_gate_rejected"] += 1
                continue
            stats["survivors"] += 1
            stats[f"survivors_{provider}"] += 1
            survivors.append(
                {
                    "title": job.title,
                    "provider": provider,
                    "company": src.get("company_name"),
                    "location": job.location_name,
                }
            )

    tasks = [asyncio.create_task(_bounded(s)) for s in sources]
    for done, coro in enumerate(asyncio.as_completed(tasks), start=1):
        await coro
        if done % max(1, len(tasks) // 10) == 0 or done == len(tasks):
            logger.info(
                "  fetched %d/%d sources (%d survivors)", done, len(tasks), stats["survivors"]
            )
    return survivors, dict(stats)


def _pick_titles(
    survivors: list[dict[str, Any]], want: int, rng: random.Random
) -> list[dict[str, Any]]:
    """Dedupe by normalized title, then round-robin across providers.

    Deduping is a cost decision, not a representativeness one: the same literal
    title recurs across boards, and a second copy buys tokens rather than signal.
    The pre-dedupe provider mix is reported in the fixture meta so the write-up
    can be honest about what the corpus is and is not.
    """
    seen: set[str] = set()
    by_provider: dict[str, list[dict[str, Any]]] = defaultdict(list)
    order = survivors[:]
    rng.shuffle(order)
    for s in order:
        key = " ".join(str(s["title"]).lower().split())
        if not key or key in seen:
            continue
        seen.add(key)
        by_provider[str(s["provider"])].append(s)

    picked: list[dict[str, Any]] = []
    providers = sorted(by_provider)
    cursor = dict.fromkeys(providers, 0)
    while len(picked) < want:
        progressed = False
        for p in providers:
            i = cursor[p]
            if i < len(by_provider[p]):
                picked.append(by_provider[p][i])
                cursor[p] = i + 1
                progressed = True
                if len(picked) >= want:
                    break
        if not progressed:
            break
    return picked


def _target_payload(t: JobTarget) -> dict[str, Any]:
    """The target as the fixture stores it.

    ``description`` is dropped: it is second-person prose that names employers
    (#868) and this fixture is committed to a PUBLIC repo. The Phase-1 prompt
    never reads it — ``title_triage._split_user_message`` uses ``label`` and the
    two example pools only — so nothing about the eval changes.
    """
    return {
        "id": t.id,
        "label": t.label,
        "normalized_label": t.normalized_label,
        "scoring_profile": t.scoring_profile.model_dump(mode="json"),
        "search_keywords": t.search_keywords,
        "example_promising_titles": t.example_promising_titles,
        "example_unpromising_titles": t.example_unpromising_titles,
        "role_family": t.role_family,
        "seniority_hint": t.seniority_hint,
        "app_active": t.app_active,
        "activation_status": t.activation_status,
        "profile_version": t.profile_version,
        "created_at": t.created_at.isoformat(),
        "updated_at": t.updated_at.isoformat(),
    }


def _build_fixture(
    targets: list[JobTarget],
    titles: list[dict[str, Any]],
    stats: dict[str, int],
    *,
    seed: int,
    per_provider: dict[str, int],
    survivors_total: int,
) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    for t in targets:
        for row in titles:
            title = str(row["title"])
            cases.append(
                {
                    "target_id": t.id,
                    "title": title,
                    # Which half of the workload this pair is — see module docstring.
                    "stratum": "own_gate" if _title_matches_any_target(title, [t]) else "cross_gate",
                    "provider": row["provider"],
                }
            )
    return {
        "meta": {
            "kind": "phase1_unadmitted_corpus",
            "built_at_unix": int(time.time()),
            "seed": seed,
            "sources_per_provider_requested": per_provider,
            "distinct_titles": len(titles),
            "targets": len(targets),
            "cases": len(cases),
            "survivors_before_dedupe": survivors_total,
            "title_provider_mix": dict(Counter(str(r["provider"]) for r in titles)),
            "stratum_mix": dict(Counter(c["stratum"] for c in cases)),
            "funnel": stats,
            "note": (
                "Free-gate survivors NOT present in jobs.external_id, fetched live "
                "from ATS boards and filtered through poller._passes_free_gates. "
                "Target descriptions are intentionally omitted (public repo)."
            ),
        },
        "targets": {t.id: {"target": _target_payload(t)} for t in targets},
        "cases": cases,
    }


async def main_async(args: argparse.Namespace) -> None:
    sb = create_service_client()
    if sb is None:
        raise RuntimeError("Supabase not configured — run under `railway run`.")

    active_targets = get_active_targets(sb)
    if not active_targets:
        raise RuntimeError("No active targets — nothing to triage against.")
    logger.info(
        "Active targets (%d): %s",
        len(active_targets),
        ", ".join(f"{t.label}{'' if t.app_active else ' [membership-only]'}" for t in active_targets),
    )

    per_provider = dict(_DEFAULT_SOURCES_PER_PROVIDER)
    if args.sources_per_provider:
        per_provider = dict.fromkeys(per_provider, args.sources_per_provider)

    rng = random.Random(args.seed)
    sources = _load_sources(sb, rng, per_provider)
    if args.dry_run:
        logger.info(
            "--dry-run: would fetch %d boards and emit up to %d titles × %d targets = %d cases.",
            len(sources),
            args.titles,
            len(active_targets),
            args.titles * len(active_targets),
        )
        return

    logger.info("Reading known external_ids for %d sources…", len(sources))
    known_by_source = _known_by_source(sb, [str(s["id"]) for s in sources])
    logger.info(
        "Held postings across sampled sources: %d",
        sum(len(v) for v in known_by_source.values()),
    )

    logger.info("Fetching boards (concurrency %d)…", args.concurrency)
    survivors, stats = await _gather_survivors(
        sources, known_by_source, active_targets, args.concurrency
    )
    logger.info("Funnel: %s", json.dumps(stats, sort_keys=True))
    if not survivors:
        raise RuntimeError("No free-gate survivors found — nothing to build a corpus from.")

    titles = _pick_titles(survivors, args.titles, rng)
    logger.info(
        "Kept %d distinct titles: %s",
        len(titles),
        dict(Counter(str(t["provider"]) for t in titles)),
    )

    fixture = _build_fixture(
        active_targets,
        titles,
        stats,
        seed=args.seed,
        per_provider=per_provider,
        survivors_total=len(survivors),
    )
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(fixture, indent=2, sort_keys=False))
    logger.info(
        "Wrote %s — %d cases (%s)",
        out,
        len(fixture["cases"]),
        fixture["meta"]["stratum_mix"],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--titles",
        type=int,
        default=_DEFAULT_TITLES,
        help=f"Distinct titles to keep (default {_DEFAULT_TITLES}); cases = titles × targets.",
    )
    parser.add_argument(
        "--sources-per-provider",
        type=int,
        default=None,
        help="Override the per-provider source sample with one flat number.",
    )
    parser.add_argument("--concurrency", type=int, default=_DEFAULT_CONCURRENCY)
    parser.add_argument("--seed", type=int, default=20260828)
    parser.add_argument(
        "--output",
        type=str,
        default="tests/fixtures/phase1_unadmitted_corpus.json",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan the source sample without fetching any board.",
    )
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
