"""Pydantic models for user profile — notification + identity fields."""

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# E.164: leading '+', country code starting 1-9, then up to 14 more digits.
# Twilio rejects malformed numbers at send time and the failure is swallowed
# in the poller — validate here so the user gets immediate feedback.
_E164_RE = re.compile(r"^\+[1-9]\d{1,14}$")


def _normalize_phone(value: str | None) -> str | None:
    """Normalize a phone number to E.164, formatting on the user's behalf.

    Accepts the way people actually type numbers — ``(415) 555-2671``,
    ``415-555-2671``, ``4155552671``, ``+1 415 555 2671``, ``1.415.555.2671`` —
    and returns E.164 (``+14155552671``). A bare 10-digit number (or 11 digits
    starting with ``1``) is assumed **US**, since the corpus is US-only; a
    genuinely international number must carry a leading ``+`` and country code
    because we can't infer the country otherwise. Empty / whitespace → ``None``
    ("clear the field"). Raises only when the input can't be resolved to a valid
    E.164 number at all.
    """
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    # Preserve a leading '+' (international dialing prefix) then keep only
    # digits — drops spaces, hyphens, parentheses, dots, and stray letters.
    has_plus = stripped.startswith("+")
    digits = re.sub(r"\D", "", stripped)
    if has_plus:
        candidate = "+" + digits
    elif len(digits) == 11 and digits.startswith("1"):
        candidate = "+" + digits  # US number typed with its country code, no '+'
    elif len(digits) == 10:
        candidate = "+1" + digits  # bare US 10-digit → assume +1
    else:
        candidate = digits  # unknown length / country → fail the check below
    if not _E164_RE.match(candidate):
        raise ValueError(
            "Couldn't read that as a phone number — enter a 10-digit US number "
            "(e.g. 415-555-2671) or an international number with its country "
            "code (e.g. +44 20 7946 0958)."
        )
    return candidate


class NotificationPreferences(BaseModel):
    """Read model for notification + jobs-list preferences.

    ``list_min_score`` is intentionally separate from
    ``job_score_threshold`` (email) and ``sms_score_threshold`` —
    historically the email threshold was reused as the list filter, but
    the email/SMS UIs are disabled until SMTP + Twilio are configured,
    leaving users no way to control the list view. NULL means "no
    auto-filter" — caller must pass ``min_score`` explicitly via chip.
    """

    job_notifications_enabled: bool = False
    job_score_threshold: int = 100
    sms_notifications_enabled: bool = False
    sms_score_threshold: int = 100
    sms_daily_limit: int = 5
    list_min_score: int | None = None
    phone_number: str | None = None
    email: str | None = None
    # Server-derived: false when the operator hasn't configured the
    # corresponding provider credentials. The frontend uses these to
    # disable the toggles; the PATCH handler rejects attempts to enable
    # a channel whose backend isn't reachable.
    email_available: bool = True
    sms_available: bool = True


class NotificationPreferencesUpdate(BaseModel):
    """Write model — all fields optional so callers can patch individual settings."""

    job_notifications_enabled: bool | None = None
    job_score_threshold: int | None = Field(default=None, ge=0, le=200)
    sms_notifications_enabled: bool | None = None
    sms_score_threshold: int | None = Field(default=None, ge=0, le=200)
    sms_daily_limit: int | None = Field(default=None, ge=1, le=50)
    # 0..100 matches the score range. ``None`` here means "don't touch
    # the column" (default for partial PATCH); to clear an existing
    # value the caller passes 0 (semantically "no floor").
    list_min_score: int | None = Field(default=None, ge=0, le=100)
    phone_number: str | None = Field(default=None, max_length=20)

    @field_validator("phone_number", mode="before")
    @classmethod
    def _validate_phone(cls, value: str | None) -> str | None:
        return _normalize_phone(value)


class IdentityFields(BaseModel):
    """Read model for resume / cover-letter identity. Backend sources contact
    info from here so the frontend never has to send it (F3-A).
    """

    name: str | None = None
    email: str | None = None
    phone_number: str | None = None
    location: str | None = None
    linkedin_url: str | None = None
    website_url: str | None = None


class IdentityFieldsUpdate(BaseModel):
    """Write model — all fields optional. Empty strings clear the field."""

    name: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=320)
    phone_number: str | None = Field(default=None, max_length=20)
    location: str | None = Field(default=None, max_length=200)
    linkedin_url: str | None = Field(default=None, max_length=500)
    website_url: str | None = Field(default=None, max_length=500)

    @field_validator("phone_number", mode="before")
    @classmethod
    def _validate_phone(cls, value: str | None) -> str | None:
        return _normalize_phone(value)


# ---------------------------------------------------------------------------
# Resume style (docx typography presets — see app/services/docx/style.py)
# ---------------------------------------------------------------------------

# Curated, author-owned looks. Users pick from this closed set rather than
# tuning fonts/sizes individually — every combination is designer-vetted, so a
# user cannot produce a broken resume. The typography behind each preset lives
# server-side in app/services/docx/style.py and is intentionally NOT exposed.
ResumeStylePreset = Literal["modern", "classic", "compact", "executive"]

