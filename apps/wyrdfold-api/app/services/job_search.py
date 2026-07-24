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

import re
from typing import Any, cast

from supabase import Client

from app.models.job_search import JobSearchResult

# Light projection — NEVER select ``description_html`` (heavy; the public row is
# a preview that links to the source) nor the legacy ``score``/``score_breakdown``
# columns (search must carry no match score).
_SEARCH_COLS = (
    "id, title, company_name, location, department, "
    "salary_text, absolute_url, first_seen_at, created_at"
)

# How many title-matching candidates to pull for Python re-ranking, and the hard
# ceiling on returned rows. A single capped page (no deep pagination) bounds
# corpus enumeration by scrapers (#467 abuse decision).
_CANDIDATE_CAP = 100
MAX_PAGE_SIZE = 25
DEFAULT_PAGE_SIZE = 20

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
    recency = str(row.get("created_at") or row.get("first_seen_at") or "")
    return (overlap, recency)


def search_jobs(
    supabase: Client, *, q: str, limit: int = DEFAULT_PAGE_SIZE
) -> list[JobSearchResult]:
    """Search the live, US jobs corpus by title. Service-role client (the corpus
    is shared, non-user data). Returns a single ranked, capped page.

    Empty/whitespace/punctuation-only queries return ``[]`` (the honest empty
    state — see #467). ``limit`` is clamped to ``[1, MAX_PAGE_SIZE]``.
    """
    limit = max(1, min(limit, MAX_PAGE_SIZE))
    tokens = _tokenize(q)
    if not tokens:
        return []

    # OR of ``title ILIKE *form*`` across every query token's synonym forms —
    # index-backed via the title trigram GIN. Tokens are sanitized above.
    ilike_terms = sorted({form for tok in tokens for form in _forms_for(tok)})
    or_filter = ",".join(f"title.ilike.*{term}*" for term in ilike_terms)

    resp = (
        supabase.table("jobs")
        .select(_SEARCH_COLS)
        # Live + US corpus gate (mirrors the scores-layer live predicate:
        # archived_at IS NULL AND purged_at IS NULL AND is_us IS NOT FALSE).
        .is_("archived_at", "null")
        .is_("purged_at", "null")
        .not_.is_("is_us", "false")
        .or_(or_filter)
        # Bias the capped candidate pull toward recent postings before re-ranking.
        .order("created_at", desc=True)
        .limit(_CANDIDATE_CAP)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])

    query_groups = _groups(tokens)
    rows.sort(key=lambda r: _rank_key(query_groups, r), reverse=True)
    return [JobSearchResult.model_validate(r) for r in rows[:limit]]
