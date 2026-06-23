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
import re

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

MAX_RESUME_SKILLS = 20
"""Hard cap on the skills section — the most ATS-scanned, easiest-to-pad
region. The prompt asks for a focused list; we enforce it in code so a
runaway LLM list can't pad the resume past what a human would write."""

# A "meaningful clause" the LLM may legitimately echo out of a role summary.
# A one-word ref like 'team' or 'React' is a substring of almost any summary,
# so it isn't real traceability — we require a multi-word clause of at least
# this many characters (or the ref reproducing the whole summary).
MIN_SUMMARY_CLAUSE_LEN = 12


# ---------------------------------------------------------------------------
# Claim-level faithfulness (#47)
#
# The ref pointer telling us *which* source item a line draws from is checked
# elsewhere; these helpers check the shipped TEXT itself, so the LLM can't
# cite a real outcome and still fabricate the numbers/skills/prose it ships.
# The product promise is "traced to your experience, never hallucinated" —
# that has to hold at the claim level, not just the pointer level.
# ---------------------------------------------------------------------------

# A numeric token: an integer/decimal with optional thousands separators and a
# trailing unit-ish suffix (%, x, k, m, etc.). We compare on the *digits* only,
# so "40%" in shipped text is supported by "40 percent" or "$40k ARR" in source.
_NUMERIC_TOKEN_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numeric_tokens(text: str) -> list[str]:
    """Digit-cores of every number in ``text``.

    Thousands separators are stripped and a trailing ``.0`` is normalized so
    "1,200" and "1200" and "1200.0" all collapse to "1200". Years and other
    bare integers count too — a fabricated "2019" is exactly the kind of
    invented fact this is meant to catch.
    """
    tokens: list[str] = []
    for raw in _NUMERIC_TOKEN_RE.findall(text):
        core = raw.replace(",", "")
        if "." in core:
            core = core.rstrip("0").rstrip(".")
        if core:
            tokens.append(core)
    return tokens


def _numeric_source_corpus(*parts: str | None) -> set[str]:
    """The set of digit-cores that appear anywhere in the given source text."""
    corpus: set[str] = set()
    for part in parts:
        if part:
            corpus.update(_numeric_tokens(part))
    return corpus


def _unsupported_numbers(text: str, source_corpus: set[str]) -> list[str]:
    """Numbers in ``text`` whose digit-core is absent from ``source_corpus``.

    Returns the raw tokens (deduped, order-preserving) so the warning names
    what the user actually sees, not the normalized core.
    """
    unsupported: list[str] = []
    seen: set[str] = set()
    for raw in _NUMERIC_TOKEN_RE.findall(text):
        core = raw.replace(",", "")
        if "." in core:
            core = core.rstrip("0").rstrip(".")
        if not core or core in source_corpus or raw in seen:
            continue
        seen.add(raw)
        unsupported.append(raw)
    return unsupported


def _normalize_skill(name: str) -> str:
    """Case- and plural-tolerant normalization for skill matching.

    Shared with the cover-letter path: the LLM frequently emits
    "Component Libraries" when the canonical name is "Component Library", or
    pluralizes/capitalizes inconsistently. Map normalized -> canonical so we
    keep the canonical form and don't drop a skill that *is* in the doc.
    """
    stripped = name.strip().lower()
    if stripped.endswith("ies"):
        return stripped[:-3] + "y"
    if stripped.endswith("s") and not stripped.endswith("ss"):
        return stripped[:-1]
    return stripped


def _skill_canonical_map(optimized: OptimizedPayload) -> dict[str, str]:
    """normalized skill -> canonical Skill.name from the OptimizedPayload."""
    return {_normalize_skill(s.name): s.name for s in optimized.skills}


