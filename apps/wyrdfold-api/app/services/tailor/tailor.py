"""Tailor a resume from an OptimizedPayload + JD via LLM.

Pure function. No DB. No docx. No retrieval (the optimized doc is small
enough to pass in full). Cost logging happens at the router layer, which
also persists the result to `documents` and kicks off rendering.

Post-generation, `validate_trace_refs()` checks that every role and
bullet carries a source ref that actually exists in the OptimizedPayload.
Bullets that fail the check are dropped; unmatched roles raise.
"""

from __future__ import annotations

import json

from app.models.experience import OptimizedPayload
from app.models.llm import LLMResult, Message, ModelId
from app.models.tailor import (
    ContactInfo,
    ResumeType,
    TailoredBullet,
    TailoredCoverLetter,
    TailoredResume,
    TailoredRole,
)
from app.services.llm.client import LLMClient, complete_json
from app.services.tailor.prompts import COVER_LETTER_SYSTEM, TAILOR_SYSTEM

DEFAULT_MODEL: ModelId = "claude-sonnet-4-6"
DEFAULT_PURPOSE = "tailor.resume"
DEFAULT_COVER_LETTER_PURPOSE = "tailor.cover_letter"


def _optimized_section(optimized: OptimizedPayload) -> str:
    """The ``[OptimizedPayload]`` section — the user's master experience doc.

    It's the largest chunk of the user turn and is byte-identical across every
    tailor/cover-letter call in a session, so both builders emit it *first* and
    the callers set a ``cache_prefix_chars`` breakpoint at its end (#73). That
    caches the heaviest repeated prefix; only the trailing job-specific content
    is re-billed at full input price on a cache hit.
    """
    return f"[OptimizedPayload]\n{optimized.model_dump_json(indent=2)}"


def build_user_message(
    *,
    optimized: OptimizedPayload,
    job_description: str,
    contact: ContactInfo,
    resume_type: ResumeType,
    preferences_text: str | None,
    annotations_text: str | None,
    critique: str | None,
    page_budget: int,
) -> str:
    """Assemble the variable content for the LLM call.

    The system prompt is static (cache target); everything that changes
    per call lives here.
    """
    sections: list[str] = []
    sections.append(_optimized_section(optimized))
    sections.append(f"[ContactInfo]\n{contact.model_dump_json(indent=2)}")
    sections.append(f"[ResumeType] {resume_type}")
    sections.append(f"[PageBudget] {page_budget}")
    if preferences_text:
        sections.append(f"[Preferences]\n{preferences_text}")
    if annotations_text:
        sections.append(f"[Annotations]\n{annotations_text}")
    if critique:
        sections.append(f"[Critique]\n{critique}")
    sections.append(f"[JobDescription]\n{job_description}")
    return "\n\n".join(sections)


def validate_trace_refs(
    resume: TailoredResume,
    optimized: OptimizedPayload,
) -> tuple[TailoredResume, list[str]]:
    """Repair and validate a tailored resume against the OptimizedPayload.

    Every pass is keyed off the source ``Role.id`` the LLM must echo as
    ``source_role_ref``:

    1. Unknown ``source_role_ref`` -> raise (the role is untraceable).
    2. Pin ``company``/``start``/``end`` from the source Role. These are
       facts, not tailored copy — trusting the LLM's free text let it
       label a role with a company the user never worked at and invent
       overlapping dates (#87). Pinning makes the displayed employer and
       timeline always match the stored profile; a changed company is
       surfaced as a warning.
    3. Drop a bullet whose source outcome belongs to a *different* role
       (``Outcome.role_ref`` != this role's ref) — a cross-employer
       accomplishment leak (#87). Bullets that don't trace to any
       outcome or the role summary are dropped as before.

    Returns the repaired resume + warnings about every correction/drop.
    """
    roles_by_id = {r.id: r for r in optimized.roles}
    outcomes_by_desc = {o.description: o for o in optimized.outcomes}
    outcome_descriptions = set(outcomes_by_desc)
    # Role summaries are also valid bullet sources — the prompt allows
    # "a literal clause from role.summary". We accept any non-empty summary
    # string as a valid ref (LLM can echo a sentence fragment).
    role_summaries: dict[str, str] = {
        r.id: r.summary or "" for r in optimized.roles
    }

    warnings: list[str] = []
    cleaned_experience: list[TailoredRole] = []

    for role in resume.experience:
        source = roles_by_id.get(role.source_role_ref)
        if source is None:
            raise ValueError(
                f"TailoredRole references unknown role id: {role.source_role_ref!r}"
            )
        kept_bullets: list[TailoredBullet] = []
        for bullet in role.bullets:
            ref = (bullet.source_outcome_ref or "").strip()
            if not ref:
                warnings.append(f"Dropped bullet with no ref: {bullet.text[:80]!r}")
                continue
            summary_for_role = role_summaries.get(role.source_role_ref, "")
            traces_to_outcome = ref in outcome_descriptions
            traces_to_summary = bool(summary_for_role) and ref in summary_for_role
            if not (traces_to_outcome or traces_to_summary):
                warnings.append(
                    f"Dropped bullet with untraceable ref {ref[:60]!r}: "
                    f"{bullet.text[:80]!r}"
                )
                continue
            if traces_to_outcome:
                outcome = outcomes_by_desc.get(ref)
                if (
                    outcome is not None
                    and outcome.role_ref
                    and outcome.role_ref != role.source_role_ref
                ):
                    warnings.append(
                        "Dropped bullet attributed to the wrong employer "
                        f"(outcome belongs to role {outcome.role_ref!r}, placed "
                        f"under {role.source_role_ref!r}): {bullet.text[:80]!r}"
                    )
                    continue
            kept_bullets.append(bullet)

        if role.company != source.company:
            warnings.append(
                f"Corrected employer {role.company!r} -> {source.company!r} "
                f"for role {source.id!r}"
            )
        # Company + dates are authoritative from the source Role, never the
        # LLM's free text. Title is left to the tailoring step.
        cleaned_role = role.model_copy(
            update={
                "bullets": kept_bullets,
                "company": source.company,
                "start": source.start,
                "end": source.end,
            }
        )
        cleaned_experience.append(cleaned_role)

    return (
        resume.model_copy(update={"experience": cleaned_experience}),
        warnings,
    )


