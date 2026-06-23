"""Tailor service tests with MockLLMClient."""

import json

import pytest

from app.models.experience import OptimizedPayload, Outcome, Role, Skill
from app.models.tailor import (
    ContactInfo,
    TailoredBullet,
    TailoredCoverLetter,
    TailoredEducation,
    TailoredResume,
    TailoredRole,
)
from app.services.llm.mock import MockLLMClient
from app.services.tailor.tailor import (
    DEFAULT_PURPOSE,
    _optimized_section,
    build_user_message,
    tailor_resume,
    validate_cover_letter_refs,
    validate_trace_refs,
)


def _optimized() -> OptimizedPayload:
    return OptimizedPayload(
        summary="Senior frontend with a focus on performance and a11y.",
        roles=[
            Role(
                id="fc",
                company="FightCamp",
                title="Senior Frontend Engineer",
                start="2021-11",
                end="2024-04",
                summary="Led the PDP rebuild and cut mobile load times.",
                skills=["React", "Next.js"],
                outcome_refs=[],
            ),
            Role(
                id="winc",
                company="Winc",
                title="Frontend Engineer",
                start="2018-01",
                end="2021-10",
                summary="Built the self-serve landing page CMS.",
                skills=["Vue.js", "Nuxt.js"],
                outcome_refs=[],
            ),
        ],
        skills=[Skill(name="React"), Skill(name="TypeScript")],
        outcomes=[
            Outcome(
                description="Cut mobile load times from 10s to 2s",
                metric="LCP",
                value="2s",
                role_ref="fc",
            )
        ],
    )


def _contact() -> ContactInfo:
    return ContactInfo(name="Daniel Joffe", email="daniel@example.com")


def _valid_resume_json() -> str:
    return TailoredResume(
        summary="Senior FE with 10 years of shipped work. Focus on performance and design systems.",
        contact=_contact(),
        experience=[
            TailoredRole(
                company="FightCamp",
                title="Senior Frontend Engineer",
                start="2021-11",
                end="2024-04",
                bullets=[
                    TailoredBullet(
                        text="Cut mobile load times from 10s to 2s.",
                        source_outcome_ref="Cut mobile load times from 10s to 2s",
                    ),
                    TailoredBullet(
                        text="Led the PDP rebuild.",
                        source_outcome_ref="Led the PDP rebuild",
                    ),
                ],
                source_role_ref="fc",
            )
        ],
        skills=["React", "TypeScript"],
        education=[TailoredEducation(school="UCLA", degree="BA")],
        jd_snippet="Senior FE role",
    ).model_dump_json()


# ---- build_user_message ---------------------------------------------------


def test_build_user_message_contains_every_section() -> None:
    msg = build_user_message(
        optimized=_optimized(),
        job_description="We want a senior FE.",
        contact=_contact(),
        resume_type="senior-frontend",
        preferences_text='{"rules": ["present tense"]}',
        annotations_text="EMPHASIZE: role roadmap",
        critique="Lead with performance.",
        page_budget=2,
    )
    for tag in (
        "[OptimizedPayload]",
        "[ContactInfo]",
        "[ResumeType] senior-frontend",
        "[PageBudget] 2",
        "[Preferences]",
        "[Annotations]",
        "[Critique]",
        "[JobDescription]",
    ):
        assert tag in msg


def test_build_user_message_omits_preferences_and_critique_when_absent() -> None:
    msg = build_user_message(
        optimized=_optimized(),
        job_description="jd",
        contact=_contact(),
        resume_type="generic",
        preferences_text=None,
        annotations_text=None,
        critique=None,
        page_budget=1,
    )
    assert "[Preferences]" not in msg
    assert "[Annotations]" not in msg
    assert "[Critique]" not in msg
    assert "[JobDescription]" in msg


# ---- validate_trace_refs --------------------------------------------------


def test_validate_raises_on_unknown_role_ref() -> None:
    resume = TailoredResume(
        summary="x",
        contact=_contact(),
        experience=[
            TailoredRole(
                company="Acme",
                title="FE",
                start="2020-01",
                bullets=[],
                source_role_ref="unknown-role",
            )
        ],
        skills=[],
    )
    with pytest.raises(ValueError, match="unknown role id"):
        validate_trace_refs(resume, _optimized())


