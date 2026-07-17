"""Suggest job targets from the user's OptimizedDoc via LLM.

Given the user's structured experience (roles, skills, outcomes), the LLM
suggests 2-3 concrete role targets. Each suggestion includes a label,
short description, and core skills — enough to create a useful target
immediately. Detailed scoring profiles come later via reference JDs.
"""

from app.models.experience import OptimizedPayload
from app.models.llm import LLMResult, Message, ModelId
from app.models.targets import TargetSuggestions
from app.services.llm.client import LLMClient, complete_json

DEFAULT_MODEL: ModelId = "claude-sonnet-4-6"
DEFAULT_PURPOSE = "target.suggest"
QUERY_DEFAULT_PURPOSE = "target.suggest_from_query"

SYSTEM_PROMPT = """\
You are a career advisor. Given a user's structured experience profile \
(roles, skills, outcomes), suggest 2-3 job targets they should pursue.

Return JSON matching this exact schema:

{
  "suggestions": [
    {
      "label": "Senior Frontend Engineer",
      "description": "Frontend-focused roles at mid-to-large companies \
leveraging your React and TypeScript expertise.",
      "core_skills": ["React", "TypeScript", "CSS", "Testing"]
    }
  ]
}

Rules:
- Suggest 2-3 targets. Each should be a distinct career direction.
- "label" is a concise role title (e.g., "Staff Full-Stack Engineer", \
"Engineering Manager", "Senior DevOps Engineer").
- "description" is 1-2 sentences explaining why this target fits and what \
kinds of companies/teams to look for.
- "core_skills" lists 3-6 skills from the user's profile most relevant to \
this target. Use canonical names (React not reactjs, TypeScript not TS).
- Base suggestions ONLY on the user's actual experience. Do not invent \
skills or roles they haven't held.
- Vary seniority or function across suggestions when the experience supports \
it (e.g., IC vs management, frontend vs full-stack).
- Return ONLY the JSON object. No prose, no markdown, no code fences."""


QUERY_SYSTEM_PROMPT = """\
You are a career advisor. A user typed a free-text role they are searching \
for. Turn it into concrete job targets they can pursue.

Return JSON matching this exact schema:

{
  "suggestions": [
    {
      "label": "Senior Frontend Engineer",
      "description": "Frontend-focused roles leveraging React/TypeScript at \
mid-to-large companies.",
      "core_skills": ["React", "TypeScript", "CSS", "Testing"]
    }
  ]
}

Rules:
- Return 3-5 suggestions.
- The FIRST suggestion MUST be the canonical, well-formed version of exactly \
what the user searched for — fix casing/abbreviations (e.g. "sr fe eng" → \
"Senior Frontend Engineer") but DO NOT broaden or change the role itself.
- The remaining suggestions are CLOSE neighbours the user might also pursue: \
adjacent seniority (Staff, Lead), a common specialization, or an immediately \
neighbouring function. They must be plausible alternatives to the query — NOT \
a generic career-change list.
- "label" is a concise, canonical role title.
- "description" is 1 sentence on who the role fits and what to look for.
- "core_skills" lists 3-6 canonical skills typical for the role (React not \
reactjs, TypeScript not TS).
- If the user's background is provided, prefer neighbours that fit it — but \
the FIRST suggestion always reflects the query itself.
- Return ONLY the JSON object. No prose, no markdown, no code fences."""


def _build_user_message(payload: OptimizedPayload) -> str:
    """Serialize the OptimizedPayload into a compact text summary for the LLM."""
    parts: list[str] = []

    if payload.summary:
        parts.append(f"Summary: {payload.summary}")

    if payload.roles:
        role_lines = []
        for r in payload.roles:
            line = f"- {r.title} at {r.company} ({r.start}–{r.end or 'present'})"
            if r.skills:
                line += f" | Skills: {', '.join(r.skills)}"
            role_lines.append(line)
        parts.append("Roles:\n" + "\n".join(role_lines))

    if payload.skills:
        skill_lines = []
        for s in payload.skills:
            line = s.name
            if s.years:
                line += f" ({s.years}y)"
            skill_lines.append(line)
        parts.append("Skills: " + ", ".join(skill_lines))

    if payload.outcomes:
        outcome_lines = []
        for o in payload.outcomes:
            line = f"- {o.description}"
            if o.metric and o.value:
                line += f" ({o.metric}: {o.value})"
            outcome_lines.append(line)
        parts.append("Key outcomes:\n" + "\n".join(outcome_lines[:10]))

    return "\n\n".join(parts) if parts else "No experience data available."


async def suggest_targets(
    llm: LLMClient,
    *,
    payload: OptimizedPayload,
    model: ModelId = DEFAULT_MODEL,
    purpose: str = DEFAULT_PURPOSE,
) -> tuple[TargetSuggestions, LLMResult]:
    """Suggest targets from an OptimizedPayload.

    Returns (suggestions, result) so callers can log cost.
    """
    user_message = _build_user_message(payload)
    return await complete_json(
        llm,
        model=model,
        system=SYSTEM_PROMPT,
        messages=[Message(role="user", content=user_message)],
        schema=TargetSuggestions,
        purpose=purpose,
        cache_system=True,
    )


def _build_query_message(query: str, payload: OptimizedPayload | None) -> str:
    """Build the user message for query-driven suggestions.

    The raw query goes on the FIRST line (the local MockLLMClient reads it
    from there to synthesize dev suggestions — see ``mock._dev_suggest_from_query``).
    The user's experience, when present, is appended only to *tailor* the
    adjacent neighbours; the query itself is always the primary signal.
    """
    lines = [query.strip()]
    if payload is not None:
        experience = _build_user_message(payload)
        if experience and experience != "No experience data available.":
            lines.append("")
            lines.append("The user's background, for tailoring adjacent suggestions:")
            lines.append(experience)
    return "\n".join(lines)


async def suggest_targets_from_query(
    llm: LLMClient,
    *,
    query: str,
    payload: OptimizedPayload | None = None,
    model: ModelId = DEFAULT_MODEL,
    purpose: str = QUERY_DEFAULT_PURPOSE,
) -> tuple[TargetSuggestions, LLMResult]:
    """Suggest targets from a free-text role query.

    Unlike :func:`suggest_targets` (which requires a full experience profile),
    this canonicalizes the query into a target plus a few adjacent roles. The
    profile is optional — when provided it tailors the neighbours, but the
    query alone is enough. Backs the catalog-search LLM fallback.

    Returns (suggestions, result) so callers can log cost.
    """
    user_message = _build_query_message(query, payload)
    return await complete_json(
        llm,
        model=model,
        system=QUERY_SYSTEM_PROMPT,
        messages=[Message(role="user", content=user_message)],
        schema=TargetSuggestions,
        purpose=purpose,
        cache_system=True,
    )