def _preferences_text(
    rules: list[str] | None,
    avoid: list[str] | None,
    tone_notes: list[str] | None,
) -> str | None:
    if not rules and not avoid and not tone_notes:
        return None
    payload = {
        "rules": rules or [],
        "avoid": avoid or [],
        "tone_notes": tone_notes or [],
    }
    return json.dumps(payload, indent=2)


async def tailor_resume(
    llm: LLMClient,
    *,
    optimized: OptimizedPayload,
    job_description: str,
    contact: ContactInfo,
    resume_type: ResumeType = "generic",
    preferences_rules: list[str] | None = None,
    preferences_avoid: list[str] | None = None,
    preferences_tone_notes: list[str] | None = None,
    annotations_text: str | None = None,
    critique: str | None = None,
    page_budget: int = 2,
    model: ModelId = DEFAULT_MODEL,
    purpose: str = DEFAULT_PURPOSE,
) -> tuple[TailoredResume, list[str], LLMResult]:
    """Run the LLM and post-validate traceability.

    Returns (resume, warnings, llm_result). Caller is responsible for
    cost-logging result + persisting resume.
    """
    user_message = build_user_message(
        optimized=optimized,
        job_description=job_description,
        contact=contact,
        resume_type=resume_type,
        preferences_text=_preferences_text(
            preferences_rules, preferences_avoid, preferences_tone_notes
        ),
        annotations_text=annotations_text,
        critique=critique,
        page_budget=page_budget,
    )

    parsed, result = await complete_json(
        llm,
        model=model,
        system=TAILOR_SYSTEM,
        messages=[
            Message(
                role="user",
                content=user_message,
                cache_prefix_chars=len(_optimized_section(optimized)),
            )
        ],
        schema=TailoredResume,
        purpose=purpose,
        cache_system=True,
        # Cut from 8192 to 4096: typical tailored resumes are 1500-3500
        # output tokens; the 8192 cap was a worst-case ceiling that we
        # never hit. At Sonnet output $15/Mtok, full-fill would have been
        # $0.123/call just for output. 4096 keeps real headroom for the
        # rare long-spec resume while halving the worst-case spend. See
        # plan-wyrdfold-openrouter-investigation.md Recommendation C.
        # Reassess by querying llm_costs output_tokens p95 after ~2
        # weeks of production data.
        max_tokens=4096,
    )

    cleaned, warnings = validate_trace_refs(parsed, optimized)
    return cleaned, warnings, result


# ---------------------------------------------------------------------------
# Cover letters
# ---------------------------------------------------------------------------


def build_cover_letter_user_message(
    *,
    optimized: OptimizedPayload,
    job_description: str,
    company_name: str,
    contact: ContactInfo,
    role_title: str | None,
    preferences_text: str | None,
    annotations_text: str | None,
    critique: str | None,
) -> str:
    """Assemble the variable content for the LLM call.

    The system prompt is static (cache target); everything that changes
    per call lives here.
    """
    sections: list[str] = []
    sections.append(_optimized_section(optimized))
    sections.append(f"[ContactInfo]\n{contact.model_dump_json(indent=2)}")
    sections.append(f"[RecipientCompany] {company_name}")
    if role_title:
        sections.append(f"[RoleTitle] {role_title}")
    if preferences_text:
        sections.append(f"[Preferences]\n{preferences_text}")
    if annotations_text:
        sections.append(f"[Annotations]\n{annotations_text}")
    if critique:
        sections.append(f"[Critique]\n{critique}")
    sections.append(f"[JobDescription]\n{job_description}")
    return "\n\n".join(sections)


