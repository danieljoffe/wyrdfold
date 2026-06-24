"""Pydantic models for job targets (#495).

ScoringProfile is the target-based scoring schema. Each category has named
keywords with individual integer weights plus a float category multiplier.
This replaces the old TieredKeywords/KeywordConfig for target-aware scoring
while keeping the original intact for backward compatibility.
"""

from datetime import datetime
from typing import Literal, get_args

from pydantic import BaseModel, Field, field_validator, model_validator

# ---- Scoring Profile schema ------------------------------------------------


class CategoryProfile(BaseModel):
    """One scoring category (e.g., core_skills, secondary_skills)."""

    keywords: dict[str, int] = Field(default_factory=dict)  # keyword -> weight 1-3
    weight: float = 1.0  # category multiplier


class SeniorityProfile(BaseModel):
    level: str | None = None  # e.g. "senior", "staff"
    signals: list[str] = Field(default_factory=list)


class DomainProfile(BaseModel):
    signals: list[str] = Field(default_factory=list)
    weight: float = 0.5


class NegativeProfile(BaseModel):
    keywords: list[str] = Field(default_factory=list)
    weight: float = -10.0


class ScoringProfile(BaseModel):
    """Per-target scoring profile. Stored as JSONB in targets.scoring_profile."""

    categories: dict[str, CategoryProfile] = Field(default_factory=dict)
    seniority: SeniorityProfile = Field(default_factory=SeniorityProfile)
    domain: DomainProfile = Field(default_factory=DomainProfile)
    negative: NegativeProfile = Field(default_factory=NegativeProfile)


# ---- Row models (DB read shapes) -------------------------------------------


SeniorityHint = Literal["ic", "senior", "staff", "manager", "director", "vp", "c_level"]


class JobTarget(BaseModel):
    id: str
    label: str
    description: str | None = None
    normalized_label: str | None = None
    scoring_profile: ScoringProfile
    search_keywords: list[str] = Field(
        default_factory=list,
        description=(
            "ATS query keywords (Greenhouse q=, etc.). Distinct from "
            "scoring_profile.categories.*.keywords, which weight JD text "
            "during scoring — these drive which jobs get fetched in the "
            "first place."
        ),
    )
    activation_status: str = Field(
        default="idle",
        description=(
            "Background pipeline state: idle | deriving | polling | ready "
            "| error. Distinct from is_active, the user-facing toggle for "
            "whether jobs should be queried for this target."
        ),
    )
    profile_version: int = 1
    is_active: bool
    # Few-shot title pools for the upcoming Phase 1 LLM triage. Seeded
    # at target creation from the same LLM call that derives the
    # scoring profile; later (Phase 1 PR) augmented from user 👍/👎
    # feedback once enough labels accumulate per target.
    example_promising_titles: list[str] = Field(default_factory=list)
    example_unpromising_titles: list[str] = Field(default_factory=list)
    # Slim shape (PR A of plan-wyrdfold-streamlined-target.md). NULL/empty
    # on legacy rows until PR B's backfill; new targets populate at
    # creation alongside the legacy scoring_profile.
    seniority_hint: SeniorityHint | None = None
    domain_hints: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class AxisWeights(BaseModel):
    """User-tunable per-axis multiplier for Phase 2's four-axis scorecard.

    Each axis weight is in [0, 1]. Defaults are equal quartile (0.25
    each) so the display_score reproduces Sonnet's holistic ``score`` —
    setting weights to defaults is behaviorally identical to not setting
    them. NULL in the DB column means "use defaults"; the router
    short-circuits the math when weights are unset.

    See plan-wyrdfold-streamlined-target.md "User-tunable axis weights".
    """

    title_fit: float = Field(default=0.25, ge=0.0, le=1.0)
    skills_fit: float = Field(default=0.25, ge=0.0, le=1.0)
    seniority_fit: float = Field(default=0.25, ge=0.0, le=1.0)
    domain_fit: float = Field(default=0.25, ge=0.0, le=1.0)

    def is_default(self) -> bool:
        """True iff every axis is exactly the default quartile.

        The router can skip the per-row math when this is True — no
        change vs the raw ``score`` value.
        """
        return (
            self.title_fit == 0.25
            and self.skills_fit == 0.25
            and self.seniority_fit == 0.25
            and self.domain_fit == 0.25
        )


