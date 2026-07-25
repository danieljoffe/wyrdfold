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

import asyncio
import logging
from typing import cast

from fastapi import APIRouter, Depends, Query, Request
from supabase import Client

from app.cache import job_list_cache, make_cache_key
from app.dependencies import get_supabase, require_bff_secret
from app.models.job_search import JobSearchResponse
from app.rate_limit import limiter
from app.services import job_search

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


@router.get("/public/search", response_model=JobSearchResponse)
@limiter.limit("10/minute;60/hour")
async def public_search_endpoint(
    request: Request,
    q: str = Query(..., min_length=1, max_length=120, description="Title / keyword query"),
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
    supabase: Client = Depends(get_supabase),
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
    )
    cached = job_list_cache.get(cache_key)
    if cached is not None:
        return cast(JobSearchResponse, cached)

    # supabase-py is blocking; offload both round-trips (search + the page snippet
    # fetch) onto one worker thread off the event loop (#107).
    results, has_more = await asyncio.to_thread(
        job_search.search_jobs_with_snippets,
        supabase,
        q=q,
        limit=page_size,
        offset=offset,
        location=location,
        posted_within_days=posted_within_days,
    )
    response = JobSearchResponse(
        query=q, count=len(results), has_more=has_more, results=results
    )
    job_list_cache.set(cache_key, response)
    return response