def test_validate_drops_bullets_without_ref() -> None:
    resume = TailoredResume(
        summary="x",
        contact=_contact(),
        experience=[
            TailoredRole(
                company="FightCamp",
                title="Senior Frontend Engineer",
                start="2021-11",
                bullets=[
                    TailoredBullet(text="No ref here.", source_outcome_ref=None),
                ],
                source_role_ref="fc",
            )
        ],
        skills=[],
    )
    cleaned, warnings = validate_trace_refs(resume, _optimized())
    assert cleaned.experience[0].bullets == []
    assert len(warnings) == 1
    assert "no ref" in warnings[0].lower()


def test_validate_drops_bullets_with_untraceable_ref() -> None:
    resume = TailoredResume(
        summary="x",
        contact=_contact(),
        experience=[
            TailoredRole(
                company="FightCamp",
                title="Senior Frontend Engineer",
                start="2021-11",
                bullets=[
                    TailoredBullet(
                        text="Invented claim.",
                        source_outcome_ref="something the LLM made up",
                    ),
                ],
                source_role_ref="fc",
            )
        ],
        skills=[],
    )
    cleaned, warnings = validate_trace_refs(resume, _optimized())
    assert cleaned.experience[0].bullets == []
    assert any("untraceable" in w for w in warnings)


def test_validate_keeps_bullets_matching_outcome_description() -> None:
    resume = TailoredResume(
        summary="x",
        contact=_contact(),
        experience=[
            TailoredRole(
                company="FightCamp",
                title="Senior Frontend Engineer",
                start="2021-11",
                bullets=[
                    TailoredBullet(
                        text="Cut mobile LCP from 10s to 2s.",
                        source_outcome_ref="Cut mobile load times from 10s to 2s",
                    ),
                ],
                source_role_ref="fc",
            )
        ],
        skills=[],
    )
    cleaned, warnings = validate_trace_refs(resume, _optimized())
    assert len(cleaned.experience[0].bullets) == 1
    assert warnings == []


def test_validate_keeps_bullets_matching_role_summary_substring() -> None:
    resume = TailoredResume(
        summary="x",
        contact=_contact(),
        experience=[
            TailoredRole(
                company="FightCamp",
                title="Senior Frontend Engineer",
                start="2021-11",
                bullets=[
                    TailoredBullet(
                        text="Led the PDP rebuild.",
                        source_outcome_ref="Led the PDP rebuild",
                    ),
                ],
                source_role_ref="fc",
            )
        ],
        skills=[],
    )
    cleaned, _ = validate_trace_refs(resume, _optimized())
    assert len(cleaned.experience[0].bullets) == 1


# ---- claim-level faithfulness (#47) ---------------------------------------


def _resume_with_bullet(text: str, ref: str, *, skills: list[str] | None = None,
                        summary: str = "Senior FE.") -> TailoredResume:
    return TailoredResume(
        summary=summary,
        contact=_contact(),
        experience=[
            TailoredRole(
                company="FightCamp",
                title="Senior Frontend Engineer",
                start="2021-11",
                bullets=[TailoredBullet(text=text, source_outcome_ref=ref)],
                source_role_ref="fc",
            )
        ],
        skills=skills if skills is not None else [],
    )


def test_validate_drops_bullet_with_fabricated_number_on_valid_ref() -> None:
    # The ref points at a real outcome ("Cut mobile load times from 10s to 2s")
    # but the shipped text inflates the win with numbers the source never had.
    # This is the headline #47 gap: a real ref, a fabricated metric.
    resume = _resume_with_bullet(
        "Cut LCP from 10s to 0.5s and grew revenue 40%.",
        "Cut mobile load times from 10s to 2s",
    )
    cleaned, warnings = validate_trace_refs(resume, _optimized())
    assert cleaned.experience[0].bullets == []
    assert any("fabricated number" in w for w in warnings)


def test_validate_keeps_bullet_whose_numbers_are_all_in_source() -> None:
    # Same ref; every number the bullet ships ("10s", "2s") is in the source.
    # Faithful text must pass unchanged with no warning.
    resume = _resume_with_bullet(
        "Cut mobile LCP from 10s to 2s.",
        "Cut mobile load times from 10s to 2s",
    )
    cleaned, warnings = validate_trace_refs(resume, _optimized())
    assert len(cleaned.experience[0].bullets) == 1
    assert warnings == []


