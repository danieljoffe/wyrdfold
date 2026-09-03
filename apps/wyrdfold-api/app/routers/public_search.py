"""Public (unauthenticated) job-search router (#467 — V1 slice 1).

The unauth counterpart to the authed ``job_search`` router: the SAME model-free
``search_jobs`` service, exposed to logged-out visitors as the growth-funnel
surface. Public users never touch LLM or embeddings — ``search_jobs`` is
un-personalized by construction (no per-user scoring, and it never selects the
JD body).

SECURITY POSTURE — mirrors the waitlist, the app's other public endpoint:
  - **BFF-ONLY** (``require_bff_secret``): reachable only via the trusted Next.js
    BFF, which injects the shared secret. A direct hit to Railway can't spoof
    ``X-Forwarded-For`` to rotate past the per-IP limit. NB ``require_bff_secret``
    *fails open* when the secret is unset — so setting ``WYRDFOLD_BFF_SECRET`` on
    BOTH Railway and Vercel is a launch gate for this scrape-sensitive route
    (documented in docs/job-search-public-design.md §10).
  - **Per-client-IP rate-limited**, tighter than the authed route (the limiter
    keys on client IP for tokenless callers).
  - **Hard result-depth cap** (``page_size`` ≤ :data:`PUBLIC_MAX_PAGE_SIZE`,
    ``offset`` ≤ :data:`PUBLIC_MAX_OFFSET`) so anonymous callers can't enumerate
    the whole corpus — the moat's raw material. The authed route keeps its deeper
    cap (``job_search.MAX_PAGE_SIZE`` / ``MAX_OFFSET``).
  - **Reads via the service-role client** (shared, non-user corpus). Results carry
    NO match score and NO JD body — a preview that links to the source posting;
    the full JD + "how you match" stay behind login.
  - **Cached**: responses are fully anonymous. The public projection carries a
    ``snippet`` the authed route doesn't, so it uses its OWN cache namespace
    (``publicsearch:``) rather than sharing the authed ``jobsearch:`` entries.
"""

from __future__ import annotations

import logging
from typing import cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from supabase import AsyncClient

from app.cache import job_list_cache, make_cache_key
from app.dependencies import get_async_service_supabase, require_bff_secret
from app.models.job_search import JobSearchResponse, JobSearchResult
from app.rate_limit import limiter
from app.services import job_search, search_events

logger = logging.getLogger(__name__)

# Public callers get a HARDER cap than the authed route (``job_search.MAX_*``):
# ~2 shallow pages, so the shared corpus can't be deep-enumerated logged-out.
PUBLIC_MAX_PAGE_SIZE = 20
PUBLIC_MAX_OFFSET = 40

router = APIRouter(
    tags=["public-job-search"],
    # BFF-only — see the module docstring. The per-IP limit below is only
    # spoof-proof if the endpoint can't be hit directly with a forged
    # X-Forwarded-For header.
    dependencies=[Depends(require_bff_secret)],
)

# Own cache namespace — the public route keeps its harder caps + BFF gate, so it
# holds entries separate from the authed route even though the projection (now
# with ``snippet`` on both) matches. Search + snippets is the shared service helper.
_CACHE_PREFIX = "publicsearch:"

# Single-listing cache namespace (#467 §11.2 fast-follow) — shared TTL cache,
# distinct prefix so listing entries never collide with search pages.
_LISTING_CACHE_PREFIX = "publiclisting:"


