"""Derive a ScoringProfile + search keywords from a target label via LLM.

Given a target label (e.g., "Senior Frontend Engineer") and the user's
experience context, the LLM generates:
1. A full ScoringProfile for scoring jobs against this target
2. Search keywords (role title variations) for filtering jobs from ATS APIs

Follows the same pattern as derive_profile.py:
- Static system prompt (cacheable via Anthropic prompt caching)
- Label + user context are the variable content
- complete_json() validates output against DerivedTarget
"""

from app.models.llm import LLMResult, Message, ModelId
from app.models.targets import DerivedTarget
from app.services.llm.client import LLMClient, complete_json

DEFAULT_MODEL: ModelId = "claude-sonnet-4-6"
DEFAULT_PURPOSE = "target.derive_from_label"

# The scoring_profile is built from role-generic industry knowledge of the
# LABEL ALONE — never an individual's résumé — so a shared target's rubric
# isn't skewed by whoever activated it (#5 layer 1). The résumé only ever feeds
# fit_score (targets/fit_score.py).
SYSTEM_PROMPT_GENERIC = """\
You are a job-search scoring-profile generator. Given a target role label, \
generate two things based on what this role GENERALLY requires across the \
industry — NOT tailored to any individual's background:

1. A scoring profile for evaluating job postings against this target role
2. Search keywords for finding matching job postings on company career pages

Return JSON matching this exact schema:

{
  "scoring_profile": {
    "categories": {
      "core_skills": {
        "keywords": {"React": 3, "TypeScript": 3},
        "weight": 2.0
      },
      "secondary_skills": {
        "keywords": {"Node.js": 2, "GraphQL": 2},
        "weight": 1.0
      },
      "nice_to_have": {
        "keywords": {"Kubernetes": 1, "Terraform": 1},
        "weight": 0.5
      }
    },
    "seniority": {
      "level": "senior",
      "signals": ["5+ years", "lead", "mentor"]
    },
    "domain": {
      "signals": ["fintech", "payments"],
      "weight": 0.5
    },
    "negative": {
      "keywords": ["junior", "intern", "entry-level"],
      "weight": -10
    }
  },
  "search_keywords": [
    "frontend engineer",
    "front-end engineer",
    "ui engineer",
    "react developer",
    "frontend developer",
    "web developer"
  ],
  "example_promising_titles": [
    "Senior Frontend Engineer",
    "Staff Web Engineer",
    "Principal Frontend Engineer",
    "Senior UI Engineer",
    "Staff Full-Stack Engineer",
    "Frontend Platform Engineer",
    "Senior React Engineer",
    "Web Platform Engineer"
  ],
  "example_unpromising_titles": [
    "Senior Product Designer",
    "Product Marketing Manager",
    "Data Scientist",
    "Security Engineer",
    "Customer Success Manager",
    "Sales Development Representative",
    "Backend Engineer",
    "Mobile Engineer"
  ],
  "description": "Frontend IC at scale: production React + TypeScript work on customer surfaces.",
  "seniority_hint": "staff",
  "domain_hints": ["SaaS", "DTC e-commerce", "developer tools"]
}

Rules for scoring_profile:
- "core_skills": skills ESSENTIAL to this role as it is typically posted. \
Weight each keyword 2-3.
- "secondary_skills": skills commonly preferred or frequently listed for \
this role. Weight each keyword 1-2.
- "nice_to_have": adjacent bonus skills that strengthen a candidate for \
this role. Weight each keyword 1.
- Seniority level: one of "junior", "mid", "senior", "staff", "principal", \
"director". Match the target label's implied seniority.
- Domain signals: industry verticals this role commonly spans (leave thin \
if the role is genuinely domain-agnostic).
- Negative keywords: terms indicating a job is NOT for this target \
(e.g., wrong seniority, wrong specialization).
- Use canonical skill names (React not reactjs, TypeScript not TS).
- Base the profile on the ROLE as it is generally understood across the \
industry, drawing on broad knowledge of what these jobs ask for. Do NOT \
assume any particular candidate's experience — this is a role-generic \
rubric that many different candidates will be scored against.

Rules for search_keywords:
- 5-15 lowercase role title variations and synonyms.
- Include the target label itself (lowercased) and common variations.
- These are used for substring matching against job posting titles on \
career pages, so be broad: include both formal and informal variations.
- Include related but distinct role titles in the same career direction \
(e.g., "frontend engineer" and "ui developer" target similar roles).
- Do NOT include technology names — only role titles.
- Do NOT include seniority prefixes (no "senior", "staff", "lead") — \
the system handles seniority matching separately.

Rules for example_promising_titles:
- 6-10 concrete, properly-cased job titles that are strong matches for \
this role. Include realistic seniority prefixes (Senior, Staff, Principal) \
— these are full titles a hiring page would post, not search keywords.
- Span the range of acceptable seniorities for the target. If the target \
is a senior+ role, include staff/principal variants too.
- Include close-adjacent role variants in the same career direction \
(e.g., for a Frontend Engineer target: also "Full-Stack Engineer", \
"Web Platform Engineer").
- These become POSITIVE few-shot anchors for a downstream binary \
classifier that decides which new job postings to evaluate deeply, so \
choose titles whose meaning is unambiguous.

Rules for example_unpromising_titles:
- 6-10 concrete job titles that look adjacent but are NOT this role. The \
harder cases — same seniority, same company-type, different role function \
— are the most valuable.
- Examples for a Frontend Engineer target: "Senior Product Designer", \
"Product Marketing Manager", "Data Scientist", "Sales Engineer". These \
share keywords but the ROLE itself is different.
- Avoid obvious negatives ("Nurse", "Truck Driver") — those waste prompt \
space. Pick close-but-wrong roles that a keyword scorer alone would admit.
- These become NEGATIVE few-shot anchors. Be precise about role function, \
not technology overlap.

Rules for the slim shape (``description`` / ``seniority_hint`` / \
``domain_hints``)
- ``description`` (80-600 chars): 1-2 paragraphs capturing WHAT THIS ROLE \
IS in general (don't echo the label). Mention the flavor of work — \
operations-heavy? IC craft? transformation-led? — and the kind of \
companies that hire for it. Avoid vague phrases like "great team player". \
This feeds Phase 2's ``## Target`` block in the fit-grading prompt, so \
concreteness pays off.
- ``seniority_hint``: MUST be EXACTLY one of these seven values — ic, \
senior, staff, manager, director, vp, c_level — never any other word. \
Map the title's nomenclature onto the closest one (Lead / Principal -> \
staff, Sr -> senior, Head of -> director, EVP / SVP -> vp, Chief _ \
Officer -> c_level). Pick the level the label implies. This is what \
Phase 2's seniority_fit axis grades against.
- ``domain_hints``: 3-6 industries / verticals / product types this role \
commonly spans (e.g. ["SaaS", "DTC", "healthtech"]). Empty array if the \
role is genuinely domain-agnostic. This feeds Phase 2's domain_fit axis — \
be specific enough to penalise off-domain matches.

Return ONLY the JSON object. No prose, no markdown, no code fences."""


async def derive_profile_from_label(
    llm: LLMClient,
    *,
    label: str,
    model: ModelId = DEFAULT_MODEL,
    purpose: str = DEFAULT_PURPOSE,
) -> tuple[DerivedTarget, LLMResult]:
    """Derive a ScoringProfile + search keywords from a target label.

    The profile is grounded in role-generic industry knowledge of the label
    alone (#5 layer 1) — never an individual's résumé — so a shared target's
    rubric isn't skewed by whoever activated it. The résumé feeds ``fit_score``
    separately. Returns (derived, result) so callers can log cost.
    """
    return await complete_json(
        llm,
        model=model,
        system=SYSTEM_PROMPT_GENERIC,
        messages=[Message(role="user", content=f"Target role: {label}")],
        schema=DerivedTarget,
        purpose=purpose,
        cache_system=True,
    )
