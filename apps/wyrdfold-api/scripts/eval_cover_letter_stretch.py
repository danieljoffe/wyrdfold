"""Eval: does the stretch opt-in stop the model declining on the user's behalf?

Covers ``COVER_LETTER_STRETCH_ADDENDUM`` (``app/services/tailor/prompts.py``),
added because a paid generation on a poor-fit job returned a refusal:

    "I am a full-stack engineer, not a UX designer. I am not applying for this
     role. I am flagging this mismatch..."

Honest, but the user had paid for a document and had no way to say "I know it's
a reach — write it anyway". `eval_cover_letter.py` answers a different question
(model bake-off), so this prompt had no eval covering the behaviour it changes.
CONTRIBUTING: "If no eval covers the prompt you are editing, that gap is the
first thing to fix."

What it measures, on a deliberately poor-fit (payload x JD) pair:

  refusal_rate   — control (no opt-in) vs stretch (opt-in). Stretch must be 0.
  invented_rate  — skills named in the letter that appear nowhere in the
                   candidate's OptimizedPayload. Must stay 0 in BOTH arms:
                   the point is a stronger honest letter, not a fabricated one.

Repeats because generation is nondeterministic even at temperature 0 — a single
sample per arm would measure noise. Temperature is pinned to 0.0 to match
production (`complete_json` forwards 0.0); leaving it unset measures a noisier
distribution than the thing under test.

Usage::

    cd apps/wyrdfold-api
    zsh -c 'source ~/.zshrc && uv run python scripts/eval_cover_letter_stretch.py'
    ... --repeats 5      # default 3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.models.experience import OptimizedPayload, Outcome, Role, Skill
from app.models.tailor import ContactInfo
from app.services.tailor.prompts import cover_letter_system
from app.services.tailor.tailor import build_cover_letter_user_message
from scripts._openrouter import MODELS, call_model, get_api_key

# Self-contained, deliberately poor-fit pair. NOT read from
# ``tests/fixtures/eval_set.json``: that file is gitignored (it carries a real
# profile), so an eval built on it can't be reproduced by anyone else. This
# mirrors the exact prod case that produced the refusal — a full-stack
# engineer's payload against a Senior UX Designer JD — and stays in the repo.
_PAYLOAD = OptimizedPayload(
    summary=(
        "Full-stack engineer and founder with 10+ years building production web "
        "platforms, with a strong track record in accessibility-first development, "
        "component library architecture, and shipping end-to-end features."
    ),
    roles=[
        Role(
            id="role-1",
            company="Wyrdfold",
            title="Founder & Full-Stack Engineer",
            start="2026-03",
            end=None,
            summary="Built and operate a production job-matching platform solo.",
            skills=["React", "TypeScript", "Python", "FastAPI", "PostgreSQL"],
            outcome_refs=[],
        ),
        Role(
            id="role-2",
            company="The Library Corporation",
            title="Software Engineer",
            start="2019-09",
            end="2021-11",
            summary="Sole frontend engineer on a platform serving 5,500 library clients.",
            skills=["React", "WCAG Accessibility", "JavaScript"],
            outcome_refs=[],
        ),
    ],
    skills=[
        Skill(name="React", evidence_refs=["role-1"], years=8),
        Skill(name="TypeScript", evidence_refs=["role-1"], years=7),
        Skill(name="Python", evidence_refs=["role-1"], years=5),
        Skill(name="WCAG Accessibility", evidence_refs=["role-2"], years=4),
        Skill(name="Component library architecture", evidence_refs=["role-2"], years=5),
    ],
    outcomes=[
        Outcome(
            description=(
                "Resolved 200 WCAG accessibility violations as sole frontend engineer "
                "on a platform serving 5,500 library clients."
            ),
            metric="violations resolved",
            value="200",
            role_ref="role-2",
        ),
        Outcome(
            description=(
                "Built a centralized React component library adopted by 80% of company "
                "applications, growing from 12 to 30 documented components."
            ),
            metric="adoption",
            value="80%",
            role_ref="role-1",
        ),
    ],
)

_JD = """Senior UX Designer — Product Design & Research