def test_validate_rejects_trivial_substring_summary_ref() -> None:
    # The role summary is "Led the PDP rebuild and cut mobile load times."
    # A trivial one-word ref ('the') is a substring but not real traceability;
    # the tightened clause check must reject it (#47 fix 4).
    resume = _resume_with_bullet("Worked on the team.", "the")
    cleaned, warnings = validate_trace_refs(resume, _optimized())
    assert cleaned.experience[0].bullets == []
    assert any("untraceable" in w for w in warnings)


def test_validate_keeps_meaningful_clause_summary_ref() -> None:
    # A genuine multi-word clause from the summary still traces (no over-strip).
    resume = _resume_with_bullet("Led the PDP rebuild.", "Led the PDP rebuild")
    cleaned, warnings = validate_trace_refs(resume, _optimized())
    assert len(cleaned.experience[0].bullets) == 1
    assert warnings == []


def test_validate_drops_unknown_skill_from_resume() -> None:
    # "Kafka" is not in optimized.skills (React/TypeScript). Drop + warn (#47
    # fix 2). "react" (lowercase) and "TypeScript" survive via the canonical
    # map, proving the tolerance carries over from the cover-letter path.
    resume = _resume_with_bullet(
        "Led the PDP rebuild.", "Led the PDP rebuild",
        skills=["react", "TypeScript", "Kafka"],
    )
    cleaned, warnings = validate_trace_refs(resume, _optimized())
    assert cleaned.skills == ["React", "TypeScript"]
    assert any("Kafka" in w for w in warnings)


def test_validate_enforces_skills_cap() -> None:
    # 25 valid skills must be trimmed to the 20 cap, with a warning.
    payload = OptimizedPayload(
        roles=[Role(id="fc", company="FightCamp", title="FE", start="2021-11",
                    summary="Built things across the stack with care.")],
        skills=[Skill(name=f"Skill{i}") for i in range(25)],
    )
    resume = TailoredResume(
        summary="Senior FE.",
        contact=_contact(),
        experience=[],
        skills=[f"Skill{i}" for i in range(25)],
    )
    cleaned, warnings = validate_trace_refs(resume, payload)
    assert len(cleaned.skills) == 20
    assert any("cap" in w.lower() for w in warnings)


def test_validate_warns_on_fabricated_summary_number_without_stripping() -> None:
    # The summary is required text (min_length=1), so a fabricated number is
    # WARNed and surfaced, not stripped (#47 fix 2/conservative policy).
    resume = _resume_with_bullet(
        "Led the PDP rebuild.", "Led the PDP rebuild",
        summary="Senior FE who grew revenue 300% in 18 months.",
    )
    cleaned, warnings = validate_trace_refs(resume, _optimized())
    assert cleaned.summary == "Senior FE who grew revenue 300% in 18 months."
    assert any("summary contains number" in w for w in warnings)


def test_validate_summary_with_grounded_number_is_silent() -> None:
    # Source corpus has "10s"/"2s"; a summary number that matches the digits
    # ("2" -> "2s" in source) must not warn.
    resume = _resume_with_bullet(
        "Led the PDP rebuild.", "Led the PDP rebuild",
        summary="Cut load to 2s on the flagship surface.",
    )
    _, warnings = validate_trace_refs(resume, _optimized())
    assert not any("summary contains number" in w for w in warnings)


def test_validate_pins_company_from_source_role() -> None:
    # The LLM labels the role with a company the user never worked at; the
    # source_role_ref still points at a real role (#87 employer misattribution).
    resume = TailoredResume(
        summary="x",
        contact=_contact(),
        experience=[
            TailoredRole(
                company="HubSpot",
                title="Senior Frontend Engineer",
                start="2021-11",
                end="2024-04",
                bullets=[],
                source_role_ref="fc",
            )
        ],
        skills=[],
    )
    cleaned, warnings = validate_trace_refs(resume, _optimized())
    assert cleaned.experience[0].company == "FightCamp"
    assert any("Corrected employer" in w and "HubSpot" in w for w in warnings)


def test_validate_pins_dates_from_source_role() -> None:
    # Invented dates (impossible overlap) are overwritten from the source
    # Role's stored timeline (#87 date fabrication).
    resume = TailoredResume(
        summary="x",
        contact=_contact(),
        experience=[
            TailoredRole(
                company="FightCamp",
                title="Senior Frontend Engineer",
                start="2099-01",
                end="2099-12",
                bullets=[],
                source_role_ref="fc",
            )
        ],
        skills=[],
    )
    cleaned, _ = validate_trace_refs(resume, _optimized())
    assert cleaned.experience[0].start == "2021-11"
    assert cleaned.experience[0].end == "2024-04"