def _traces_to_summary(ref: str, summary: str) -> bool:
    """A bullet ref echoed out of a role summary is only traceable if it is a
    *meaningful* clause, not a trivial substring.

    The old check (``ref in summary``) let a one-word ref like 'team' or
    'React' pass, since almost anything is a substring of a sentence. We
    require a multi-word clause of meaningful length (or that the ref
    reproduces the whole summary). The exact ``Outcome.description`` match
    path is unaffected; this only guards the looser summary-substring path.
    """
    if not summary:
        return False
    ref = ref.strip()
    # A short summary echoed (near-)verbatim is genuinely traceable.
    if ref.lower() == summary.strip().lower():
        return True
    if ref not in summary:
        return False
    # Otherwise require a real clause: multiple words AND meaningful length, so
    # a trivial single-word substring ('team', 'React') doesn't qualify.
    is_multiword = len(ref.split()) >= 2
    return is_multiword and len(ref) >= MIN_SUMMARY_CLAUSE_LEN


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

    Claim-level checks (#47), beyond the ref pointer:

    4. Drop a bullet whose text ships a number absent from the source it
       traces to (outcome description/metric/value + role summary) — a
       fabricated metric on a real ref.
    5. Validate ``resume.skills`` against ``optimized.skills`` (canonical,
       case/plural-tolerant); drop unknown skills and enforce the skills cap.
    6. Warn (don't strip) when ``resume.summary`` ships a number absent from
       source — the summary is required text, so we surface rather than drop.

    Returns the repaired resume + warnings about every correction/drop.
    """
    roles_by_id = {r.id: r for r in optimized.roles}
    outcomes_by_desc = {o.description: o for o in optimized.outcomes}
    outcome_descriptions = set(outcomes_by_desc)
    # Role summaries are also valid bullet sources — the prompt allows
    # "a literal clause from role.summary". We accept a *meaningful* clause
    # echoed out of the summary (see ``_traces_to_summary``); a trivial
    # one-word substring is not real traceability.
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
        summary_for_role = role_summaries.get(role.source_role_ref, "")
        kept_bullets: list[TailoredBullet] = []
        for bullet in role.bullets:
            ref = (bullet.source_outcome_ref or "").strip()
            if not ref:
                warnings.append(f"Dropped bullet with no ref: {bullet.text[:80]!r}")
                continue
            traces_to_outcome = ref in outcome_descriptions
            traces_to_summary = _traces_to_summary(ref, summary_for_role)
            if not (traces_to_outcome or traces_to_summary):
                warnings.append(
                    f"Dropped bullet with untraceable ref {ref[:60]!r}: "
                    f"{bullet.text[:80]!r}"
                )
                continue
            outcome = outcomes_by_desc.get(ref) if traces_to_outcome else None
            if traces_to_outcome:
                owner = optimized.owner_role_id(outcome) if outcome else None
                if owner is not None and owner != role.source_role_ref:
                    warnings.append(
                        "Dropped bullet attributed to the wrong employer "
                        f"(outcome belongs to role {owner!r}, placed "
                        f"under {role.source_role_ref!r}): {bullet.text[:80]!r}"
                    )
                    continue
            # Claim check (#47): the bullet may cite a real outcome but still
            # invent the numbers in its text. Every numeric token the bullet
            # ships must appear in the source it traces to — the outcome
            # (description/metric/value) plus the role summary. A bullet whose
            # numbers can't be grounded is a fabricated metric; drop it (same
            # disposition as an untraceable ref) and warn.
            source_corpus = _numeric_source_corpus(
                outcome.description if outcome else None,
                outcome.metric if outcome else None,
                outcome.value if outcome else None,
                ref,
                summary_for_role,
            )
            unsupported = _unsupported_numbers(bullet.text, source_corpus)
            if unsupported:
                warnings.append(
                    "Dropped bullet with fabricated number(s) "
                    f"{unsupported} not in source: {bullet.text[:80]!r}"
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

    # Skills (#47): the skills section ships with zero source validation today,
    # yet it's the most ATS-scanned, easiest-to-over-claim region. Validate each
    # against ``optimized.skills`` using the same case/plural-tolerant canonical
    # map the cover-letter path uses; drop unknown skills (fabricated expertise)
    # and warn. Then enforce the hard cap in code so a runaway list can't pad it.
    canonical_map = _skill_canonical_map(optimized)
    kept_skills: list[str] = []
    seen_skills: set[str] = set()
    for skill in resume.skills:
        canonical = canonical_map.get(_normalize_skill(skill))
        if canonical is None:
            warnings.append(f"Dropped unknown skill (not in source): {skill!r}")
            continue
        if canonical in seen_skills:
            continue
        seen_skills.add(canonical)
        kept_skills.append(canonical)
    if len(kept_skills) > MAX_RESUME_SKILLS:
        warnings.append(
            f"Trimmed skills from {len(kept_skills)} to the {MAX_RESUME_SKILLS} cap"
        )
        kept_skills = kept_skills[:MAX_RESUME_SKILLS]

    # Summary (#47): the summary ships verbatim and is min_length=1, so we
    # cannot drop it without breaking the resume. Numbers it invents are the
    # most damaging claim of all, but auto-editing prose risks mangling
    # legitimate copy. Policy: WARN (don't strip) and surface so the user sees
    # the unsupported number before they send the resume.
    summary_corpus = _numeric_source_corpus(
        optimized.summary,
        *(o.description for o in optimized.outcomes),
        *(o.metric for o in optimized.outcomes),
        *(o.value for o in optimized.outcomes),
        *(r.summary for r in optimized.roles),
    )
    unsupported_summary = _unsupported_numbers(resume.summary, summary_corpus)
    if unsupported_summary:
        warnings.append(
            "Resume summary contains number(s) not found in source: "
            f"{unsupported_summary} — verify before sending: "
            f"{resume.summary[:80]!r}"
        )

    return (
        resume.model_copy(
            update={"experience": cleaned_experience, "skills": kept_skills}
        ),
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

    Beyond the ref lists, we also inspect the paragraph *text* (#47): any
    numeric token in the prose that isn't grounded in the source corpus is
    surfaced as a warning (WARN, not strip — prose can't be safely
    auto-edited). Callers should surface warnings to the user.
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
    # canonical form in the response. (Shared with the resume path.)
    skill_normalized_to_canonical = _skill_canonical_map(optimized)

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
        elif (canonical := skill_normalized_to_canonical.get(_normalize_skill(ref))):
            kept_skill_refs.append(canonical)
        else:
            warnings.append(f"Dropped unknown skill_ref: {ref!r}")

    # Attribution consistency (#87). A cover letter is free prose, so we
    # can't pin a company per sentence the way the resume validator does.
    # But every outcome the letter draws on has an owning role, and that
    # role's company is the only employer the accomplishment may be credited
    # to. If the letter cites an outcome whose owning role it never declared,
    # that's the fingerprint of a cross-employer misattribution (an Internet
    # Brands accomplishment narrated under HubSpot). We fold the true owner
    # into the role refs so the audit trail names the right employer, and
    # warn so the prose gets a human check.
    outcomes_by_desc = {o.description: o for o in optimized.outcomes}
    declared_roles = set(kept_role_refs)
    for ref in kept_outcome_refs:
        outcome = outcomes_by_desc.get(ref)
        if outcome is None:
            continue
        owner = optimized.owner_role_id(outcome)
        if owner in valid_role_ids and owner not in declared_roles:
            kept_role_refs.append(owner)
            declared_roles.add(owner)
            warnings.append(
                f"Outcome {ref[:50]!r} is owned by role {owner!r}, which the "
                "letter did not credit — added to role refs; verify the prose "
                "attributes it to the correct employer"
            )

    # Prose claim check (#47). The ref lists above are validated, but the
    # validator's own contract concedes "the prose itself is what the user
    # ships" — and that prose was never inspected, so a letter could narrate a
    # fabricated number with a perfectly clean ref set. Check every numeric
    # token across the paragraph text against the source corpus. Policy: WARN,
    # not strip — cover-letter prose can't be safely auto-edited without
    # mangling legitimate copy, so we surface the unsupported number and let
    # the user fix it before they send.
    prose_corpus = _numeric_source_corpus(
        optimized.summary,
        *(o.description for o in optimized.outcomes),
        *(o.metric for o in optimized.outcomes),
        *(o.value for o in optimized.outcomes),
        *(r.summary for r in optimized.roles),
    )
    for i, para in enumerate(letter.paragraphs):
        unsupported = _unsupported_numbers(para.text, prose_corpus)
        if unsupported:
            warnings.append(
                f"Cover-letter paragraph {i + 1} contains number(s) not found "
                f"in source: {unsupported} — verify before sending: "
                f"{para.text[:80]!r}"
            )

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
