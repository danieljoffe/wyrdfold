"""Keeping the skill dictionary alive: backfill + vocabulary growth.

A dictionary that never changes rots — new frameworks appear, and the catalog
picks up role families the seed vocabulary never covered. Two operations keep
it current, and both are FREE (no LLM), which is the property that makes the
whole approach work: improving the vocabulary re-enriches history instead of
only helping future postings.

``backfill_dictionary_skills`` — re-scan stored postings and write
``jobs.skills_required``. Idempotent and cheap, so it is also the "apply a
vocabulary change retroactively" button: add a term, run this, and every
historical posting that mentions it becomes findable. The LLM equivalent
would mean re-paying for the entire catalog.

``vocabulary_candidates`` — what the dictionary should learn next, mined from
data already being collected:

* HARVEST TERMS. The Phase-2 grader extracts skills as a byproduct of grading
  we already pay for, using a full-JD LLM read. Terms it emits that the
  dictionary does not know, ranked by how many postings mention them, are the
  primary feed — this is exactly how the seed vocabulary was built (from 748
  jobs of harvest output).
* UNMATCHED SEARCH QUERIES. What users actually typed and the dictionary could
  not match. Proven demand beats guesswork: a search for "svelte" that finds
  nothing is a stronger signal than any list of trending frameworks.
* PER-FAMILY COVERAGE. The share of live postings in each ``role_family`` that
  carry at least one skill. The dictionary's known weakness is non-technical
  disciplines, so this turns a silent gap into a monitored number — a family
  trending down is the cue to add domain vocabulary there.

Adding a term stays a human decision in a reviewed file (is ``observability`` a
skill or a category?). That judgement gets made ONCE, deliberately, instead of
being re-litigated per posting by a model — which is why the vocabulary stays
canonical while an LLM's fragmented into 1,757 values, 68% of them singletons.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any, cast

from supabase import AsyncClient

from app.services.qualification.skill_dictionary import (
    VOCABULARY,
    extract_skills,
    unknown_terms,
)

logger = logging.getLogger(__name__)

# Rows per scan page. The read carries description_html (heavy), so keep the
# page small enough that one batch is a modest payload.
_PAGE = 200

# PostgREST clamps any response to 1,000 rows, so the coverage scan pages at
# that size. The cap bounds an admin request on an ever-growing catalog.
_COVERAGE_PAGE = 1000
_COVERAGE_MAX_ROWS = 50_000

# A term needs this many postings before it is worth proposing. Below it the
# candidate list becomes noise — one-off phrasings a model invented once.
MIN_CANDIDATE_MENTIONS = 5


async def backfill_dictionary_skills(
    supabase: AsyncClient,
    *,
    limit: int = 2000,
    only_missing: bool = True,
) -> dict[str, int]:
    """Re-scan stored postings and write ``skills_required``.

    ``only_missing`` (default) fills gaps — the normal case after shipping or
    after adding vocabulary. ``False`` re-scans everything, which is what a
    vocabulary change wants once the new term should apply to postings that
    already have some skills.

    Returns counts; never raises for a single bad row.
    """
    scanned = written = 0
    offset = 0
    while scanned < limit:
        # Rows on this page that will still match the filter next time.
        unmatched = 0
        page = min(_PAGE, limit - scanned)
        query = (
            supabase.table("jobs")
            .select("id, title, description_html")
            .is_("archived_at", "null")
            .is_("purged_at", "null")
            .order("cataloged_at", desc=True)
            .range(offset, offset + page - 1)
        )
        if only_missing:
            query = query.is_("skills_required", "null")
        try:
            resp = await query.execute()
        except Exception:
            logger.exception("skill backfill: page read failed at offset %d", offset)
            break
        rows = cast(list[dict[str, Any]], resp.data or [])
        if not rows:
            break
        scanned += len(rows)
        for row in rows:
            skills = extract_skills(row.get("title"), row.get("description_html"))
            if not skills:
                # No dictionary hit. Under `only_missing` this row keeps
                # `skills_required IS NULL`, so it STAYS in the result set —
                # the offset arithmetic below has to step over it.
                unmatched += 1
                continue
            try:
                await (
                    supabase.table("jobs")
                    .update({"skills_required": skills})
                    .eq("id", row["id"])
                    .execute()
                )
                written += 1
            except Exception:
                logger.warning("skill backfill: write failed for %s", row.get("id"))
                unmatched += 1  # still matches the filter; step over it too
        # Advance past exactly the rows that did NOT leave the result set.
        #
        # Under `only_missing` the filter is `skills_required IS NULL`, so a
        # WRITTEN row drops out and the rows behind it shift forward —
        # advancing by the full page would skip them. That was the original
        # reasoning, and it was right about written rows and wrong about the
        # rest: a row with no dictionary hit is `continue`d WITHOUT a write, so
        # it stays NULL and stays at the head. With the offset pinned at 0,
        # every subsequent page re-read those same rows, and the scan
        # livelocked as soon as enough of them accumulated at the front.
        # Observed on prod minutes after release: `scanned 500, written 0` with
        # coverage at 0%, on the DEFAULT (`only_missing=true`) path.
        offset += unmatched if only_missing else len(rows)
        if len(rows) < page:
            break
    return {"scanned": scanned, "written": written}


async def vocabulary_candidates(supabase: AsyncClient, *, limit: int = 40) -> dict[str, Any]:
    """What the dictionary should learn next. Read-only, free, no LLM."""
    out: dict[str, Any] = {
        "vocabulary_size": len(VOCABULARY),
        "from_harvest": [],
        "from_searches": [],
        "family_coverage": [],
    }

    # 1. Phase-2 harvest terms the dictionary doesn't know.
    try:
        resp = await (
            supabase.table("scores")
            .select("skills_required")
            .not_.is_("skills_required", "null")
            .limit(5000)
            .execute()
        )
        counter: Counter[str] = Counter()
        for row in cast(list[dict[str, Any]], resp.data or []):
            counter.update(unknown_terms(row.get("skills_required")))
        out["from_harvest"] = [
            {"term": t, "postings": n}
            for t, n in counter.most_common(limit)
            if n >= MIN_CANDIDATE_MENTIONS
        ]
    except Exception:
        logger.warning("vocabulary candidates: harvest scan failed", exc_info=True)

    # 2. Searches whose query names nothing the dictionary knows — demand with
    #    no supply. A single-word query is the strong case ("svelte"); longer
    #    phrases are usually role searches, not skill searches.
    try:
        resp = await supabase.table("search_events").select("query").limit(5000).execute()
        q_counter: Counter[str] = Counter()
        for row in cast(list[dict[str, Any]], resp.data or []):
            raw = row.get("query")
            if not isinstance(raw, str):
                continue
            term = " ".join(raw.lower().split())
            if term and len(term.split()) <= 2 and term not in VOCABULARY:
                q_counter[term] += 1
        out["from_searches"] = [
            {"query": t, "searches": n} for t, n in q_counter.most_common(limit)
        ]
    except Exception:
        logger.warning("vocabulary candidates: search scan failed", exc_info=True)

    # 3. Per-family coverage — the blind-spot metric.
    #
    # PAGINATED, because `.limit(20000)` does NOT return 20,000 rows: PostgREST
    # clamps a response to 1,000 regardless of the requested limit (the same
    # clamp this repo already hit in the poller). Unpaginated, this metric
    # silently described the first 1,000 jobs of a 16k catalog — and since the
    # backfill walks `cataloged_at DESC` while this scan has no order at all,
    # the two barely overlapped. It reported 0% coverage immediately after a
    # run that had demonstrably written 1,566 rows, which is worse than no
    # metric: a blind-spot monitor that always reads zero can never alarm.
    try:
        total: Counter[str] = Counter()
        with_skills: Counter[str] = Counter()
        offset = 0
        while offset < _COVERAGE_MAX_ROWS:
            resp = await (
                supabase.table("jobs")
                .select("role_family, skills_required")
                .is_("archived_at", "null")
                .is_("purged_at", "null")
                .order("id")
                .range(offset, offset + _COVERAGE_PAGE - 1)
                .execute()
            )
            rows = cast(list[dict[str, Any]], resp.data or [])
            for row in rows:
                fam = row.get("role_family") or "untagged"
                total[fam] += 1
                if row.get("skills_required"):
                    with_skills[fam] += 1
            if len(rows) < _COVERAGE_PAGE:
                break
            offset += len(rows)
        out["family_coverage"] = sorted(
            (
                {
                    "role_family": fam,
                    "jobs": n,
                    "with_skills_pct": round(100.0 * with_skills[fam] / n, 1),
                }
                for fam, n in total.items()
            ),
            key=lambda r: cast(int, r["jobs"]),
            reverse=True,
        )
    except Exception:
        logger.warning("vocabulary candidates: coverage scan failed", exc_info=True)

    return out