class UserTarget(BaseModel):
    """Junction row linking a user to a shared target."""

    id: str
    user_id: str
    target_id: str
    is_active: bool
    fit_score: int | None = None
    fit_score_reasoning: str | None = None
    # PR E (plan-wyrdfold-streamlined-target.md). NULL = use defaults
    # (equal quartile); router skips per-row math. axis_weights_previous
    # holds the one-step-back snapshot for the undo button.
    axis_weights: AxisWeights | None = None
    axis_weights_previous: AxisWeights | None = None
    # Per-target notification thresholds (#15, columns from #178). NULL =
    # fall back to the user-profile default for that channel (notify.py).
    job_score_threshold: int | None = None
    sms_score_threshold: int | None = None
    # Per-user target preferences (#60). A read-time filter/re-rank over the
    # SHARED, cached fit score — never a per-user re-grade. See
    # ``TargetPreferences`` for field semantics + defaults. Carried on the
    # junction row so a single user_targets read hydrates them alongside the
    # other per-user knobs.
    pref_score_cutoff: int = 40
    pref_locations: list[str] | None = None
    pref_remote_ok: bool = True
    pref_seniority_min: str | None = None
    pref_seniority_max: str | None = None
    pref_employment_types: list[str] | None = None
    pref_include_unknown_salary: bool = True
    created_at: datetime
    updated_at: datetime


class NotificationThresholdsUpdate(BaseModel):
    """Per-target email/SMS score thresholds (#15).

    Each is the minimum score a new match must reach to alert on that
    channel. The PATCH is a partial update: an *omitted* field leaves that
    channel untouched, while an explicit ``null`` resets it to the
    user-profile default (``user_profiles.{job,sms}_score_threshold``). The
    UI can send one channel or both.
    """

    job_score_threshold: int | None = Field(default=None, ge=0, le=200)
    sms_score_threshold: int | None = Field(default=None, ge=0, le=200)


# Closed vocabulary for the seniority range. Mirrors ``SeniorityHint`` (the
# job-side firewall tag the read path filters against) plus a sentinel so the
# preference set and the job tag compare on the same ladder. Ordered low→high;
# the read path uses the index to do range comparisons.
SeniorityLevel = Literal["ic", "senior", "staff", "manager", "director", "vp", "c_level"]

SENIORITY_ORDER: tuple[SeniorityLevel, ...] = get_args(SeniorityLevel)


class TargetPreferences(BaseModel):
    """Per-user, per-target read-time preferences (#60).

    These shape the *calling user's* view of a SHARED target's jobs list. They
    are applied as a filter/re-rank over the shared, cached fit score at read
    time — they NEVER trigger a re-grade or any per-user scoring. Every field
    has a behaviorally-neutral default so an un-customized link sees the same
    list it always did.

    Field semantics:

    * ``pref_score_cutoff`` — hide jobs whose fit score is below this. Always
      enforceable (``scores.score`` always exists). Default 40.
    * ``pref_locations`` — keep jobs whose location matches any of these terms
      (matched against the job's ``metro`` firewall tag when present, else a
      free-text ILIKE on ``location``). ``None``/empty = no location filter.
    * ``pref_remote_ok`` — when True, remote roles pass the location filter even
      if they don't match ``pref_locations``. Default True.
    * ``pref_seniority_min`` / ``pref_seniority_max`` — keep jobs whose
      ``seniority`` firewall tag falls within this (inclusive) range on the
      ``SENIORITY_ORDER`` ladder. ``None`` = open-ended on that end.
    * ``pref_employment_types`` — keep jobs whose ``employment_type`` firewall
      tag is in this set. ``None``/empty = no employment-type filter.
    * ``pref_include_unknown_salary`` — out of scope for v1 filtering (salary
      filtering isn't implemented yet); stored so the UI can round-trip the
      toggle. Default True.

    The seniority / employment-type / metro / remote job-side tag columns are
    added by a separate firewall PR and are NOT backfilled. The read path
    feature-detects them and treats a missing/NULL job tag as "unknown → keep"
    (lenient), so these preferences are inert until the firewall lands.
    """

    pref_score_cutoff: int = Field(default=40, ge=0, le=200)
    pref_locations: list[str] | None = None
    pref_remote_ok: bool = True
    pref_seniority_min: SeniorityLevel | None = None
    pref_seniority_max: SeniorityLevel | None = None
    pref_employment_types: list[str] | None = None
    pref_include_unknown_salary: bool = True

    @model_validator(mode="after")
    def _seniority_range_ordered(self) -> "TargetPreferences":
        """Reject an inverted range (min ranks above max) — it would silently
        match nothing, which reads as a bug to the user. Open-ended ends
        (``None``) are always fine."""
        if self.pref_seniority_min is not None and self.pref_seniority_max is not None:
            lo = SENIORITY_ORDER.index(self.pref_seniority_min)
            hi = SENIORITY_ORDER.index(self.pref_seniority_max)
            if lo > hi:
                raise ValueError(
                    "pref_seniority_min must not rank above pref_seniority_max"
                )
        return self


