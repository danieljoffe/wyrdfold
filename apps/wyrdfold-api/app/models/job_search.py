"""Pydantic models for the public job-search surface (#467).

The public search is **un-personalized and model-free**: a keyword/title search
over the shared, live jobs corpus with no per-user scoring, no LLM, no
embeddings. Results deliberately carry NO match score (search must never be
mistaken for the AI-matched ranking) and NO job-description body — the public
row is a preview that links out to the source posting; the full JD and the
"how you match" analysis stay behind login (#467 exposure decision).
"""

from datetime import datetime

from pydantic import BaseModel


class JobSearchResult(BaseModel):
    """One public search result row — preview + link to the source posting."""

    id: str
    title: str
    company_name: str
    location: str | None = None
    # Structured parts parsed from ``location`` at ingest (#518). Nullable —
    # unparseable strings leave them None and the client falls back to the
    # raw ``location`` for display.
    city: str | None = None
    state: str | None = None
    country: str | None = None
    location_remote: bool | None = None
    salary_text: str | None = None
    # Structured salary parsed from ``salary_text`` at ingest — query-grade
    # (range filters); the raw text stays the display value. ``salary_period``
    # is "yearly" | "hourly" | None (unknown periods are never guessed).
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    salary_period: str | None = None
    # Link to the ORIGINAL posting (Greenhouse/Ashby/…). Public results point at
    # the source rather than republishing the full JD.
    absolute_url: str | None = None
    source_posted_at: datetime | None = None
    cataloged_at: datetime | None = None
    # A short PLAINTEXT preview (NOT the full JD body) — populated only for the
    # PUBLIC surface, where a snippet helps a logged-out visitor judge the role.
    # Left ``None`` on the authed surface, which spares its hot path the extra
    # ``description_html`` read. Tag-stripped + truncated server-side.
    snippet: str | None = None


class JobSearchResponse(BaseModel):
    """Envelope for one page of search results.

    ``count`` is the size of THIS page (not the total corpus match count, which
    a title-ranked search doesn't compute); ``has_more`` drives the client's
    "Load more" affordance.
    """

    query: str
    count: int
    has_more: bool
    results: list[JobSearchResult]