@router.get("/public/search", response_model=JobSearchResponse)
@limiter.limit("10/minute;60/hour")
async def public_search_endpoint(
    request: Request,
    q: str = Query(
        "",
        max_length=120,
        description="Title / keyword query; blank browses the pool newest-first (#834)",
    ),
    page_size: int = Query(
        PUBLIC_MAX_PAGE_SIZE,
        ge=1,
        le=PUBLIC_MAX_PAGE_SIZE,
        description="Results per page (hard-capped for public callers)",
    ),
    offset: int = Query(
        0,
        ge=0,
        le=PUBLIC_MAX_OFFSET,
        description="Pagination offset (hard-capped for public callers)",
    ),
    location: str | None = Query(
        None, max_length=100, description="Case-insensitive location substring filter"
    ),
    posted_within_days: int | None = Query(
        None,
        ge=1,
        le=job_search.MAX_POSTED_WITHIN_DAYS,
        description="Only postings created within the last N days",
    ),
    salary_floor: int | None = Query(
        None,
        ge=1,
        le=job_search.MAX_SALARY_FLOOR,
        description="Only postings whose yearly USD salary range reaches this floor",
    ),
    skill: list[str] | None = Query(
        None,
        description=(
            "Narrow to postings requiring ALL named skills (repeatable). "
            "Matched against tagger-extracted skills; normalized, so "
            "'React' and 'react' are equivalent."
        ),
    ),
    supabase: AsyncClient = Depends(get_async_service_supabase),
) -> JobSearchResponse:
    """Public keyword search over the live, US jobs corpus (one shallow page).

    Unauthenticated: no session, no per-user data, no match score, no JD body.
    ``request`` is required by slowapi to key the per-IP limit. Enumeration is
    bounded by the hard ``page_size`` / ``offset`` caps above (tighter than the
    authed route). The projection matches the authed route exactly, so responses
    share the anonymous ``job_list_cache``.
    """
    cache_key = make_cache_key(
        _CACHE_PREFIX,
        q=q.strip().lower(),
        page_size=page_size,
        offset=offset,
        location=(location or "").strip().lower(),
        posted_within_days=posted_within_days or 0,
        salary_floor=salary_floor or 0,
        skills=",".join(job_search.normalize_skill_filter(skill)),
    )

    def _instrument(resp: JobSearchResponse) -> None:
        # Fire-and-forget funnel metrics (#467 §10 PR6): O(1) in-memory
        # enqueue, flushed in bulk by a background task — never a request
        # round-trip. Cache hits count too (still a user search).
        search_events.record_search(
            surface="public",
            q=q,
            result_count=resp.count,
            has_more=resp.has_more,
            offset=offset,
            location=location,
            posted_within_days=posted_within_days,
        )

    cached = job_list_cache.get(cache_key)
    if cached is not None:
        response = cast(JobSearchResponse, cached)
        _instrument(response)
        return response

    # Native async round-trips on the pooled service client (#57 slice 4): search
    # + the page snippet fetch.
    results, has_more = await job_search.search_jobs_with_snippets(
        supabase,
        q=q,
        limit=page_size,
        offset=offset,
        location=location,
        posted_within_days=posted_within_days,
        salary_floor=salary_floor,
        skills=skill,
    )
    # The service's ``has_more`` reports corpus depth against the AUTHED
    # candidate window; this surface refuses any offset past
    # ``PUBLIC_MAX_OFFSET``. Past the last fetchable page the honest answer
    # is "no more" — otherwise the client renders a "Load more" whose only
    # possible outcome is a 422 (#832). Clamped before caching so cached
    # pages carry the same answer.
    if offset + page_size > PUBLIC_MAX_OFFSET:
        has_more = False
    response = JobSearchResponse(query=q, count=len(results), has_more=has_more, results=results)
    job_list_cache.set(cache_key, response)
    _instrument(response)
    return response


@router.get("/public/listings/{listing_id}", response_model=JobSearchResult)
@limiter.limit("30/minute;300/hour")
async def public_listing_endpoint(
    request: Request,
    listing_id: UUID,
    supabase: AsyncClient = Depends(get_async_service_supabase),
) -> JobSearchResult:
    """One public listing by id — the shareable `/search/<id>` URL's data read
    (#467 §11.2 fast-follow).

    Same posture as ``/public/search`` (router-level BFF gate, per-IP limit,
    service-role read of the shared corpus) and the SAME preview projection: no
    match score, no JD body — snippet + source link only. ``listing_id`` is a
    typed UUID, so junk ids 422 at the edge without touching the service.

    404s never leak existence: a missing id and a no-longer-eligible row
    (archived / purged / non-US) are indistinguishable (the service returns
    ``None`` for both). NO search_events row is recorded here — opening a
    shared link is not a search, and the ``card_open`` funnel tick stays a
    client-side beacon. ``request`` is required by slowapi.
    """
    cache_key = make_cache_key(_LISTING_CACHE_PREFIX, listing_id=str(listing_id))
    cached = job_list_cache.get(cache_key)
    if cached is not None:
        return cast(JobSearchResult, cached)

    # Native async round-trips on the pooled service client (row + snippet, #57
    # slice 4).
    result = await job_search.get_listing(supabase, str(listing_id))
    if result is None:
        raise HTTPException(status_code=404, detail="Listing not found")
    job_list_cache.set(cache_key, result)
    return result
