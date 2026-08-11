"""Pydantic models for the tailor layer.

The LLM produces a TailoredResume — a structured representation the
`.docx` renderer consumes. Every claim made in this structure must
trace back to something in the OptimizedPayload; source refs are
mandatory for bullets and roles so a post-hoc hallucination check can
verify them.

P5 adds cover letters via the same pipeline. The `document_type`
discriminator decides which payload shape the LLM produces and which
renderer is used downstream. Cover letter tracing is aggregate (list
of referenced outcomes/roles/skills) rather than per-bullet — prose
doesn't cleanly split into traceable units.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.models.ats_lint import LintViolation

DocumentType = Literal["resume", "cover_letter"]

ResumeType = Literal["senior-frontend", "fullstack", "frontend-lead", "generic"]


class ContactInfo(BaseModel):
    """Header fields. Lives outside the experience doc — passed in by
    the caller (profile store or request body; TBD in P3d).
    """

    name: str
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    website: str | None = None
    linkedin: str | None = None


class TailoredBullet(BaseModel):
    """A single achievement line under a role.

    `source_outcome_ref` ties back to an `Outcome.description` or an
    explicit fact the role summary already stated. If the LLM can't
    produce one, the bullet is suspect.
    """

    text: str = Field(min_length=1, max_length=400)
    source_outcome_ref: str | None = None


class TailoredRole(BaseModel):
    company: str
    title: str
    location: str | None = None
    start: str
    end: str | None = None
    bullets: list[TailoredBullet] = Field(default_factory=list)
    source_role_ref: str
    """Must equal a Role.id from the OptimizedPayload."""


class TailoredEducation(BaseModel):
    school: str
    degree: str | None = None
    dates: str | None = None


class TailoredResume(BaseModel):
    """The structured resume the docx renderer consumes."""

    summary: str = Field(min_length=1, max_length=600)
    contact: ContactInfo
    experience: list[TailoredRole]
    skills: list[str]
    education: list[TailoredEducation] = Field(default_factory=list)

    resume_type: ResumeType = "generic"
    jd_snippet: str = Field(default="", max_length=800)
    preferences_applied: list[str] = Field(default_factory=list)


class TailorRequest(BaseModel):
    """Router input shape for POST /tailor/resume."""

    job_description: str = Field(min_length=1, max_length=20_000)
    contact: ContactInfo | None = None
    """Optional override; backend resolves from user_profiles when absent (F3-A)."""
    critique: str | None = Field(default=None, max_length=5_000)
    resume_type: ResumeType | None = None
    page_budget: Literal[1, 2] = 2
    job_posting_id: str | None = None
    """Optional link to a jobs pipeline row (#184)."""
    target_label: str | None = None
    """Target label for annotation resolution (#499)."""
    force_fresh: bool = False
    """Skip resume reuse check and generate from scratch (#504)."""


# ---------------------------------------------------------------------------
# Cover letter shapes
# ---------------------------------------------------------------------------


class ResumeEditRequest(BaseModel):
    """Markdown edit to a draft resume.

    Markdown is the source of truth — the user freely edits text, the
    backend lints and stores it, and the docx gets re-rendered lazily
    on download (hash-cached).
    """

    markdown: str = Field(min_length=1, max_length=50_000)


class ResumeCheckpointRequest(BaseModel):
    """Optional in-flight markdown to flush before checkpointing.

    The frontend uses this for `navigator.sendBeacon` on pagehide so an
    edit that hasn't yet been autosaved still lands in the version
    snapshot. When omitted, the endpoint just snapshots whatever is
    currently in the row.
    """

    markdown: str | None = Field(default=None, max_length=50_000)


class CoverLetterParagraph(BaseModel):
    """One paragraph of cover-letter prose."""

    text: str = Field(min_length=1, max_length=1500)


class TailoredCoverLetter(BaseModel):
    """The structured cover letter the docx renderer consumes.

    Tracing is aggregate: the LLM declares which OptimizedPayload items
    it drew from (outcomes, roles, skills) as lists of refs. Exact
    per-sentence traceability isn't practical for prose — we validate
    structurally that the declared refs exist in the source doc.
    """

    contact: ContactInfo
    recipient_company: str = Field(min_length=1, max_length=200)
    recipient_role: str | None = Field(default=None, max_length=200)
    salutation: str = Field(min_length=1, max_length=200)
    paragraphs: list[CoverLetterParagraph]
    closing: str = Field(min_length=1, max_length=100)
    signature: str = Field(min_length=1, max_length=200)

    jd_snippet: str = Field(default="", max_length=800)
    preferences_applied: list[str] = Field(default_factory=list)

    source_outcome_refs: list[str] = Field(default_factory=list)
    """Outcome.description values from the OptimizedPayload this letter drew from."""
    source_role_refs: list[str] = Field(default_factory=list)
    """Role.id values from the OptimizedPayload this letter drew from."""
    source_skill_refs: list[str] = Field(default_factory=list)
    """Skill.name values from the OptimizedPayload this letter drew from."""


class CoverLetterRequest(BaseModel):
    """Router input shape for POST /tailor/cover-letter."""

    job_description: str = Field(min_length=1, max_length=20_000)
    company_name: str = Field(min_length=1, max_length=200)
    role_title: str | None = Field(default=None, max_length=200)
    contact: ContactInfo | None = None
    """Optional override; backend resolves from user_profiles when absent (F3-A)."""
    critique: str | None = Field(default=None, max_length=5_000)
    job_posting_id: str | None = None
    target_label: str | None = None
    """Target label for annotation resolution (#499)."""


# ---------------------------------------------------------------------------
# Records + responses
# ---------------------------------------------------------------------------


class TailoredResumeRecord(BaseModel):
    """Read shape for a documents row.

    `payload` is stored as JSONB; its shape depends on `document_type`:
    - `"resume"` -> parseable as `TailoredResume`
    - `"cover_letter"` -> parseable as `TailoredCoverLetter`

    Helpers below do the typed parse at the call site.
    """

    id: str
    user_id: str | None
    job_posting_id: str | None
    document_type: DocumentType = "resume"
    resume_type: str
    style_settings: dict[str, Any] | None = None
    """Per-record docx style override ({preset, accent}); None falls back to
    the user's profile default, then to today's unstyled render."""
    jd_snapshot: str
    jd_snapshot_hash: str
    payload: dict[str, Any]
    payload_md: str | None = None
    docx_payload_md_hash: str | None = None
    storage_path: str | None
    warnings: list[str]
    model: str | None
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int
    created_at: datetime
    updated_at: datetime | None = None
    approved_at: datetime | None = None
    source_resume_id: str | None = None
    """Points to the original resume when this was cloned via reuse (#504)."""

    lint_violations: list[LintViolation] | None = None
    """ATS lint state (#656). ``None`` = never linted (every row predating the
    migration); ``[]`` = linted with nothing to report; a list containing any
    ``severity == "error"`` entry = **flagged draft**, persisted despite
    failing lint so the generation spend isn't thrown away. A warnings-only
    list is clean-with-advisories, NOT flagged. Applies to resumes AND cover
    letters alike. Refreshed by ``POST /tailor/resumes/{id}/ats-recheck``,
    which is free (lint is deterministic — no LLM)."""

    model_config = {"extra": "ignore"}

    def as_resume(self) -> TailoredResume:
        if self.document_type != "resume":
            raise ValueError(f"record is document_type={self.document_type!r}, not a resume")
        return TailoredResume.model_validate(self.payload)

    def as_cover_letter(self) -> TailoredCoverLetter:
        if self.document_type != "cover_letter":
            raise ValueError(f"record is document_type={self.document_type!r}, not a cover letter")
        return TailoredCoverLetter.model_validate(self.payload)


class TailorResponse(BaseModel):
    """Router output for POST /tailor/resume and /tailor/cover-letter on success."""

    record: TailoredResumeRecord
    lint_warnings: list[LintViolation] = Field(default_factory=list)


class TailorLintFailureResponse(BaseModel):
    """Router output when the linter finds blocking errors.

    Still the shape of the PATCH/checkpoint 422s (a user edit that fails lint
    is rejected outright — nothing was spent producing it). Post-#656 it is no
    longer the shape of a *generation* lint failure: that persists a flagged
    draft instead, and surfaces the same violations on the record.
    """

    ok: Literal[False] = False
    violations: list[LintViolation]


TailorRunStatus = Literal["running", "error", "idle"]


class TailorStatusResponse(BaseModel):
    """202 kick-off marker for the non-blocking tailor flow (#656).

    * ``running`` — the pipeline is in flight in a detached task. It persists
      regardless of the client, so the caller may navigate away and poll later.
    * ``error`` — a previous run for this key failed; the client should retry.
      (Only ever returned by the poll surface, not the kick-off.)
    """

    status: TailorRunStatus
    message: str | None = None


class TailoredDocumentState(BaseModel):
    """Poll shape for ``GET /tailor/resumes/by-job/{id}`` and its cover-letter
    sibling (#656).

    Supersedes the bare ``TailoredResumeRecord | null`` those routes used to
    return: a client that kicked off a background generation needs to tell
    "nothing here yet, keep polling" from "nothing here, and nothing coming".

    * ``record`` — the persisted document, or ``null`` when none exists yet.
      A record whose ``lint_violations`` is non-empty is a **flagged draft**.
    * ``status`` — ``running`` while a detached generation is in flight for
      this (user, type, posting); ``error`` when the last one failed;
      ``idle`` otherwise. ``idle`` with a ``null`` record is the "generate"
      empty state; ``idle`` with a record is a settled document.
    * ``message`` — the failure text when ``status == "error"``.

    Violations deliberately live on the record rather than being mirrored at
    this level: the ``documents.lint_violations`` column is the single source
    of truth, and a copy here would go stale against an ats-recheck.
    """

    record: TailoredResumeRecord | None = None
    status: TailorRunStatus = "idle"
    message: str | None = None


class AtsRecheckResponse(BaseModel):
    """Router output for ``POST /tailor/resumes/{id}/ats-recheck``.

    ``ok`` mirrors ``LintResult.ok`` — true when nothing blocking remains, at
    which point ``violations`` holds only warnings (or is empty).
    """

    ok: bool
    violations: list[LintViolation]
    record: TailoredResumeRecord


class GapGateFailureResponse(BaseModel):
    """Router output when the master doc is structurally insufficient to generate."""

    ok: Literal[False] = False
    code: Literal["gap_gate"] = "gap_gate"
    reason: str
    message: str
    gap_pct: float
    tier: str


class BulkExportRequest(BaseModel):
    """Input for POST /tailor/resumes/export-zip."""

    resume_ids: list[str] = Field(min_length=1, max_length=20)