def validate_cover_letter_refs(
    letter: TailoredCoverLetter,
    optimized: OptimizedPayload,
) -> tuple[TailoredCoverLetter, list[str]]:
    """Drop refs that don't trace back to the OptimizedPayload. Returns
    the cleaned letter + warnings for each dropped ref.

    Unlike the resume trace check, cover letters reference refs in
    aggregate (not per-bullet). We drop invalid refs rather than raise
    because a cover letter's prose may still be salvageable even if one
    claimed ref is off — the prose itself is what the user ships.
    Callers should surface warnings to the user.
    """
    valid_role_ids = {r.id for r in optimized.roles}
    valid_outcome_descriptions = {o.description for o in optimized.outcomes}
    valid_skill_names = {s.name for s in optimized.skills}
    # Case- and pluralization-tolerant lookup: the LLM frequently emits
    # "Component Libraries" when the canonical name is "Component
    # Library", or pluralizes/capitalizes inconsistently. Without this
    # tolerance every cover letter ships with cosmetic ``Dropped unknown
    # skill_ref`` warnings even though the underlying skill is in the
    # optimized doc. Map normalized → canonical so we keep the
    # canonical form in the response.
    def _normalize(s: str) -> str:
        stripped = s.strip().lower()
        if stripped.endswith("ies"):
            return stripped[:-3] + "y"
        if stripped.endswith("s") and not stripped.endswith("ss"):
            return stripped[:-1]
        return stripped

    skill_normalized_to_canonical: dict[str, str] = {
        _normalize(name): name for name in valid_skill_names
    }

    warnings: list[str] = []

    kept_role_refs: list[str] = []
    for ref in letter.source_role_refs:
        if ref in valid_role_ids:
            kept_role_refs.append(ref)
        else:
            warnings.append(f"Dropped unknown role_ref: {ref!r}")

    kept_outcome_refs: list[str] = []
    for ref in letter.source_outcome_refs:
        if ref in valid_outcome_descriptions:
            kept_outcome_refs.append(ref)
        else:
            warnings.append(f"Dropped unknown outcome_ref: {ref[:60]!r}")

    kept_skill_refs: list[str] = []
    for ref in letter.source_skill_refs:
        if ref in valid_skill_names:
            kept_skill_refs.append(ref)
        elif (canonical := skill_normalized_to_canonical.get(_normalize(ref))):
            kept_skill_refs.append(canonical)
        else:
            warnings.append(f"Dropped unknown skill_ref: {ref!r}")

    cleaned = letter.model_copy(
        update={
            "source_role_refs": kept_role_refs,
            "source_outcome_refs": kept_outcome_refs,
            "source_skill_refs": kept_skill_refs,
        }
    )
    return cleaned, warnings


async def tailor_cover_letter(
    llm: LLMClient,
    *,
    optimized: OptimizedPayload,
    job_description: str,
    company_name: str,
    contact: ContactInfo,
    role_title: str | None = None,
    preferences_rules: list[str] | None = None,
    preferences_avoid: list[str] | None = None,
    preferences_tone_notes: list[str] | None = None,
    annotations_text: str | None = None,
    critique: str | None = None,
    model: ModelId = DEFAULT_MODEL,
    purpose: str = DEFAULT_COVER_LETTER_PURPOSE,
) -> tuple[TailoredCoverLetter, list[str], LLMResult]:
    """Run the LLM and post-validate that declared refs trace back.

    Returns (letter, warnings, llm_result). Caller is responsible for
    cost-logging and persistence.
    """
    user_message = build_cover_letter_user_message(
        optimized=optimized,
        job_description=job_description,
        company_name=company_name,
        contact=contact,
        role_title=role_title,
        preferences_text=_preferences_text(
            preferences_rules, preferences_avoid, preferences_tone_notes
        ),
        annotations_text=annotations_text,
        critique=critique,
    )

    parsed, result = await complete_json(
        llm,
        model=model,
        system=COVER_LETTER_SYSTEM,
        messages=[
            Message(
                role="user",
                content=user_message,
                cache_prefix_chars=len(_optimized_section(optimized)),
            )
        ],
        schema=TailoredCoverLetter,
        purpose=purpose,
        cache_system=True,
        max_tokens=4096,
    )

    cleaned, warnings = validate_cover_letter_refs(parsed, optimized)
    return cleaned, warnings, result
