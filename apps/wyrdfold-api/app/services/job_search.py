"""Public job search — model-free keyword/title search over the live corpus (#467).

No LLM, no embeddings, no per-user scoring. The query runs **directly against
``jobs``** (skip ``scores``/targets/dedup entirely — ``jobs`` is already one row
per posting, ``id`` PK / ``(source_id, external_id)`` unique), gated to the live,
US corpus.

Ranking is **synonym-aware title-token overlap**, chosen over raw trigram
similarity because trigram is character-level: it would rank "backend developer"
*high* for the query "frontend developer" (they share "…end developer"), the
opposite of the intent. Here "developer" ≈ "engineer" (same group) but
"frontend" ≠ "backend" (distinct, distinguishing groups), so "frontend engineer"
outranks "backend developer" for "frontend developer" — the #467 acceptance
example. The DB pre-filter (an OR of ``title ILIKE`` over each query token's
synonym forms) rides the existing ``idx_job_postings_title_trgm`` GIN index; the
Python re-rank supplies the precision trigram can't.

Accepted V1 limitation (agreed in #467): obliquely-named roles ("Software
Engineer, UI Platform" ≈ frontend) are missed. The synonym map is curated +
easily extended; embedding re-rank is a later, logged-in-only enhancement.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from supabase import AsyncClient

from app.models.job_search import JobSearchResult
from app.services.fit.job_fit import normalize_skill
from app.services.scoring import strip_html

logger = logging.getLogger(__name__)

# Light projection — NEVER select ``description_html`` (heavy; the public row is
# a preview that links to the source) nor the legacy ``score``/``score_breakdown``
# columns (search must carry no match score).
_SEARCH_COLS = (
    "id, title, title_display, company_name, location, city, state, country, location_remote, "
    "salary_text, salary_min, salary_max, salary_currency, "
    "salary_period, absolute_url, source_posted_at, cataloged_at"
)

# Salary-floor ceiling — mirrors the parser's plausibility cap (no real posted
# salary exceeds $5M/yr), so the router bound and the data agree.
MAX_SALARY_FLOOR = 5_000_000

# Upper bound on skills in one filter. A facet UI sends a handful; a longer
# list is either abuse or a bug, and every added term shrinks the result set
# anyway (containment is AND).
MAX_SKILL_FILTER_TERMS = 8

# How many title-matching candidates to pull for Python re-ranking. This also
# bounds how deep pagination can go (we rank the candidate window, then page
# through it): ~250 ranked matches across ~12 pages is ample for the curated
# corpus and keeps the light-column fetch cheap on the small instance. Truly
# unbounded paging would need DB-side ranking (an RPC) — a later optimization.
_CANDIDATE_CAP = 250
MAX_PAGE_SIZE = 25
DEFAULT_PAGE_SIZE = 20
# Hard ceiling on the pagination offset — there's nothing to page past the
# ranked candidate window, so reject offsets beyond it at the edge.
MAX_OFFSET = _CANDIDATE_CAP
# Recency-filter ceiling — a year back is effectively "any time" for this corpus,
# and bounding it keeps the DB predicate sane.
MAX_POSTED_WITHIN_DAYS = 365

# Public-row preview length. Long enough to judge a role, short enough that we're
# not republishing the JD (exposure decision: link to the source for the full text).
SNIPPET_MAX_LEN = 180

# Canonical role/seniority groups → surface forms (all lowercase). Members of a
# group are treated as synonyms for both the DB pre-filter and the overlap rank.
# Deliberately small + curated; grow as real queries reveal gaps.
_SYNONYMS: dict[str, set[str]] = {
    "engineer": {
        "engineer",
        "engineers",
        "engineering",
        "eng",
        "developer",
        "developers",
        "dev",
        "devs",
        "programmer",
        "swe",
        "sde",
    },
    "frontend": {"frontend", "front-end", "fe"},
    "backend": {"backend", "back-end", "be"},
    "fullstack": {"fullstack", "full-stack"},
    "senior": {"senior", "sr"},
    "staff": {"staff"},
    "principal": {"principal"},
    "junior": {"junior", "jr", "entry"},
    "lead": {"lead"},
    "manager": {"manager", "mgr", "management"},
    "designer": {"designer", "design"},
    "product": {"product"},
    "data": {"data"},
    "mobile": {"mobile"},
    "platform": {"platform", "infrastructure", "infra"},
    "security": {"security", "appsec", "infosec"},
    "devops": {"devops", "sre"},
}

# Reverse index: surface form → canonical group. A token absent here is its own
# group (so novel keywords like "kubernetes" still filter + rank on themselves).
_FORM_TO_CANON: dict[str, str] = {
    form: canon for canon, forms in _SYNONYMS.items() for form in forms
}

# Keep only lowercase alphanumerics + hyphen — neutralizes PostgREST filter
# metacharacters (``,`` ``(`` ``)`` ``*`` ``:``) so a query string can't inject
# into the ``or_`` chain.
_SAFE_TOKEN_RE = re.compile(r"[^a-z0-9-]")


def _tokenize(raw: str) -> list[str]:
    """Lowercase, split on whitespace, sanitize; dedupe preserving order."""
    seen: set[str] = set()
    out: list[str] = []
    for tok in raw.lower().split():
        t = _SAFE_TOKEN_RE.sub("", tok).strip("-")
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def _canon(token: str) -> str:
    return _FORM_TO_CANON.get(token, token)


def _groups(tokens: list[str]) -> set[str]:
    """Canonical group set for a token list."""
    return {_canon(t) for t in tokens}


def _forms_for(token: str) -> set[str]:
    """All surface forms to ILIKE-match for a query token (its whole synonym
    group, or just itself for a novel keyword)."""
    canon = _canon(token)
    return _SYNONYMS.get(canon, {token})


def _rank_key(query_groups: set[str], row: dict[str, Any]) -> tuple[int, str]:
    """Sort key: more overlapping query groups first, then most-recent.

    Overlap = how many of the query's canonical groups appear in the title. For
    "frontend developer" (groups {frontend, engineer}): "frontend engineer" = 2,
    "backend developer" = 1 → the frontend role ranks above the backend one.
    """
    title_groups = _groups(_tokenize(str(row.get("title") or "")))
    overlap = len(query_groups & title_groups)
    # created_at is ISO8601 → lexical sort == chronological; missing sorts last.
    recency = str(row.get("source_posted_at") or row.get("cataloged_at") or "")
    return (overlap, recency)


async def search_jobs(
    supabase: AsyncClient,
    *,
    q: str,
    limit: int = DEFAULT_PAGE_SIZE,
    offset: int = 0,
    location: str | None = None,
    posted_within_days: int | None = None,
    salary_floor: int | None = None,
    skills: list[str] | None = None,
) -> tuple[list[JobSearchResult], bool]:
    """Search the live, US jobs corpus by title. Service-role client (the corpus
    is shared, non-user data).

    Returns ``(page, has_more)``: the ranked results in
    ``[offset, offset + limit)`` and whether more ranked matches remain (up to
    the candidate window). Blank/whitespace queries BROWSE the live corpus
    newest-first (#834); only a non-blank query that sanitizes to no
    searchable tokens (all punctuation) returns ``([], False)``. ``limit``
    is clamped to ``[1, MAX_PAGE_SIZE]``.

    Optional refinements (both narrow the same candidate window the ranker sees):
    ``posted_within_days`` filters ``created_at`` DB-side (a clean date bound,
    clamped to ``[1, MAX_POSTED_WITHIN_DAYS]``); ``location`` is a case-
    insensitive substring match applied in Python over the fetched candidates
    (mirrors the ``/jobs`` location filter — avoids PostgREST ilike wildcard/
    escaping over a free-text value, and location text is too irregular to index
    on anyway). Both are refinements over the title match, so they operate within
    the capped candidate window — the same V1 bound as pagination.

    ``skills`` narrows to postings whose extracted ``skills_required`` contains
    EVERY named skill (jsonb ``@>``, GIN-indexed) — "frontend" + react + node.js
    means all three, not any. Values are normalized through the harvest's
    ``normalize_skill`` so a caller's "React" matches the stored "react": the
    filter is exact-string containment, and a casing mismatch would silently
    return zero rather than erroring. Applied DB-side so non-matching rows never
    consume candidate-window slots.

    Coverage caveat worth knowing at the call site: ``skills_required`` is
    populated forward-only by the qualification tagger (every newly-tagged job)
    plus the Phase-2 harvest for graded rows — historical jobs were NOT
    backfilled, so a skill filter reaches only the tagged-since-2026-08-15
    corpus and grows as the catalog turns over.

    ``salary_floor`` (USD/year) keeps rows whose posted range REACHES the floor
    (``salary_max >= floor``, or ``salary_min >= floor`` for min-only rows) —
    and therefore only rows carrying structured yearly-USD salary at all:
    "pays at least X" is a claim salary-less rows can't make. Hourly and
    unknown-period rows are excluded rather than fake-annualized. Applied
    DB-side (a clean numeric predicate on validated ints) so sub-floor rows
    never waste candidate-window slots.
    """
    limit = max(1, min(limit, MAX_PAGE_SIZE))
    offset = max(0, offset)
    tokens = _tokenize(q)
    if q.strip() and not tokens:
        # A real query that sanitizes away to nothing (all punctuation) can
        # match nothing — do NOT fall through to browse, or "),(*:" would
        # return the whole pool.
        return [], False

    query = (
        supabase.table("jobs")
        .select(_SEARCH_COLS)
        # Live + US corpus gate (mirrors the scores-layer live predicate:
        # archived_at IS NULL AND purged_at IS NULL AND is_us IS NOT FALSE).
        .is_("archived_at", "null")
        .is_("purged_at", "null")
        .not_.is_("is_us", "false")
    )
    if tokens:
        # OR of ``title ILIKE *form*`` across every query token's synonym forms
        # — index-backed via the title trigram GIN. Tokens are sanitized above.
        ilike_terms = sorted({form for tok in tokens for form in _forms_for(tok)})
        query = query.or_(",".join(f"title.ilike.*{term}*" for term in ilike_terms))
    # A blank query BROWSES the pool (#834): same corpus gate, filters and
    # capped recency-biased window — just no title clause. With no query
    # groups every row's overlap is 0, so the ranker below degrades to pure
    # recency: newest-first, which is exactly what a browse should show.
    if posted_within_days is not None:
        days = max(1, min(posted_within_days, MAX_POSTED_WITHIN_DAYS))
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        # "Posted within N days": provider date when known, cataloged time
        # otherwise (PostgREST or_ keeps it one round-trip).
        query = query.or_(
            f"source_posted_at.gte.{cutoff},and(source_posted_at.is.null,cataloged_at.gte.{cutoff})"
        )
    if salary_floor is not None:
        floor = max(1, min(int(salary_floor), MAX_SALARY_FLOOR))
        query = (
            query.eq("salary_currency", "USD")
            .eq("salary_period", "yearly")
            .or_(f"salary_max.gte.{floor},and(salary_max.is.null,salary_min.gte.{floor})")
        )
    wanted_skills = normalize_skill_filter(skills)
    if wanted_skills:
        # jsonb containment — index-backed by idx_jobs_skills_required (GIN).
        #
        # The value MUST be a JSON-encoded array string, not a Python list:
        # postgrest-py renders a list as a Postgres array literal (``cs.{react}``),
        # which a JSONB column rejects with 22P02 — surfaced as a 404 by the
        # app-wide PostgREST handler, i.e. a silently empty facet rather than an
        # error. Verified against real PostgREST (a mock asserting the call
        # arguments cannot catch this).
        query = query.filter("skills_required", "cs", json.dumps(wanted_skills))

    resp = await (
        # Bias the capped candidate pull toward recent postings before re-ranking.
        query.order("cataloged_at", desc=True).limit(_CANDIDATE_CAP).execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])

    # Location refine (Python, post-fetch): case-insensitive substring over the
    # candidate window. A blank/whitespace value is a no-op. Matches the raw
    # string AND the parsed parts (#518) so canonical forms hit regardless of
    # how the board spelled it — "california" finds rows whose raw says only
    # "CA" (state=CA expands nothing, but city/state/country hold canonical
    # spellings like "San Francisco"/"CA"/"US" that the raw may lack).
    if location and location.strip():
        needle = location.strip().lower()
        rows = [
            r
            for r in rows
            if any(
                needle in str(r.get(field) or "").lower()
                for field in ("location", "city", "state", "country")
            )
        ]

    query_groups = _groups(tokens)
    rows.sort(key=lambda r: _rank_key(query_groups, r), reverse=True)
    page = rows[offset : offset + limit]
    has_more = len(rows) > offset + limit
    return [JobSearchResult.model_validate(r) for r in page], has_more


async def get_listing(supabase: AsyncClient, listing_id: str) -> JobSearchResult | None:
    """One publicly-eligible listing by id — the shareable-URL detail read
    (#467 §11.2 fast-follow).

    Returns the SAME projection as one ``search_jobs`` result (``_SEARCH_COLS``
    + the page-only ``snippet``), gated by the SAME live/US eligibility
    predicate. A missing id and a no-longer-eligible row (archived / purged /
    non-US) both return ``None`` — the caller maps both to one indistinguishable
    404, so a public prober can't learn whether a delisted id ever existed.

    Native async round-trips (row fetch + snippet fetch) on the pooled async
    service client (#57 slice 4), mirroring ``search_jobs_with_snippets``.
    """
    resp = await (
        supabase.table("jobs")
        .select(_SEARCH_COLS)
        # Live + US corpus gate — MUST match search_jobs exactly, or a shared
        # link could resurface a listing the search surface no longer shows.
        .is_("archived_at", "null")
        .is_("purged_at", "null")
        .not_.is_("is_us", "false")
        .eq("id", listing_id)
        .limit(1)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    if not rows:
        return None
    result = JobSearchResult.model_validate(rows[0])
    await attach_snippets(supabase, [result])
    return result


def _html_to_snippet(html: str | None, max_len: int = SNIPPET_MAX_LEN) -> str | None:
    """Tag-strip + whitespace-collapse + truncate a JD's ``description_html`` into
    a short plaintext preview. ``None`` for empty/blank input; appends an ellipsis
    when truncated.

    Delegates the strip to :func:`app.services.scoring.strip_html` — the ONE
    shared implementation of the escaped-HTML-defensive double-strip (see its
    docstring for the Greenhouse stored-escaped-rows story), so the snippet,
    keyword scoring, and salary extraction can never disagree about the text.
    """
    if not html:
        return None
    text = strip_html(html)
    if not text:
        return None
    if len(text) <= max_len:
        return text
    return text[:max_len].rstrip() + "…"


async def attach_snippets(
    supabase: AsyncClient,
    results: list[JobSearchResult],
    *,
    max_len: int = SNIPPET_MAX_LEN,
) -> None:
    """Populate each result's ``snippet`` IN PLACE from a bounded, PAGE-ONLY fetch
    of ``description_html``.

    ``description_html`` is the heavy column deliberately excluded from the ranked
    candidate query (:data:`_SEARCH_COLS`), so this fetches it for ONLY the ≤page
    result ids — never the ~250-row candidate window. **Public surface only**: the
    authed search leaves snippets ``None``, sparing its hot path this extra read.

    Best-effort: a fetch failure logs and leaves snippets ``None`` rather than
    failing the search — a snippet is an enhancement, not the result. Runs
    natively on the pooled async service client (#57 slice 4).
    """
    if not results:
        return
    ids = [r.id for r in results]  # ≤ page size, so no in_() chunking (#414 guard)
    try:
        resp = await supabase.table("jobs").select("id, description_html").in_("id", ids).execute()
        rows = cast(list[dict[str, Any]], resp.data or [])
    except Exception:
        logger.warning("snippet fetch failed; leaving snippets empty", exc_info=True)
        return
    by_id = {row["id"]: row.get("description_html") for row in rows}
    for r in results:
        r.snippet = _html_to_snippet(by_id.get(r.id), max_len)


def normalize_skill_filter(skills: list[str] | None) -> list[str]:
    """Caller-supplied skill terms → the stored vocabulary, deduped and capped.

    Shares ``normalize_skill`` with the write side (tagger + Phase-2 harvest),
    which is the whole point: the DB filter is exact-string jsonb containment,
    so read and write MUST agree on casing/spacing or the facet silently
    returns nothing. Empty/whitespace terms drop out; ``None`` yields ``[]``
    (no filter).
    """
    if not skills:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in skills:
        # No isinstance guard: FastAPI validates the ``list[str]`` query param
        # at the boundary, so a non-string can't reach here (mypy agrees — the
        # check was dead code).
        norm = normalize_skill(raw)
        if not norm or len(norm) > 60 or norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
        if len(out) >= MAX_SKILL_FILTER_TERMS:
            break
    return out


async def search_jobs_with_snippets(
    supabase: AsyncClient,
    *,
    q: str,
    limit: int = DEFAULT_PAGE_SIZE,
    offset: int = 0,
    location: str | None = None,
    posted_within_days: int | None = None,
    salary_floor: int | None = None,
    skills: list[str] | None = None,
) -> tuple[list[JobSearchResult], bool]:
    """``search_jobs`` + the page-only snippet fetch, both native async round-trips
    on the pooled async service client (#57 slice 4).

    Used by BOTH the authed and public search endpoints: the card-grid UX (#467
    §11) shows a snippet on every result, so the preview is no longer public-only.
    The extra read is bounded to the ≤page ids and cached — see ``attach_snippets``.
    """
    results, has_more = await search_jobs(
        supabase,
        q=q,
        limit=limit,
        offset=offset,
        location=location,
        posted_within_days=posted_within_days,
        salary_floor=salary_floor,
        skills=skills,
    )
    await attach_snippets(supabase, results)
    return results, has_more
