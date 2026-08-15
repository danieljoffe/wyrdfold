"""Canonicalize a job posting's own title into a reusable target label.

``from_url`` derives its label from the posting title, because a user-supplied
override poisons matching and the shared catalog. But a posting title is
written to sell one specific requisition at one specific company, so taking it
verbatim produced targets like::

    Senior Product Builder (Product Manager), Enterprise Readiness & Admin Platform

— 78 characters of company-, team- and product-specific phrasing. Two problems
follow, and both are structural rather than cosmetic:

1. **It is not a role profile.** Targets are long-lived things a user scores
   every future posting against. A label naming one company's internal platform
   describes a requisition, not a role.
2. **It defeats dedup.** ``crud.normalize_label`` — the key behind the
   ``targets_normalized_label_key`` UNIQUE constraint — only lowercases, trims
   and collapses whitespace. Punctuation, parentheticals and comma-suffixes all
   survive into the key, so a title like the above can never collide with the
   "Senior Product Manager" some other user already follows. Every URL-created
   target minted its own catalog row.

So the canonicalization has to happen **before** ``find_matching_target``, not
after: the whole point is that the canonical form is what gets matched and what
becomes the dedup key. Normalizing after the row exists would already have
written the wrong key.

This is deliberately a *separate, narrower* prompt from
``normalize_manual.SYSTEM_PROMPT``. That one's job is to stay as close to the
user's typed words as possible (they typed what they meant). This one's job is
the opposite — to strip everything the employer added around the role.
"""

from pydantic import BaseModel, Field

from app.models.llm import LLMResult, Message, ModelId
from app.services.llm.client import LLMClient, complete_json

DEFAULT_MODEL: ModelId = "claude-sonnet-4-6"
DEFAULT_PURPOSE = "target.normalize_posting_title"

# Enough JD to disambiguate the function when the title alone is jargon
# ("Product Builder" reads as engineering until the body says otherwise);
# far short of the whole posting, which would cost tokens for no extra signal.
_JD_CONTEXT_CHARS = 1500

MAX_LABEL_CHARS = 80


class NormalizedTitle(BaseModel):
    """The canonical role title for a posting."""

    label: str = Field(
        ...,
        min_length=1,
        max_length=MAX_LABEL_CHARS,
        description="Canonical, reusable role title.",
    )


SYSTEM_PROMPT = """\
You are a job-market taxonomist. Given one job posting's title (and an \
excerpt of its description for disambiguation), return the canonical role \
title that a job seeker would use to describe the KIND of role this is.

Return JSON matching this exact schema:

{
  "label": "Senior Product Manager"
}

The label names a ROLE, not a requisition. Multiple postings at different \
companies must normalize to the SAME label.

REMOVE:
- Company, team, org, product, and platform names, and any trailing \
qualifier after a comma or dash that names one \
("Senior Product Builder (Product Manager), Enterprise Readiness & Admin \
Platform" -> "Senior Product Manager").
- Requisition IDs, numbers, and codes ("Software Engineer III - R2938" -> \
"Software Engineer III" only if the numeral is a real level; drop bare req \
codes).
- Location, remote/hybrid/onsite, and employment-type suffixes \
("Backend Engineer (Remote, US) - Contract" -> "Backend Engineer").
- Marketing adjectives and punctuation noise ("Rockstar", "Ninja", emoji, \
"!!!").

KEEP:
- The genuine seniority word if the posting has one: Junior, Mid, Senior, \
Staff, Principal, Lead, Director, VP, Head of, Chief.
- The genuine function and, where it is a real discipline rather than a \
team name, one specialization: "Frontend", "Backend", "Full-Stack", \
"Platform", "Security", "Data", "Machine Learning", "Site Reliability".
- Numeric levels only when they are industry-legible ("Software Engineer \
III").

NEVER:
- Invent a seniority the posting does not state. An untitled "Software \
Engineer" stays "Software Engineer".
- Invent a specialization the posting does not support.
- Return a company name, a person's name, or a sentence.
- Exceed 80 characters. Prefer 2-5 words.

If the title is unusable (empty, gibberish, or clearly not a job title), \
infer the role from the description excerpt instead. If that is impossible \
too, return "Untitled Role".

Return ONLY the JSON object. No prose, no markdown, no code fences."""


def _build_user_message(title: str, jd_text: str) -> str:
    excerpt = (jd_text or "").strip()[:_JD_CONTEXT_CHARS]
    parts = [f"Posting title: {title.strip() or '(none)'}"]
    if excerpt:
        parts.append("")
        parts.append("Description excerpt:")
        parts.append(excerpt)
    return "\n".join(parts)


async def normalize_posting_title(
    llm: LLMClient,
    *,
    title: str,
    jd_text: str,
    model: ModelId = DEFAULT_MODEL,
    purpose: str = DEFAULT_PURPOSE,
) -> tuple[NormalizedTitle, LLMResult]:
    """Canonicalize one posting title into a reusable role label."""
    return await complete_json(
        llm,
        model=model,
        system=SYSTEM_PROMPT,
        messages=[Message(role="user", content=_build_user_message(title, jd_text))],
        schema=NormalizedTitle,
        purpose=purpose,
        cache_system=True,
    )
