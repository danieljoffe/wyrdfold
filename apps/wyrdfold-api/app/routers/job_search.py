"""Public job-search router (#467).

The one PUBLIC (unauthenticated) READ over the shared jobs corpus: keyword/title
search, model-free, no per-user scoring. Security posture mirrors ``waitlist``:

  - Reads via the **service-role** client — the corpus is shared, non-user data
    (``jobs`` is SELECT-true under RLS anyway); no user context is needed or used.
  - **BFF-only** (``require_bff_secret``) so the per-IP rate limit below can't be
    bypassed by a direct hit with a forged ``X-Forwarded-For`` (SEC-5).
  - **Per-IP rate-limited** (slowapi keys on client IP when there's no JWT).
  - **Cached** — the query is fully anonymous, so responses are shareable across
    callers with no tenant key.
  - **Single capped page** (no deep pagination) to bound corpus enumeration.
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

router = APIRouter(tags=["job-search"])

_CACHE_PREFIX = "jobsearch:"


@router.get(
    "/search",
    response_model=JobSearchResponse,
    # BFF-only: the per-IP limit is only spoof-proof if the route can't be hit
    # directly with a forged forwarded-for (SEC-5, mirrors waitlist).
    dependencies=[Depends(require_bff_secret)],
)
@limiter.limit("30/minute;300/hour")
async def search_jobs_public(
    request: Request,
    q: str = Query(..., min_length=1, max_length=120, description="Title / keyword query"),
    page_size: int = Query(job_search.DEFAULT_PAGE_SIZE, ge=1, le=job_search.MAX_PAGE_SIZE),
    supabase: Client = Depends(get_supabase),
) -> JobSearchResponse:
    """Public keyword search over the live, US jobs corpus.

    ``request`` is required by slowapi to key the per-IP limit (no JWT on this
    public route). Results carry NO match score and NO JD body — a preview that
    links to the source posting; the full JD + "how you match" stay behind login.
    """
    cache_key = make_cache_key(_CACHE_PREFIX, q=q.strip().lower(), page_size=page_size)
    cached = job_list_cache.get(cache_key)
    if cached is not None:
        return cast(JobSearchResponse, cached)

    # supabase-py is blocking; offload the round-trip off the event loop.
    results = await asyncio.to_thread(job_search.search_jobs, supabase, q=q, limit=page_size)
    response = JobSearchResponse(query=q, count=len(results), results=results)
    job_list_cache.set(cache_key, response)
    return response
