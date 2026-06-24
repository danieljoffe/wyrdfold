"""L2 job qualification tagger — ONE structured LLM call (#60).

The qualification firewall classifies each posting ONCE, target-independently,
and writes the intrinsic facts onto the jobs row so per-target grading can
pre-filter cheaply. This module is the L2 layer: a single Haiku call per job
returning a structured ``QualificationTags`` object. L1 (``heuristics``) cleans
the description, computes the cache hash, and supplies the permissive US guess
the prompt uses as a prior.

Why one call per job (not batched like Phase 1 title triage): the tagger reads
the full description (US inference, genuine-role detection, employment type all
need the body, not just the title), so batching 250 descriptions into one
prompt would blow the context budget and muddy attribution. One job per call
keeps each classification cheap (~1-2K input tokens at Haiku pricing) and lets
the content-hash cache skip the vast majority of re-polls.

Reuses the exact LLM client abstraction Phase 1 uses
(``app.services.llm.client.complete_json`` over the ``LLMClient`` Protocol) —
no new client. Model is Haiku 4.5, the cheap-fast tier, consistent with
``relevance.title_triage``.

Fail-soft: any LLM/parse/network error returns ``None`` and the caller leaves
the row's tags NULL (not-yet-tagged), so a tagger outage never blocks polling
and the row is simply re-attempted on a later cycle.
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field

from app.models.llm import LLMResult, Message, ModelId
from app.services.llm.client import LLMClient, complete_json
from app.services.qualification.heuristics import (
    clean_description,
    is_us_location,
)

logger = logging.getLogger(__name__)

# Haiku 4.5 — the cheap-fast tier, same tier as Phase 1 title triage. This is a
# bounded extraction/classification task, not deep judgement; Sonnet would just
# inflate cost. Pinned into the prompt-regression golden contract
# (tests/test_prompt_regression.py) so a model swap can't merge silently.
QUALIFICATION_MODEL: ModelId = "claude-haiku-4-5"
QUALIFICATION_PURPOSE = "qualification.tagger"

# Cap the description we send. Job bodies run long; the signals the tagger needs
# (country, seniority cues, "general application" boilerplate, contract/intern
# wording) are dense near the top. 6000 chars (~1.5K tokens) is plenty and keeps
# per-call cost flat regardless of a vendor's verbose footer.
_MAX_DESCRIPTION_CHARS = 6000

RoleFamily = Literal[
    "engineering",
    "data_ml",
    "product",
    "design",
    "customer_experience",
    "sales",
    "marketing",
    "finance",
    "operations",
    "people_hr",
    "legal",
    "other",
]

Seniority = Literal[
    "intern",
    "entry",
    "ic",
    "senior_ic",
    "manager",
    "director",
    "vp",
    "exec",
    "unknown",
]

EmploymentType = Literal[
    "full_time",
    "contract",
    "part_time",
    "internship",
    "temporary",
    "unknown",
]


class QualificationTags(BaseModel):
    """The structured verdict the LLM returns for one job.

    Maps 1:1 onto the ``jobs`` qualification columns (migration
    20260624090000). ``metro`` is the only optional field — null when no
    single city is identifiable (remote-only / multi-metro / unstated).
    """

    is_us: bool = Field(
        description="True if the role is US-based. A multi-location role that "
        "includes ANY US location counts as US."
    )
    us_confidence: int = Field(
        ge=0, le=100, description="0-100 certainty in the is_us verdict."
    )
    role_family: RoleFamily
    seniority: Seniority
    employment_type: EmploymentType
    metro: str | None = Field(
        default=None,
        description="Primary metro/city when identifiable, else null.",
    )
    is_remote: bool = Field(description="True if the role is remote-eligible.")
    is_genuine_role: bool = Field(
        description="False for talent-pool / 'general application' / evergreen "
        "non-roles."
    )


# The rules below are baked from a validated dry-run (#60). They are the
# difference between a tagger that scores 60% and one that scores ~95% on the
# hard cases, so they live in the SYSTEM prompt (stable, cacheable) verbatim.
_SYSTEM_PROMPT = """\
You are a job-posting classifier. Given ONE job posting (title, company, \
location, and description) you return a single structured verdict describing \
intrinsic, target-independent facts about the role. You are NOT judging fit for \
any candidate — only classifying what the posting itself is.

Return exactly these fields:

is_us (bool) + us_confidence (0-100)
- INFER THE COUNTRY FROM THE CITY when the location names a city without a \
country. Examples: Taichung → Taiwan → false. Calgary / Toronto / Vancouver → \
Canada → false. London → UK → false. Bengaluru / Mumbai → India → false. \
Munich / Berlin → Germany → false.
- "Remote (Bulgaria)" / "Remote - EMEA" / "Remote, India" → false (the \
parenthetical/qualifier names the country/region).
- A MULTI-LOCATION role that includes ANY US location is US → true. \
"New York, Stamford, London" → true. "Bellevue, Washington; Toronto, Ontario, \
Canada" → true (Bellevue WA is US). The presence of one non-US city does NOT \
make a multi-US role non-US.
- "Remote - United States", "US", "USA", "City, ST" (two-letter US state) → \
true.
- Genuinely global/unstated remote with no country signal → make your best \
call and lower us_confidence accordingly.