class TargetPreferencesUpdate(BaseModel):
    """PUT body for the per-user target preferences (#60).

    A full replace (PUT semantics): every omitted scalar falls back to its
    documented default, and omitted array fields clear to ``None`` (no filter).
    This keeps the stored row a complete, self-describing preference set rather
    than an accreting partial. The validation mirrors ``TargetPreferences``.
    """

    pref_score_cutoff: int = Field(default=40, ge=0, le=200)
    pref_locations: list[str] | None = Field(default=None, max_length=50)
    pref_remote_ok: bool = True
    pref_seniority_min: SeniorityLevel | None = None
    pref_seniority_max: SeniorityLevel | None = None
    pref_employment_types: list[str] | None = Field(default=None, max_length=20)
    pref_include_unknown_salary: bool = True

    @model_validator(mode="after")
    def _seniority_range_ordered(self) -> "TargetPreferencesUpdate":
        if self.pref_seniority_min is not None and self.pref_seniority_max is not None:
            lo = SENIORITY_ORDER.index(self.pref_seniority_min)
            hi = SENIORITY_ORDER.index(self.pref_seniority_max)
            if lo > hi:
                raise ValueError(
                    "pref_seniority_min must not rank above pref_seniority_max"
                )
        return self


class TargetReferenceJD(BaseModel):
    id: str
    target_id: str
    # The user who contributed this JD. NULL for legacy rows and
    # operator/system-seeded JDs, which the merge treats as one shared
    # "system" contributor (#5 refinement layer / de-bias).
    user_id: str | None = None
    jd_url: str | None = None
    jd_text: str
    extracted_profile: ScoringProfile
    # Down-voted past the quorum → excluded from the shared-profile merge
    # (#5 P3). The only surfaced signal of the otherwise-anonymous votes.
    suppressed: bool = False
    created_at: datetime


# ---- Response shapes ---------------------------------------------------------


class UserTargetWithTarget(BaseModel):
    """A user's link to a target, paired with the full target data."""

    user_target: UserTarget
    target: JobTarget


class CreateOrLinkResult(BaseModel):
    """Outcome of a from-input flow.

    ``was_matched`` indicates whether the LLM-normalized input collided with
    an existing shared target — useful for the frontend to vary the toast.
    """

    user_target: UserTarget
    target: JobTarget
    was_matched: bool


class TargetsListResponse(BaseModel):
    """Response shape for endpoints returning a list of shared JobTargets."""

    targets: list[JobTarget]


class MyTargetsListResponse(BaseModel):
    """Response shape for the per-user targets list (with link metadata)."""

    targets: list[UserTargetWithTarget]


# ---- List-DTO (summary) shapes (#863) --------------------------------------
# Light projections for the targets list views. They omit the heavy JSONB
# fields (scoring_profile, search_keywords, example_*_titles, domain_hints)
# and instead surface the two counts the list UI needs. The full target is
# still served by GET /targets/{id} for the detail view.