# Applied to the name + section headings only (ATS parsers ignore color).
# "black" is the no-accent option for conservative / regulated audiences.
ResumeStyleAccent = Literal["slate", "navy", "black", "burgundy", "forest"]


class ResumeStyleSettings(BaseModel):
    """A user's resume style choice. Two enums, nothing free-form.

    Stored as JSONB on ``user_profiles.resume_style_settings`` (the default)
    and ``documents.style_settings`` (per-record override, deferred UI). A
    NULL column means "no choice yet" and renders today's unstyled pandoc
    default — see ``download_tailored_resume``.
    """

    preset: ResumeStylePreset = "modern"
    accent: ResumeStyleAccent = "slate"


class ResumeStyleSettingsUpdate(BaseModel):
    """Write model — both optional so the UI can PATCH one axis at a time."""

    preset: ResumeStylePreset | None = None
    accent: ResumeStyleAccent | None = None


# ---------------------------------------------------------------------------
# Onboarding completion + step tracking
# ---------------------------------------------------------------------------

# Mirrors the Step union in apps/wyrdfold/src/app/onboarding/OnboardingWizard.tsx.
# Step names are hyphenated by FE convention (CSS-class friendly) — keep
# parity rather than enforcing snake_case at the schema boundary.
OnboardingStep = Literal[
    "path-chooser",
    "identity",
    "subscribe",
    "upload-resume",
    "add-job",
    "pick-targets",
    "conversation",
    "completion",
]

# Three onboarding paths (see STEPS_BY_PATH in OnboardingWizard.tsx).
# A = full setup (resume + JD + targets); B = resume + targets;
# C = conversation + targets.
OnboardingPath = Literal["A", "B", "C"]


class OnboardingStatus(BaseModel):
    """Read model for the user's onboarding progress.

    A user is considered "onboarded" when ``completed_at`` is non-null.
    Until then, the dashboard redirects them to the wizard — unless
    ``deferred_at`` is set, which records "deliberately exited the wizard
    without finishing" (the global 'Finish setup later' exit): the
    redirect is suppressed but /onboarding stays enterable and resumes
    mid-flow. The wizard consumes ``current_step`` + ``path`` to resume
    (Stage 2 of plan-wyrdfold-onboarding-completion-tracking.md).
    """

    completed_at: datetime | None = None
    deferred_at: datetime | None = None
    path: OnboardingPath | None = None
    current_step: OnboardingStep | None = None


class OnboardingStepUpdate(BaseModel):
    """Write model for PATCH /profile/onboarding/step.

    Both fields are optional — most transitions only update ``current_step``,
    but ``path`` is set once on the PathChooser → Identity transition.
    Server treats unset fields as "leave the column alone."
    """

    path: OnboardingPath | None = None
    current_step: OnboardingStep | None = None


class LlmUsageWindow(BaseModel):
    """One budget window: dollars spent vs the cap (0 cap = disabled)."""

    spent_usd: float
    limit_usd: float


class JobsFilterPrefs(BaseModel):
    """The /jobs page's per-target filter snapshots (#866).

    One map of ``{target_id | "__all__": JobsFilterState}``, treated as an
    OPAQUE client-owned blob: the server never reads inside it, and the
    client re-validates each entry on read (``coerceStoredFilters``), so the
    only server-side concerns are ownership (RLS) and size. The caps below
    stop the column becoming an unbounded dumping ground: a user can follow
    at most a few dozen targets, and one snapshot is a handful of short
    scalar fields.
    """

    filters: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @field_validator("filters")
    @classmethod
    def _bounded(cls, v: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        if len(v) > 64:
            raise ValueError("too many filter snapshots (max 64)")
        for key in v:
            if not key or len(key) > 64:
                raise ValueError("filter snapshot keys must be 1-64 characters")
        import json as _json

        if len(_json.dumps(v)) > 16_000:
            raise ValueError("filter snapshots too large (max 16KB serialized)")
        return v


class LlmUsageResponse(BaseModel):
    """Read model for GET /profile/llm-usage — the user's allowance state.

    ``monthly_resets_at`` approximates when capacity starts freeing: the
    oldest cost row in the rolling 30-day window plus 30 days. Null when
    the user has no spend in the window.
    """

    hourly: LlmUsageWindow
    daily: LlmUsageWindow
    monthly: LlmUsageWindow
    monthly_resets_at: datetime | None = None
    analysis_daily_used: int
    analysis_daily_limit: int
    # Who pays when this user spends (#858) — the SAME resolution the budget
    # gates enforce (ResolvedQuota.key_source). The allowance widget renders
    # only for "host": for "user" the numbers are their own key's spend with
    # no managed cap, and for "none" (saas free, no usable key) every AI call
    # 402s before a quota is read, so showing "$0.00 of $5.00" told a free
    # user they had hosted budget they could never spend.
    key_source: Literal["host", "user", "none"]
    # Whether this server offers BYOK at all (BYOK_MASTER_KEY configured) —
    # lets the "none" copy honestly say "add a key" vs "requires a paid plan".
    byok_available: bool