role_family — the coarse role FUNCTION, exactly one of: engineering, data_ml, \
product, design, customer_experience, sales, marketing, finance, operations, \
people_hr, legal, other.
- Disambiguation that trips up keyword matching:
  • "Sales Engineer" → sales (the function is selling). \
"Solutions Engineer / Solutions Architect" presale → sales.
  • "Legal Engineer" / "Legal Operations" → legal.
  • "AI Engineer", "ML Engineer", "Machine Learning Engineer", "Data \
Scientist", "Data Engineer", "Analytics Engineer" → data_ml.
  • "Software Engineer", "Infrastructure Engineer", "DevOps", "SRE", \
"Platform Engineer", "Security Engineer" → engineering.
  • "Controller", "Accountant", "FP&A", "Treasury" → finance.
  • "NOC", "Support", "Technical Support", "QoS", "Network Operations" → \
operations or customer_experience (customer-facing support → \
customer_experience; internal network/infra ops → operations).
  • "Customer Success", "Customer Experience", "Account Management \
(post-sale)", "Onboarding" → customer_experience.
  • "Recruiter", "Talent", "People Operations", "HR" → people_hr.

seniority — the ORG LEVEL / SCOPE, exactly one of: intern, entry, ic, \
senior_ic, manager, director, vp, exec, unknown. Read SCOPE, not title \
keywords:
- "Product Manager", "Engineer", "Designer", "Analyst" (no modifier) → ic \
(individual contributor; "Manager" in "Product Manager" is the function, not \
people-management).
- "Associate Manager", "Manager, X", "Engineering Manager", "Team Lead \
(people)" → manager (manages people).
- "Principal", "Staff", "Lead" (IC senior), "Senior X" → senior_ic.
- "I", "II", "Junior", "Associate <IC role>" (e.g. "Associate Accountant") → \
entry.
- "Intern" → intern.
- "Head of X", "VP", "Vice President" → vp.
- "Director", "Senior Director" → director.
- "Chief", "C-level" (CEO/CTO/CFO/CMO/COO), "President", "Founder" → exec.
- Can't tell → unknown.

employment_type — exactly one of: full_time, contract, part_time, internship, \
temporary, unknown.
- "(Contract)", "Contractor", "Fixed-term", "FTC" → contract.
- "Intern", "Internship", "Co-op" → internship.
- "Part-time" → part_time. "Temporary", "Seasonal" → temporary.
- Default to full_time only when the posting reads as a standard permanent \
role; otherwise unknown.

metro (string or null) — the primary city/metro if one is clearly identifiable \
(e.g. "San Francisco", "London", "Taichung"); null for remote-only, \
multi-metro with no primary, or unstated.

is_remote (bool) — true if the role is remote-eligible (says Remote / Hybrid \
with remote option / "work from anywhere"); false for explicitly on-site.

is_genuine_role (bool) — false when the posting is NOT a specific open req: \
"General Application", "Talent Community", "Future Opportunities", \
"Join our talent pool", evergreen "We're always hiring" pages. true for a \
normal specific role.

Be decisive. Use the provided heuristic hints as a prior, but TRUST THE \
DESCRIPTION when it contradicts them."""


def _build_user_message(
    *,
    title: str,
    company: str | None,
    location: str | None,
    description: str,
) -> str:
    """Compose the per-job user message.

    Carries the four intrinsic fields plus the L1 permissive US guess as an
    explicit prior the prompt tells the model to override when the description
    contradicts it. The description is already cleaned + truncated by the
    caller.
    """
    l1_us = is_us_location(location)
    lines = [
        f"Title: {title}",
        f"Company: {company or '(unknown)'}",
        f"Location: {location or '(unstated)'}",
        f"Heuristic US guess (permissive prior, override if wrong): {l1_us}",
        "",
        "Description:",
        description or "(no description provided)",
    ]
    return "\n".join(lines)


async def tag_job(
    llm: LLMClient,
    *,
    title: str,
    company: str | None,
    location: str | None,
    description: str | None,
    model: ModelId = QUALIFICATION_MODEL,
    purpose: str = QUALIFICATION_PURPOSE,
) -> tuple[QualificationTags | None, LLMResult | None]:
    """Classify ONE job. Returns ``(tags, llm_result)``.

    ``tags`` is ``None`` (and ``llm_result`` ``None``) on any LLM/parse/network
    error — fail-soft so the caller leaves the row's qualification columns NULL
    (not-yet-tagged) and a later poll re-attempts it. ``llm_result`` is returned
    for cost logging on success.

    The caller is responsible for the content-hash cache (``qualified_hash``);
    this function always calls the model when invoked.
    """
    cleaned = clean_description(description)[:_MAX_DESCRIPTION_CHARS]
    user_message = _build_user_message(
        title=title,
        company=company,
        location=location,
        description=cleaned,
    )

    try:
        parsed, result = await complete_json(
            llm,
            model=model,
            system=_SYSTEM_PROMPT,
            messages=[Message(role="user", content=user_message)],
            schema=QualificationTags,
            purpose=purpose,
            # One verdict object — small output. 1024 covers the JSON envelope
            # with headroom.
            max_tokens=1024,
            cache_system=True,
        )
    except Exception:
        logger.exception(
            "Qualification tagging failed for %r @ %r; leaving tags NULL",
            title,
            location,
        )
        return None, None

    return parsed, result