def test_validate_drops_cross_employer_bullet() -> None:
    # The "Cut mobile load times" outcome belongs to role "fc" but is placed
    # under "winc" — a cross-employer accomplishment leak (#87).
    resume = TailoredResume(
        summary="x",
        contact=_contact(),
        experience=[
            TailoredRole(
                company="Winc",
                title="Frontend Engineer",
                start="2018-01",
                end="2021-10",
                bullets=[
                    TailoredBullet(
                        text="Cut mobile LCP from 10s to 2s.",
                        source_outcome_ref="Cut mobile load times from 10s to 2s",
                    ),
                ],
                source_role_ref="winc",
            )
        ],
        skills=[],
    )
    cleaned, warnings = validate_trace_refs(resume, _optimized())
    assert cleaned.experience[0].bullets == []
    assert any("wrong employer" in w for w in warnings)


# ---- tailor_resume end-to-end --------------------------------------------


async def test_tailor_resume_parses_and_returns_result() -> None:
    llm = MockLLMClient(scripted={DEFAULT_PURPOSE: _valid_resume_json()})
    resume, warnings, result = await tailor_resume(
        llm,
        optimized=_optimized(),
        job_description="We want a senior FE",
        contact=_contact(),
    )
    assert isinstance(resume, TailoredResume)
    assert len(resume.experience) == 1
    assert result.cost_usd > 0
    assert warnings == []


async def test_tailor_resume_cost_logs_under_default_purpose() -> None:
    llm = MockLLMClient(scripted={DEFAULT_PURPOSE: _valid_resume_json()})
    await tailor_resume(
        llm,
        optimized=_optimized(),
        job_description="jd",
        contact=_contact(),
    )
    assert len(llm.calls) == 1
    assert llm.calls[0]["purpose"] == DEFAULT_PURPOSE


async def test_tailor_enables_system_cache() -> None:
    llm = MockLLMClient(scripted={DEFAULT_PURPOSE: _valid_resume_json()})
    await tailor_resume(
        llm,
        optimized=_optimized(),
        job_description="jd",
        contact=_contact(),
    )
    assert llm.calls[0]["cache_system"] is True


async def test_tailor_sends_preferences_into_user_message() -> None:
    seen: dict[str, str] = {}

    def responder(latest_user: str, _messages: object) -> str:
        seen["latest"] = latest_user
        return _valid_resume_json()

    llm = MockLLMClient(scripted={DEFAULT_PURPOSE: responder})
    await tailor_resume(
        llm,
        optimized=_optimized(),
        job_description="jd",
        contact=_contact(),
        preferences_rules=["use present tense"],
        preferences_avoid=["em dashes"],
    )
    assert "[Preferences]" in seen["latest"]
    assert "use present tense" in seen["latest"]
    assert "em dashes" in seen["latest"]


async def test_tailor_sends_critique_into_user_message() -> None:
    seen: dict[str, str] = {}

    def responder(latest_user: str, _messages: object) -> str:
        seen["latest"] = latest_user
        return _valid_resume_json()

    llm = MockLLMClient(scripted={DEFAULT_PURPOSE: responder})
    await tailor_resume(
        llm,
        optimized=_optimized(),
        job_description="jd",
        contact=_contact(),
        critique="Lead with performance not design systems.",
    )
    assert "[Critique]" in seen["latest"]
    assert "performance" in seen["latest"]


async def test_tailor_model_override_respected() -> None:
    llm = MockLLMClient(scripted={DEFAULT_PURPOSE: _valid_resume_json()})
    await tailor_resume(
        llm,
        optimized=_optimized(),
        job_description="jd",
        contact=_contact(),
        model="claude-haiku-4-5",
    )
    assert llm.calls[0]["model"] == "claude-haiku-4-5"


async def test_tailor_drops_hallucinated_bullets() -> None:
    bad = TailoredResume(
        summary="x",
        contact=_contact(),
        experience=[
            TailoredRole(
                company="FightCamp",
                title="Senior Frontend Engineer",
                start="2021-11",
                bullets=[
                    TailoredBullet(
                        text="Real outcome.",
                        source_outcome_ref="Cut mobile load times from 10s to 2s",
                    ),
                    TailoredBullet(
                        text="Hallucinated.",
                        source_outcome_ref="Raised Series D of $1B",
                    ),
                ],
                source_role_ref="fc",
            )
        ],
        skills=[],
    )
    llm = MockLLMClient(scripted={DEFAULT_PURPOSE: bad.model_dump_json()})
    resume, warnings, _ = await tailor_resume(
        llm,
        optimized=_optimized(),
        job_description="jd",
        contact=_contact(),
    )
    assert len(resume.experience[0].bullets) == 1
    assert resume.experience[0].bullets[0].text == "Real outcome."
    assert any("untraceable" in w for w in warnings)


