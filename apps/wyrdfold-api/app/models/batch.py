"""Pydantic models for batch resume generation (#503)."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models.tailor import ContactInfo, ResumeType


class BatchItem(BaseModel):
    """Per-job status within a batch."""

    job_posting_id: str
    status: Literal["pending", "completed", "failed"] = "pending"
    resume_record_id: str | None = None
    reused_from: str | None = None
    """Source resume ID when this item reused an existing resume (#504)."""
    error: str | None = None
    lint_violations: list[str] | None = None
    """ATS lint messages when the generated resume is a flagged draft (#656).

    The item is still ``completed`` — a real ``documents`` row exists at
    ``resume_record_id`` and the posting advanced to ``resume_draft``. Before
    #656 a lint failure produced nothing and the item was marked ``failed``;
    marking it that way now would hide a draft the user already paid for and
    can fix in the editor.
    """


class BatchJob(BaseModel):
    """DB read shape for batch_runs rows."""

    id: str
    user_id: str | None
    status: Literal["pending", "processing", "completed", "failed"]
    total: int
    completed: int
    failed: int
    items: list[BatchItem]
    created_at: datetime
    updated_at: datetime


class BatchRequest(BaseModel):
    """Router input for POST /tailor/batch."""

    # Capped at 10 (was 20): each id is a Sonnet tailor call, so the max
    # batch is the single most expensive user action in the product.
    job_posting_ids: list[str] = Field(min_length=1, max_length=10)
    contact: ContactInfo | None = None
    """Optional override; backend resolves from user_profiles when absent (F3-A)."""
    resume_type: ResumeType | None = None
    page_budget: Literal[1, 2] = 2
    force_fresh: bool = False
    """Skip resume reuse and generate every resume from scratch (#504)."""


class BatchResponse(BaseModel):
    """Immediate response when a batch is created."""

    batch_id: str
    total: int
    status: str
    warnings: list[str] = Field(default_factory=list)