We're hiring a Senior UX Designer to own product areas end-to-end. This is a
hands-on, high-craft role for someone who can take a feature from ambiguity to
production with minimal oversight.

What You'll Do
- Own product design end-to-end, from discovery and wireframes through
  high-fidelity UI and production-ready handoff.
- Contribute to and strengthen the design system.
- Integrate UX research into your work: run usability tests, synthesize
  behavioral and product analytics.

Required Qualifications
- 6+ years of product design experience, with a portfolio showing end-to-end
  ownership of complex features.
- Strong fluency in Figma and modern design tooling.
- Hands-on experience integrating usability testing and product analytics.
- Working knowledge of accessibility standards (WCAG 2.2).
"""

_CONTACT = ContactInfo(
    name="Daniel Joffe",
    email="hello@example.com",
    phone_number=None,
    location="Los Angeles, CA",
    linkedin_url=None,
    website_url=None,
)

# Phrasings that mean "I am declining to apply on your behalf". Deliberately
# narrow: an honest "my background is in X rather than Y" is fine and expected
# in a stretch letter — what must not appear is a withdrawal.
_REFUSAL_PATTERNS = [
    r"\bnot applying\b",
    r"\bam not (?:a |an )?(?:qualified|the right)\b",
    r"\bnot qualified for\b",
    r"\bI (?:should|will) not\b",
    r"\bdeclin(?:e|ing) to apply\b",
    r"\bwithdraw(?:ing)? (?:my|this) application\b",
    r"\bthis (?:role|position) is not a (?:fit|match)\b",
    r"\bflagging this mismatch\b",
]


def _letter_text(parsed: dict[str, Any] | None, raw: str) -> str:
    if not parsed:
        return raw
    parts = [str(parsed.get(k, "")) for k in ("salutation", "closing", "signature")]
    body = parsed.get("body_paragraphs") or parsed.get("paragraphs") or []
    if isinstance(body, list):
        parts.extend(str(p) for p in body)
    for key in ("opening", "body", "content"):
        if isinstance(parsed.get(key), str):
            parts.append(parsed[key])
    return "\n".join(p for p in parts if p)


def _refuses(text: str) -> bool:
    low = text.lower()
    return any(re.search(p, low) for p in _REFUSAL_PATTERNS)


# Design-domain competencies a full-stack payload cannot support. Presence of
# the term is only a *candidate* — see ``_invented_skills`` for why.
_DESIGN_TERMS = re.compile(
    r"\b(?:Figma|Sketch|InVision|Photoshop|Illustrator|wireframes?|"
    r"user research|usability testing|design portfolio)\b",
    re.I,
)

# "I possess this" constructions.
_POSSESSION = re.compile(
    r"\b(?:my|i have|i've|i bring|i own|i led|i built|i designed|i prototyped|"
    r"i wireframed|i conducted|i ran|i performed|i delivered|i produced|i created|"
    r"i shipped|i drove|years? of|proficien\w+ (?:in|with)|fluen\w+ (?:in|with)|"
    r"expert\w* (?:in|with)|skilled (?:in|with)|experience (?:in|with)|"
    r"background (?:in|with)|hands-on)\b",
    re.I,
)

# Anything that turns the sentence into a concession rather than a claim.
_DISCLAIMER = re.compile(
    r"\b(?:no|not|never|without|lack\w*|gap|rather than|instead of|cannot|"
    r"don'?t|doesn'?t|can'?t|isn'?t|aren'?t|wasn'?t|weren'?t|"
    r"reach|eager|welcome|open to|learn\w*|would like|standard bar|"
    r"even if|even though|although|though|while|unlike|absent)\b",
    re.I,
)


def _invented_skills(text: str, payload: OptimizedPayload) -> list[str]:
    """Design competencies the letter *claims to possess* but the payload lacks.

    Crude but directional — the real containment is the prompt's ref-tracing
    plus ``validate_cover_letter_refs``; this exists so a stretch letter that
    starts inventing domain experience shows up as a number.

    Scoring a bare *mention* would be actively wrong: the behaviour this eval
    wants is a letter that names the gap out loud ("my artifacts were code
    rather than Figma files"), and a mention-counter marks exactly that as
    invention — rewarding a letter that stays silent about the gap over an
    honest one. So a term counts only inside a sentence that asserts
    possession and does not concede. Residual: a concession word anywhere in
    the sentence neutralizes it ("I have 6 years of Figma, not 2" scores
    clean), which is why this is a directional signal and not the containment.
    """
    source = json.dumps(payload.model_dump(), default=str).lower()
    invented: set[str] = set()
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        terms = {m.group(0) for m in _DESIGN_TERMS.finditer(sentence)}
        terms = {t for t in terms if t.lower() not in source}
        if not terms:
            continue
        if _POSSESSION.search(sentence) and not _DISCLAIMER.search(sentence):
            invented.update(terms)
    return sorted(invented)


async def _one(
    *, payload: OptimizedPayload, case: dict[str, Any], allow_stretch: bool, api_key: str
) -> dict[str, Any]:
    user_message = build_cover_letter_user_message(
        optimized=payload,
        job_description=case.get("jd_text", ""),
        company_name=case.get("company_name") or "Acme Co",
        contact=_CONTACT,
        role_title=case.get("title"),
        preferences_text=None,
        annotations_text=None,
        critique=None,
    )
    res = await call_model(
        model_slug=MODELS["sonnet-4.6"],
        system=cover_letter_system(allow_stretch=allow_stretch),
        user=user_message,
        api_key=api_key,
        max_tokens=2048,
        response_format_json=True,
        temperature=0.0,  # match production (complete_json forwards 0.0)
    )
    text = _letter_text(res.parsed, res.raw_content)
    return {
        "error": res.error,
        "refused": _refuses(text),
        "invented": _invented_skills(text, payload),
        "chars": len(text),
        "cost": res.cost_usd,
        "text": text,
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--show", action="store_true", help="print the letters")
    args = ap.parse_args()

    api_key = get_api_key()
    payload = _PAYLOAD
    case = {
        "jd_text": _JD,
        "company_name": "SecurityScorecard",
        "title": "Senior UX Designer",
    }

    print(
        "Poor-fit pair: full-stack engineer payload x 'Senior UX Designer' JD\n"
        f"repeats={args.repeats}, temperature=0.0, model=sonnet-4.6\n"
    )

    results: dict[str, list[dict[str, Any]]] = {}
    total_cost = 0.0
    for arm, allow in (("control", False), ("stretch", True)):
        runs = await asyncio.gather(
            *(
                _one(payload=payload, case=case, allow_stretch=allow, api_key=api_key)
                for _ in range(args.repeats)
            )
        )
        results[arm] = list(runs)
        total_cost += sum(r["cost"] for r in runs)

    print(f"{'arm':<10} {'refusal':>9} {'invented':>9} {'avg chars':>10}")
    for arm, runs in results.items():
        refusals = sum(1 for r in runs if r["refused"])
        invented = sum(1 for r in runs if r["invented"])
        avg = sum(r["chars"] for r in runs) / max(1, len(runs))
        print(f"{arm:<10} {refusals}/{len(runs):<7} {invented}/{len(runs):<7} {avg:>10.0f}")

    print(f"\ntotal cost: ${total_cost:.4f}")

    # A run where every call errored used to print a clean PASS: zero refusals
    # out of zero letters. Fail loudly instead — an eval that cannot fail is
    # worse than no eval.
    errors = [r["error"] for runs in results.values() for r in runs if r["error"]]
    empty = [r for runs in results.values() for r in runs if r["chars"] == 0]
    if errors or empty:
        print(f"\nFAIL — {len(errors)} call error(s), {len(empty)} empty letter(s)")
        if errors:
            print(f"first error: {errors[0]}")
        raise SystemExit(1)

    if args.show:
        for arm, runs in results.items():
            for i, r in enumerate(runs):
                print(f"\n===== {arm} #{i} (refused={r['refused']}) =====\n{r['text']}")

    stretch_refusals = sum(1 for r in results["stretch"] if r["refused"])
    stretch_invented = sum(1 for r in results["stretch"] if r["invented"])
    print(
        "\nPASS"
        if stretch_refusals == 0 and stretch_invented == 0
        else "\nFAIL — stretch arm must never refuse and never invent"
    )


if __name__ == "__main__":
    asyncio.run(main())