async def test_tailor_raises_on_hallucinated_role() -> None:
    bad = TailoredResume(
        summary="x",
        contact=_contact(),
        experience=[
            TailoredRole(
                company="Imaginary Inc.",
                title="CTO",
                start="2015-01",
                bullets=[],
                source_role_ref="not-in-payload",
            )
        ],
        skills=[],
    )
    llm = MockLLMClient(scripted={DEFAULT_PURPOSE: bad.model_dump_json()})
    with pytest.raises(ValueError):
        await tailor_resume(
            llm,
            optimized=_optimized(),
            job_description="jd",
            contact=_contact(),
        )


async def test_invalid_json_raises() -> None:
    llm = MockLLMClient(scripted={DEFAULT_PURPOSE: "not json"})
    with pytest.raises(Exception):
        await tailor_resume(
            llm,
            optimized=_optimized(),
            job_description="jd",
            contact=_contact(),
        )


def test_tailored_resume_json_round_trips() -> None:
    raw = _valid_resume_json()
    parsed = TailoredResume.model_validate_json(raw)
    assert parsed.contact.name == "Daniel Joffe"
    assert parsed.experience[0].source_role_ref == "fc"
    assert json.loads(raw)["skills"] == ["React", "TypeScript"]


def _cover_letter(skill_refs: list[str]) -> TailoredCoverLetter:
    """Bare letter used to exercise the ref-validation tolerance."""
    from app.models.tailor import CoverLetterParagraph

    return TailoredCoverLetter(
        contact=ContactInfo(name="Daniel Joffe", email="d@example.com"),
        recipient_company="Acme",
        salutation="Dear Acme team,",
        paragraphs=[CoverLetterParagraph(text="Body.")],
        closing="Sincerely,",
        signature="Daniel Joffe",
        source_role_refs=[],
        source_outcome_refs=[],
        source_skill_refs=skill_refs,
    )


def test_validate_cover_letter_accepts_canonical_skill() -> None:
    optimized = _optimized()  # has skills "React" + "TypeScript"
    letter, warnings = validate_cover_letter_refs(
        _cover_letter(["React"]), optimized
    )
    assert letter.source_skill_refs == ["React"]
    assert warnings == []


def test_validate_cover_letter_tolerates_pluralization_and_case() -> None:
    """LLM commonly emits 'TypeScripts' or 'react' when canonical is
    'TypeScript' / 'React'. Tolerance keeps the canonical form in the
    response and suppresses the cosmetic 'Dropped unknown skill_ref'
    warning users would otherwise see in their cover letter metadata."""
    optimized = _optimized()
    letter, warnings = validate_cover_letter_refs(
        _cover_letter(["react", "TypeScripts"]), optimized
    )
    assert sorted(letter.source_skill_refs) == ["React", "TypeScript"]
    assert warnings == []


def test_validate_cover_letter_drops_truly_unknown_skill() -> None:
    """Tolerance must not over-match: a skill that genuinely isn't in
    the optimized doc still gets dropped + warned."""
    optimized = _optimized()
    letter, warnings = validate_cover_letter_refs(
        _cover_letter(["Kafka"]), optimized
    )
    assert letter.source_skill_refs == []
    assert any("Kafka" in w for w in warnings)


# ---- attribution ownership (#87 follow-up) --------------------------------


def test_owner_role_id_prefers_role_ref() -> None:
    optimized = _optimized()
    outcome = optimized.outcomes[0]  # role_ref="fc"
    assert optimized.owner_role_id(outcome) == "fc"


def test_owner_role_id_falls_back_to_reverse_link() -> None:
    # role_ref is null, but role "fc" lists the outcome in outcome_refs.
    optimized = OptimizedPayload(
        roles=[
            Role(
                id="fc",
                company="FightCamp",
                title="Senior FE",
                start="2021-11",
                end="2024-04",
                outcome_refs=["Cut mobile LCP from 10s to 2s"],
            )
        ],
        outcomes=[Outcome(description="Cut mobile LCP from 10s to 2s", role_ref=None)],
    )
    assert optimized.owner_role_id(optimized.outcomes[0]) == "fc"