class JobTargetSummary(BaseModel):
    """List-view projection of JobTarget. ``keyword_count`` and
    ``category_count`` are derived server-side from ``scoring_profile`` so
    the list UI never receives the JSONB itself."""

    id: str
    label: str
    description: str | None = None
    normalized_label: str | None = None
    activation_status: str = "idle"
    profile_version: int = 1
    is_active: bool
    seniority_hint: SeniorityHint | None = None
    keyword_count: int = 0
    category_count: int = 0
    created_at: datetime
    updated_at: datetime


class UserTargetWithSummary(BaseModel):
    """A user's link to a target, paired with the summary projection."""

    user_target: UserTarget
    target: JobTargetSummary


class TargetsSummaryListResponse(BaseModel):
    """Response shape for the shared-targets list (summary projection)."""

    targets: list[JobTargetSummary]


class MyTargetsSummaryListResponse(BaseModel):
    """Response shape for the per-user targets list (summary projection)."""

    targets: list[UserTargetWithSummary]


class TargetStatusResponse(BaseModel):
    """Activation status snapshot for a target — used by the activation pipeline."""

    activation_status: str
    jobs_count: int


class ReferenceJDsListResponse(BaseModel):
    """Response shape for the reference-JDs list endpoint."""

    reference_jds: list[TargetReferenceJD]


class DeleteResponse(BaseModel):
    """Generic 200-with-body delete confirmation. Frontend reads ``deleted``."""

    deleted: bool


# ---- Request shapes (router inputs) ----------------------------------------


class TargetCreate(BaseModel):
    label: str = Field(min_length=1, max_length=200)
    description: str | None = None
    scoring_profile: ScoringProfile = Field(default_factory=ScoringProfile)
    search_keywords: list[str] = Field(default_factory=list)


