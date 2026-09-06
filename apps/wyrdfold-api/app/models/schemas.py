from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

BOARD_TOKEN_PATTERN = r"^[a-zA-Z0-9][a-zA-Z0-9_.:/|@-]{1,250}$"


class ScoreBreakdown(BaseModel):
    role_titles: float = 0
    technologies: float = 0
    domain_skills: float = 0
    seniority_signals: float = 0
    negative: float = 0


class ScoreResult(BaseModel):
    score: int
    breakdown: ScoreBreakdown
    matched_keywords: list[str]
    excluded: bool
    # The negative keywords that fired on the TITLE during THIS scoring pass.
    #
    # NARROW BY DESIGN. This records one specific cause; it is NOT a summary of
    # why a row is excluded, because ``excluded`` has three writers today:
    # this scoring path, the Phase-2 empty-JD drop in
    # ``services/fit/score_persistence.py``, and the Phase-1 backfill in
    # ``services/relevance/phase1_backfill.py`` (which ORs in ``not
    # promising``). Only the first records keywords; the other two leave the
    # field untouched, so a recorded finding survives them intact. So
    # an empty list means "no title keyword fired here" and nothing more — it is
    # not evidence of any particular alternative cause. A reader explaining a
    # skip must combine this with ``excluded`` / ``promising`` /
    # ``logistics_filters``. (Semantics tightened in review of #1018, which
    # caught the earlier comment overclaiming this as the sole writer.)
    #
    # "This pass" is enforceable, not just asserted: the row also carries
    # ``exclusion_keywords_version``, stamped with the same
    # ``scored_profile_version`` the pass writes. Other writers advance that
    # version without touching the array, so the row could otherwise read as
    # current while carrying a keyword the newer profile no longer treats as a
    # negative (review of #1018).
    #
    # READER RULE — the keywords describe the CURRENT profile iff::
    #
    #     exclusion_keywords IS NOT NULL
    #     AND exclusion_keywords_version IS NOT NULL
    #     AND exclusion_keywords_version == scored_profile_version
    #
    # RECORDEDNESS IS PART OF CURRENTNESS. Do not shorten this to a
    # NULL-tolerant equality (SQL's ``IS NOT DISTINCT FROM``): that treats
    # NULL/NULL as equal, so a legacy row — both NULL precisely because nothing
    # was recorded — would read as "current". Version equality alone cannot tell
    # "the fact is current" from "we have no fact".
    #
    # Kept because the reason is not reconstructible after the fact: a target's
    # negative list moves with its profile version, so a row scored under an
    # older profile can no longer be explained by replaying today's keywords.
    # ``_title_matches_any_target`` already admits these postings specifically
    # "so the scoring pipeline records the rejection for audit" — this is the
    # half of that intent that was never stored.
    exclusion_keywords: list[str] = Field(default_factory=list)


Provider = Literal[
    "greenhouse", "lever", "ashby", "workday", "smartrecruiters", "jsonld", "crawl", "manual"
]


class JobPosting(BaseModel):
    id: str
    external_id: str
    source_id: str
    title: str
    company_name: str
    location: str | None
    absolute_url: str | None
    score: int
    score_breakdown: ScoreBreakdown | None
    status: str
    target_id: str | None = None
    source_posted_at: datetime | None = None
    cataloged_at: datetime


class JobSource(BaseModel):
    id: str
    board_token: str
    company_name: str
    provider: Provider = "greenhouse"
    enabled: bool
    last_polled_at: datetime | None
    job_count: int


class PollResult(BaseModel):
    sources_polled: int
    new_jobs: int
    updated_jobs: int
    archived_jobs: int = 0
    errors: list[str]


class StatusUpdate(BaseModel):
    status: Literal[
        "new",
        "saved",
        "resume_draft",
        "resume_ready",
        "applied",
        "interviewing",
        "offer",
        "rejected",
        "archived",
    ]
    note: str | None = Field(default=None, max_length=1000)


class SourceAction(BaseModel):
    action: Literal["add", "remove", "toggle"]
    board_token: str = Field(pattern=BOARD_TOKEN_PATTERN, max_length=250)
    company_name: str | None = Field(default=None, max_length=200)
    provider: Provider = "greenhouse"


class PaginatedResponse(BaseModel):
    postings: list[JobPosting]
    total: int
    page: int
    page_size: int


class UrlValidateRequest(BaseModel):
    url: str = Field(max_length=2048)


class UrlValidateResponse(BaseModel):
    is_valid: bool
    final_url: str
    warnings: list[str]
    rejection_reason: str | None


class RemoveJobRequest(BaseModel):
    """Which target to remove a posting from.

    ``None`` means "every target of mine that currently holds it" — the
    All Jobs tab has no single target in scope. The id is validated against
    the caller's own ``user_targets`` before use; it is never trusted as a
    filter on its own.
    """

    target_id: str | None = Field(default=None, max_length=64)


class ManualJobRequest(BaseModel):
    url: str = Field(max_length=2048)
    title: str | None = Field(default=None, max_length=500)
    company_name: str | None = Field(default=None, max_length=200)
    location: str | None = Field(default=None, max_length=200)


class ManualJobResponse(BaseModel):
    success: bool
    posting_id: str | None = None
    extracted: dict[str, str | None]
    extraction_tier: str
    warnings: list[str]
    needs_manual_fields: bool
    # The extracted JD body, returned so callers that need it next (the
    # onboarding path-A tailor kick) don't have to re-fetch the posting.
    # ``GET /jobs/{id}`` can't serve them: its ownership probe requires a
    # ``scores`` row, and a brand-new user has no active targets at add
    # time, so nothing has scored the posting yet.
    description_html: str | None = None


class AddToTargetRequest(BaseModel):
    """Body for ``POST /jobs/{job_id}/add-to-target`` (#467 power-action).

    The job is an EXISTING posting (a search result's ``jobs.id``), so unlike
    ``ManualJobRequest`` there is no URL to fetch/materialize — only the target
    to score it against."""

    target_id: str = Field(max_length=64)


class AddToTargetResponse(BaseModel):
    job_posting_id: str
    target_id: str
    score: int


ScoringStatus = Literal["stage1", "stage2", "complete"]


class JobTargetScore(BaseModel):
    """DB read shape for scores rows."""

    id: str
    job_posting_id: str
    target_id: str
    score: int
    score_breakdown: ScoreBreakdown | None
    matched_keywords: list[str]
    excluded: bool
    scoring_status: ScoringStatus = "stage1"
    scored_profile_version: int = 1
    created_at: datetime
    updated_at: datetime
