"""Pydantic models for the job analysis feature (#501).

The LLM grades the user's OptimizedPayload against a job description,
producing a structured scorecard and one-line recommendation. Results
are cached in the `analyses` table.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class SkillMatch(BaseModel):
    name: str
    matched: bool
    confidence: Literal["high", "medium", "low"]
    evidence: str | None = None


class Scorecard(BaseModel):
    skills_matched: list[SkillMatch]
    skills_missing: list[str]
    nice_to_haves: list[str]
    seniority_fit: Literal["strong", "moderate", "weak"]
    seniority_rationale: str
    domain_fit: Literal["strong", "moderate", "weak"]
    domain_rationale: str


class JobAnalysis(BaseModel):
    """LLM output shape: scorecard + recommendation."""

    scorecard: Scorecard
    recommendation: str


class JobAnalysisRecord(BaseModel):
    """DB read shape for analyses rows."""

    id: str
    job_posting_id: str
    target_id: str
    user_id: str | None
    optimized_doc_id: str | None
    scorecard: Scorecard
    recommendation: str
    model: str
    cost_usd: float
    latency_ms: int
    created_at: datetime


class AnalysisStatusResponse(BaseModel):
    """Poll marker for the non-blocking analysis flow (#459).

    Returned (with an appropriate HTTP status) when there is no finished
    record to hand back yet:

    * ``running`` — the LLM analysis is in flight (``POST`` returns this with
      ``202``; ``GET`` returns it with ``200`` while polling). The work runs
      in a detached task and persists regardless of the client, so the caller
      is free to navigate away and come back.
    * ``error`` — the background run failed; the client should offer a retry.
    * ``idle`` — no cached result and nothing in flight (e.g. a restart
      dropped the run). The client re-kicks via ``POST``. Only ``GET``
      returns this.
    """

    status: Literal["running", "error", "idle"]
    message: str | None = None
