"""Faithfulness review pass (#6b).

A second LLM "review" over a freshly generated resume: it flags claims that
the candidate's SOURCE experience does not support — fabrication, exaggeration,
or an unsupported skill. ``run_tailor_pipeline`` gates this behind
``faithfulness_review_enabled`` and, when actionable flags exist, regenerates
ONCE with the flags folded in as a critique. The corrective run is **not**
re-reviewed — a single generate -> review -> fix cycle, no loop.

Complements the deterministic ``validate_trace_refs`` (which pins every bullet
to a source ``Role.id``): tracing proves a bullet *came from* a real role; this
catches semantic unfaithfulness *within* a traceable bullet — an inflated
outcome, a metric the source never stated, a skill it never mentions.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.models.experience import OptimizedPayload
from app.models.llm import LLMResult, Message, ModelId
from app.models.tailor import TailoredResume
from app.services.llm.client import LLMClient, complete_json
from app.services.tailor.markdown_render import to_markdown
from app.services.tailor.tailor import DEFAULT_MODEL

FAITHFULNESS_REVIEW_PURPOSE = "tailor.faithfulness_review"

IssueType = Literal["fabrication", "exaggeration", "unsupported_skill"]
Severity = Literal["low", "medium", "high"]


class FaithfulnessFlag(BaseModel):
    """One claim in the resume the source doesn't support."""

    claim: str = Field(
        description="The resume claim, quoted verbatim, that the source doesn't support."
    )
    issue: IssueType
    severity: Severity
    suggestion: str = Field(
        description="How to fix it — usually soften to what the source supports, or remove."
    )


class FaithfulnessReview(BaseModel):
    flags: list[FaithfulnessFlag] = Field(default_factory=list)

    def actionable_flags(self) -> list[FaithfulnessFlag]:
        """Flags worth a corrective regen — ``medium``/``high`` severity only,
        so a trivial wording nit never triggers a (costly) regeneration."""
        return [f for f in self.flags if f.severity in ("medium", "high")]


FAITHFULNESS_REVIEW_SYSTEM = """You review a tailored resume for FAITHFULNESS \
to the candidate's real experience.

You are given the candidate's SOURCE experience and a TAILORED resume generated \
from it. Flag every claim in the resume the source does NOT support:
- fabrication: an invented employer, role, date, metric, or outcome not in the source.
- exaggeration: a real item inflated beyond the source (bigger scope/impact, or \
a metric the source never states).
- unsupported_skill: a skill or technology claimed that the source never mentions.

Rules:
- Flag only GENUINE unfaithfulness. Reasonable rephrasing, summarizing, or \
emphasis of a real source item is NOT a flag.
- Quote the resume claim verbatim in `claim`.
- severity: high = clearly false / would not survive an interview; medium = \
inflated but arguable; low = minor.
- suggestion: how to fix it — soften to what the source supports, or remove.
- If everything is faithful, return an empty `flags` list."""


def _review_user_message(
    resume: TailoredResume, optimized: OptimizedPayload
) -> str:
    return (
        "[SOURCE experience]\n"
        + optimized.model_dump_json(indent=2)
        + "\n\n[TAILORED resume]\n"
        + to_markdown(resume)
    )


async def review_resume_faithfulness(
    llm: LLMClient,
    *,
    resume: TailoredResume,
    optimized: OptimizedPayload,
    model: ModelId = DEFAULT_MODEL,
    purpose: str = FAITHFULNESS_REVIEW_PURPOSE,
) -> tuple[FaithfulnessReview, LLMResult]:
    """Run the faithfulness review. Returns ``(review, llm_result)`` so the
    caller cost-logs the spend. Deterministic (``complete_json`` pins temp 0)."""
    return await complete_json(
        llm,
        model=model,
        system=FAITHFULNESS_REVIEW_SYSTEM,
        messages=[
            Message(content=_review_user_message(resume, optimized), role="user")
        ],
        schema=FaithfulnessReview,
        purpose=purpose,
        cache_system=True,
    )


def review_to_critique(review: FaithfulnessReview) -> str | None:
    """Render the actionable flags as a critique for ONE corrective regen.
    Returns ``None`` when nothing is actionable (so the caller skips the regen).
    """
    flags = review.actionable_flags()
    if not flags:
        return None
    lines = [
        "FAITHFULNESS FIXES — correct these without inventing anything; "
        "use ONLY the source experience:"
    ]
    for f in flags:
        lines.append(f"- [{f.issue}] {f.claim!r} — {f.suggestion}")
    return "\n".join(lines)
