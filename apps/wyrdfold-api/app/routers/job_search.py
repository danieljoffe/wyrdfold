"""Job-search router (#467).

Keyword/title search over the shared jobs corpus: model-free, no per-user
scoring, no LLM/embeddings.

**Authenticated (logged-in users + operators) for now.** The feature ships to
the beta cohort first, so the endpoint is gated to real sessions — no
public/unauthenticated surface, hence no anonymous corpus enumeration — while
the UX and abuse controls are proven. A PUBLIC (unauth) variant is a later
additive step, taken alongside opening public signups (#467 prerequisite); the
service layer is un-personalized already, so exposing it publicly later is a
gating change, not a rewrite.

  - Reads via the **service-role** client — the corpus is shared, non-user data.
  - **Per-user rate-limited** (slowapi keys on the JWT ``sub`` for authed callers).
  - **Cached** — the query is fully anonymous (no per-user data), so responses
    are shareable across callers with no tenant key.
  - **Single capped page** (no deep pagination) to bound corpus enumeration.
"""

from __future__ import annotations

import asyncio
import logging
from typing import cast

from fastapi import APIRouter, Depends, Query, Request
from supabase import Client

from app.cache import job_list_cache, make_cache_key
from app.dependencies import get_supabase, verify_api_key_or_jwt
from app.models.job_search import JobSearchResponse
from app.rate_limit import limiter
from app.services import job_search

logger = logging.getLogger(__name__)

router = APIRouter(tags=["job-search"], dependencies=[Depends(verify_api_key_or_jwt)])

_CACHE_PREFIX = "jobsearch:"


@router.get("/search", response_model=JobSearchResponse)
@limiter.limit("30/minute;300/hour")
async def search_jobs_endpoint(
    request: Request,
    q: str = Query(..., min_length=1, max_length=120, description="Title / keyword query"),
    page_size: int = Query(
        job_search.DEFAULT_PAGE_SIZE, ge=1, le=job_search.MAX_PAGE_SIZE
    ),
    offset: int = Query(0, ge=0, le=job_search.MAX_OFFSET, description="Pagination offset"),
    supabase: Client = Depends(get_supabase),
) -> JobSearchResponse:
    """Keyword search over the live, US jobs corpus (one page).

    ``request`` is required by slowapi to key the rate limit (per-user for an
    authed caller). ``offset`` pages through the ranked candidate window (see
    the service). Results carry NO match score and NO JD body — a preview that
    links to the source posting; the full JD + "how you match" stay in the
    matched experience.
    """
    cache_key = make_cache_key(
        _CACHE_PREFIX, q=q.strip().lower(), page_size=page_size, offset=offset
    )
    cached = job_list_cache.get(cache_key)
    if cached is not None:
        return cast(JobSearchResponse, cached)

    # supabase-py is blocking; offload the round-trip off the event loop.
    results, has_more = await asyncio.to_thread(
        job_search.search_jobs, supabase, q=q, limit=page_size, offset=offset
    )
    response = JobSearchResponse(
        query=q, count=len(results), has_more=has_more, results=results
    )
    job_list_cache.set(cache_key, response)
    return response
