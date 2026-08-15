"""Catalog-wide skill extraction — one LLM call per job, target-independent.

Backs the `/search?skill=react` facet: a search filter can only find what
something wrote down, and the Phase-2 harvest only ever reads the graded
slice (a few hundred of ~16k live jobs). This pass reads every job once, so
the facet covers the corpus.

WHY A SEPARATE CALL rather than folding the field into the qualification
tagger (which already reads every job exactly once):

    Measured 2026-08-15, 10 runs on the committed golden (n=28) — asking the
    classifier to ALSO extract skills cost real classification accuracy:

        role_family baseline        95.0%  (misses {2,1,1,1,2})
        role_family + skills task   90.7%  (misses {3,2,3,3,2})

    Disjoint distributions, ~4 points. ``is_us`` held 100% throughout, and
    widening the tagger's JD snippet 600->2000 measured NEUTRAL, so the cost
    was the extra task in the prompt, not the added context. ``role_family``
    feeds the strict off-family gate, so that trade would drop legitimate
    family-adjacent jobs out of users' matches to power a search facet.

    One extra cheap call keeps classification untouched. Do not "optimize"
    this back into the tagger prompt without re-running that A/B.

MODEL — deepseek-v3-2, chosen by bake-off (2026-08-15, 6 candidates x ~20 real
JDs, Sonnet as the yardstick):

    sonnet-4.6      recall ref     valid 100%   ~$165/mo at current intake
    deepseek-v3.2   recall  52%    valid 100%   ~$12/mo      <- chosen
    mistral-24b     recall  31%    valid 100%   ~$2/mo
    gemma-3-12b     recall  37%    valid  62%
    qwen3.7-flash   unusable (0% valid JSON)
    gpt-oss-120b    unusable (65% valid, 9s p50)
    nova-micro      unusable (80% valid, 504s)

    The cheap tail is not merely lower-recall: Mistral missed the conceptual
    skills entirely on a systems role (returned only the four languages) and
    emitted "claud" for "claude" — and because the filter matches text
    exactly, a typo is a dead facet value nobody can ever click. DeepSeek
    returned valid JSON on 36/36 calls and tracks Sonnet's picks closely.

PROMPT — the "be thorough" wording is deliberate and measured: an earlier
"do not pad the list" instruction suppressed every model (deepseek 5.5 -> 8.5
skills/job when switched). A skill the posting requires but we omit is a job
nobody can find, so recall is the thing to optimize here; precision is
protected by the normalizer's caps + the grounding of a real JD.

Fail-soft like the tagger: any error returns ``([], None)`` and the caller
simply omits the column, leaving it NULL for a later cycle to fill.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field, field_validator

from app.config import settings
from app.models.llm import LLMResult, Message, ModelId
from app.services.fit.job_fit import clean_skill_list
from app.services.llm.client import LLMClient, complete_json
from app.services.llm.errors import LLMServiceError
from app.services.llm.untrusted import UNTRUSTED_CONTENT_DIRECTIVE, wrap_untrusted
from app.services.qualification.heuristics import clean_description

logger = logging.getLogger(__name__)

# Bake-off winner (see module docstring). The RUNTIME model is
# ``settings.skills_extraction_model``; this constant is the documented default
# and is pinned into the prompt-regression golden so a swap can't merge silently.
SKILLS_MODEL: ModelId = "deepseek-v3-2"
SKILLS_PURPOSE = "qualification.skills"

# How much JD to send. Skills live in the "Requirements" / "What you'll need"
# section, which the tagger's 600-char classification window never reaches —
# this is the whole reason skills need their own call rather than a wider
# tagger prompt. 2500 chars (~625 tokens) is what the bake-off measured.
SKILLS_JD_CHARS = 2500

# Hard cap on the list. Matches the harvest's cap so both writers produce the
# same shape; deepseek routinely proposes more (18 on one ML posting), which the
# normalizer truncates rather than rejecting.
MAX_SKILLS = 8

_SYSTEM_PROMPT = (
    UNTRUSTED_CONTENT_DIRECTIVE
    + "\n\n"
    + """\
You extract the concrete skills a job posting requires. Return ONLY JSON.

- Canonical lowercase short names ("react", "node.js", "kubernetes", \
"postgresql", "figma", "financial modeling").
- Prefer the common form: "kubernetes" not "k8s", "postgresql" not "postgres", \
"javascript" not "js".
- The technology or hard skill ITSELF — never a version, adjective, or \
sentence. "react", not "react 18" or "strong react experience".
- NEVER soft traits (communication, teamwork, fast-paced, growth mindset), \
seniority words, or degrees.
- Only what the posting actually asks for; do not invent a stack it never names.
- List EVERY concrete skill the posting names, up to 8. Be thorough: a skill \
the posting requires but you omit is a job nobody can find. Include the \
languages, frameworks, databases, cloud services and tools it mentions.
- Empty list if the posting names no concrete skills.

Return exactly: {"skills": ["...", "..."]}"""
)


class ExtractedSkills(BaseModel):
    """LLM output: the job's required skills, normalized on the way in."""

    skills: list[str] = Field(default_factory=list)

    @field_validator("skills", mode="before")
    @classmethod
    def _clean(cls, value: object) -> list[str]:
        """Normalize / dedupe / bound / cap via the ONE shared cleaner.

        ``clean_skill_list`` is the same function the Phase-2 harvest uses and
        the same normalization the search filter queries with — the DB
        predicate is exact-string jsonb containment, so a second
        implementation drifting here would silently halve a facet's results.
        """
        return clean_skill_list(value, cap=MAX_SKILLS)


def _build_user_message(*, title: str, description: str | None) -> str:
    """Title + cleaned/truncated JD, both inside an untrusted fence.

    Scraped, attacker-controllable text: the system directive tells the model
    to treat it as data to analyze, never instructions to follow.
    """
    jd = clean_description(description)[:SKILLS_JD_CHARS]
    return (
        f"Title: {wrap_untrusted(title, name='title', block=False)}\n\n"
        "Description:\n" + wrap_untrusted(jd or "(no description provided)", name="description")
    )


async def extract_skills(
    llm: LLMClient,
    *,
    title: str,
    description: str | None,
    model: ModelId | None = None,
) -> tuple[list[str], LLMResult | None]:
    """Extract one job's required skills. Returns ``(skills, llm_result)``.

    ``([], None)`` on any failure — the caller omits the column and a later
    cycle retries. ``llm_result`` is returned so the caller can log cost
    (the poller enqueues it under ``SKILLS_PURPOSE``).
    """
    if not (description or "").strip():
        return [], None
    try:
        parsed, result = await complete_json(
            llm,
            model=model or settings.skills_extraction_model,
            system=_SYSTEM_PROMPT,
            messages=[
                Message(
                    role="user", content=_build_user_message(title=title, description=description)
                )
            ],
            schema=ExtractedSkills,
            purpose=SKILLS_PURPOSE,
            max_tokens=400,
        )
    except LLMServiceError:
        # Classified upstream condition (dead key, spent cap, rate limit) — the
        # caller's breaker handles the cycle-wide response; one line, no stack.
        raise
    except Exception:
        logger.warning("Skill extraction failed for %r; leaving skills NULL", title[:80])
        return [], None
    return parsed.skills, result
