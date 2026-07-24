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
    department: str | None = None
    salary_text: str | None = None
    # Link to the ORIGINAL posting (Greenhouse/Ashby/…). Public results point at
    # the source rather than republishing the full JD.
    absolute_url: str | None = None
    first_seen_at: datetime | None = None
    created_at: datetime | None = None


class JobSearchResponse(BaseModel):
    """Envelope for a public search — a single capped page (no deep pagination,
    to bound corpus enumeration)."""

    query: str
    count: int
    results: list[JobSearchResult]