def test_owner_role_id_unknown_when_no_link() -> None:
    optimized = OptimizedPayload(
        roles=[Role(id="fc", company="FightCamp", title="FE", start="2021-11")],
        outcomes=[Outcome(description="Orphan outcome", role_ref=None)],
    )
    assert optimized.owner_role_id(optimized.outcomes[0]) is None


def test_validate_drops_cross_employer_bullet_via_reverse_link() -> None:
    # The outcome has no role_ref, but role "fc" owns it via outcome_refs.
    # The LLM still files the bullet under "winc" — the drop must fire even
    # though the forward link was null (#87 null-role_ref bypass).
    optimized = OptimizedPayload(
        roles=[
            Role(
                id="fc",
                company="FightCamp",
                title="Senior FE",
                start="2021-11",
                end="2024-04",
                outcome_refs=["Cut mobile LCP from 10s to 2s"],
            ),
            Role(id="winc", company="Winc", title="FE", start="2018-01", end="2021-10"),
        ],
        outcomes=[Outcome(description="Cut mobile LCP from 10s to 2s", role_ref=None)],
    )
    resume = TailoredResume(
        summary="x",
        contact=_contact(),
        experience=[
            TailoredRole(
                company="Winc",
                title="FE",
                start="2018-01",
                end="2021-10",
                bullets=[
                    TailoredBullet(
                        text="Cut LCP.",
                        source_outcome_ref="Cut mobile LCP from 10s to 2s",
                    )
                ],
                source_role_ref="winc",
            )
        ],
        skills=[],
    )
    cleaned, warnings = validate_trace_refs(resume, optimized)
    assert cleaned.experience[0].bullets == []
    assert any("wrong employer" in w for w in warnings)


def _letter_citing(outcome_refs: list[str], role_refs: list[str]) -> TailoredCoverLetter:
    from app.models.tailor import CoverLetterParagraph

    return TailoredCoverLetter(
        contact=ContactInfo(name="Daniel Joffe", email="d@example.com"),
        recipient_company="Acme",
        salutation="Dear Acme team,",
        paragraphs=[CoverLetterParagraph(text="Body.")],
        closing="Sincerely,",
        signature="Daniel Joffe",
        source_role_refs=role_refs,
        source_outcome_refs=outcome_refs,
        source_skill_refs=[],
    )


def test_cover_letter_credits_outcome_owner_when_undeclared() -> None:
    # The letter draws on an outcome owned by "fc" but declares no role refs:
    # the fingerprint of a cross-employer misattribution in the prose (#87).
    letter = _letter_citing(["Cut mobile load times from 10s to 2s"], [])
    cleaned, warnings = validate_cover_letter_refs(letter, _optimized())
    assert "fc" in cleaned.source_role_refs
    assert any("owned by role 'fc'" in w for w in warnings)


def test_cover_letter_no_warning_when_owner_declared() -> None:
    letter = _letter_citing(["Cut mobile load times from 10s to 2s"], ["fc"])
    cleaned, warnings = validate_cover_letter_refs(letter, _optimized())
    assert cleaned.source_role_refs == ["fc"]
    assert warnings == []


# ---- prompt caching (#73) -------------------------------------------------


def test_optimized_section_is_message_prefix() -> None:
    optimized = _optimized()
    msg = build_user_message(
        optimized=optimized,
        job_description="jd",
        contact=_contact(),
        resume_type="generic",
        preferences_text=None,
        annotations_text=None,
        critique=None,
        page_budget=2,
    )
    assert msg.startswith(_optimized_section(optimized))


async def test_tailor_resume_sets_cache_breakpoint_on_master_doc() -> None:
    optimized = _optimized()
    llm = MockLLMClient(scripted={DEFAULT_PURPOSE: _valid_resume_json()})
    await tailor_resume(
        llm,
        optimized=optimized,
        job_description="We want a senior FE",
        contact=_contact(),
    )
    user_msg = llm.calls[-1]["messages"][0]  # type: ignore[index]
    section = _optimized_section(optimized)
    # The user turn carries a cache breakpoint at the end of the master doc,
    # and that prefix is byte-identical to the OptimizedPayload section so the
    # cached block stays stable across calls.
    assert user_msg.cache_prefix_chars == len(section)
    assert user_msg.content[: user_msg.cache_prefix_chars] == section
