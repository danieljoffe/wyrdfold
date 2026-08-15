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
        # When filling gaps the written rows leave the result set, so the
        # window must NOT advance or it skips the rows that shift into place.
        if not only_missing:
            offset += len(rows)
        for row in rows:
            skills = extract_skills(row.get("title"), row.get("description_html"))
            if not skills:
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
        if len(rows) < page:
            break
    return {"scanned": scanned, "written": written}


async def vocabulary_candidates(
    supabase: AsyncClient, *, limit: int = 40
) -> dict[str, Any]:
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
        resp = await (
            supabase.table("search_events")
            .select("query")
            .limit(5000)
            .execute()
        )
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
    try:
        resp = await (
            supabase.table("jobs")
            .select("role_family, skills_required")
            .is_("archived_at", "null")
            .is_("purged_at", "null")
            .limit(20000)
            .execute()
        )
        total: Counter[str] = Counter()
        with_skills: Counter[str] = Counter()
        for row in cast(list[dict[str, Any]], resp.data or []):
            fam = row.get("role_family") or "untagged"
            total[fam] += 1
            if row.get("skills_required"):
                with_skills[fam] += 1
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