class TargetUpdate(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    scoring_profile: ScoringProfile | None = None
    search_keywords: list[str] | None = None
    activation_status: str | None = None
    is_active: bool | None = None
    profile_version: int | None = None
    example_promising_titles: list[str] | None = None
    example_unpromising_titles: list[str] | None = None
    # Slim shape additions (PR A of plan-wyrdfold-streamlined-target.md).
    # Update partials: None leaves the column unchanged on the DB side.
    seniority_hint: SeniorityHint | None = None
    domain_hints: list[str] | None = None


class TargetFromManual(BaseModel):
    """Create a target from user-typed title + description.

    The LLM normalizes the input into a standardized ``TargetSuggestion``
    shape before matching against existing targets, so created and suggested
    targets share the same canonical format.
    """

    label: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class TargetFromUrl(BaseModel):
    """Create a target from a JD URL.

    The label is optional — when omitted, the job title extracted from the
    page is used. Falls back to "Untitled Target" if neither is available.
    """

    jd_url: str = Field(max_length=2048)
    label: str | None = Field(default=None, max_length=200)


class ReferenceJDAdd(BaseModel):
    """Add a reference JD to a target. Triggers profile derivation + merge.

    Either `jd_text` (>=50 chars) or `jd_url` must be provided. When only
    `jd_url` is given, the server fetches the page and extracts JD text via
    the same pipeline used by `POST /jobs/manual`.
    """

    jd_text: str | None = Field(default=None, max_length=100_000)
    jd_url: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def _require_text_or_url(self) -> "ReferenceJDAdd":
        if not self.jd_text and not self.jd_url:
            raise ValueError("Either jd_text or jd_url is required")
        if self.jd_text is not None and len(self.jd_text) < 50:
            raise ValueError("jd_text must be at least 50 characters")
        return self


class ReferenceJDVote(BaseModel):
    """Cast a vote on a reference-JD contribution (#5 P3).

    ``-1`` down-votes, ``+1`` up-votes, ``0`` clears the caller's vote. Votes
    are anonymous; the response surfaces only the caller's own vote and the
    contribution's suppression outcome.
    """

    value: int = Field(ge=-1, le=1)


class ContributionVoteResult(BaseModel):
    """Outcome of a vote: the caller's recorded vote + the shared suppression
    state. Deliberately omits the vote tally + voter identities (anonymous)."""

    reference_jd_id: str
    your_vote: int
    suppressed: bool
    # Set when the vote flipped suppression and the profile was re-merged.
    profile_version: int | None = None


# ---- Suggestion shapes (LLM output) ----------------------------------------


# Verbose leadership roles overshoot the prompt's 80-600 char target; the
# field is truncated (not rejected) so a long paragraph never discards the
# whole derivation. The DB column is unbounded TEXT. #27.
_DERIVED_DESCRIPTION_MAX = 800


class DerivedTarget(BaseModel):
    """LLM output: scoring profile + search keywords + few-shot title
    pools + slim shape fields derived from a target.

    The example_*_titles lists seed the Phase 1 binary triage prompt:
    promising = positive few-shot anchors, unpromising = negative
    anchors. Both default to empty so legacy LLM outputs that pre-date
    the prompt extension still validate cleanly (the Phase 1 grader
    treats empty lists as "no examples available" and degrades to
    label-only grading).

    The ``description`` / ``seniority_hint`` / ``domain_hints`` triple is
    the slim target shape (PR A of plan-wyrdfold-streamlined-target.md).
    They default to None / empty so legacy LLM outputs that don't include
    them still validate; new derivations populate them.
    """

    scoring_profile: ScoringProfile
    search_keywords: list[str] = Field(default_factory=list)
    example_promising_titles: list[str] = Field(default_factory=list)
    example_unpromising_titles: list[str] = Field(default_factory=list)
    # Slim shape — optional in the model so legacy LLM outputs that
    # don't include these fields still validate. The prompt asks for
    # them; reality may serve old-prompt cached responses for a while.
    description: str | None = Field(default=None, max_length=_DERIVED_DESCRIPTION_MAX)
    seniority_hint: SeniorityHint | None = None
    domain_hints: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("seniority_hint", mode="before")
    @classmethod
    def _coerce_seniority_hint(cls, value: object) -> object:
        """Safety net (#27): never let one out-of-vocabulary seniority value
        reject the whole derived profile.

        The prompt instructs the model to map title nomenclature onto the
        closed ``SeniorityHint`` set, but this is a probabilistic generator
        against a hard ``Literal`` — a stray value like "principal" or "lead"
        would otherwise raise and discard the entire derivation. Normalize
        case/whitespace and drop anything outside the set to None ("no hint"),
        so Phase 2 degrades to label-only seniority grading instead of failing.
        """
        if isinstance(value, str):
            normalized = value.strip().lower()
            return normalized if normalized in get_args(SeniorityHint) else None
        return value

    @field_validator("description", mode="before")
    @classmethod
    def _truncate_description(cls, value: object) -> object:
        """Truncate an over-long description instead of rejecting the whole
        derivation (#27). The prompt asks for 80-600 chars, but verbose
        leadership roles overshoot the cap; trim at a word boundary. Runs
        before the ``max_length`` constraint so the trimmed value passes.
        """
        if isinstance(value, str) and len(value) > _DERIVED_DESCRIPTION_MAX:
            truncated = value[: _DERIVED_DESCRIPTION_MAX - 1].rstrip()
            head, sep, _tail = truncated.rpartition(" ")
            return f"{head if sep else truncated}…"
        return value


class TargetSuggestion(BaseModel):
    """A single suggested target derived from the user's experience profile."""

    label: str = Field(min_length=1, max_length=200)
    description: str = Field(max_length=500)
    core_skills: list[str] = Field(default_factory=list)


class TargetSuggestions(BaseModel):
    """LLM response containing 2-3 suggested targets."""

    suggestions: list[TargetSuggestion] = Field(default_factory=list)


# ---- Match result shapes (suggest_and_match output) --------------------------


class MatchedSuggestion(BaseModel):
    """A suggestion that was matched to an existing target or flagged as new."""

    suggestion: TargetSuggestion
    matched_target: JobTarget | None = None
    is_new: bool = True


class MatchedSuggestions(BaseModel):
    """Result of suggest_and_match: suggestions with match info."""

    matches: list[MatchedSuggestion] = Field(default_factory=list)
