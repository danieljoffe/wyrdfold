"""MockLLMClient behavior."""

import json

import pytest

from app.models.llm import Message
from app.models.targets import TargetSuggestions
from app.services.llm.client import complete_json
from app.services.llm.errors import MissingToolCallError
from app.services.llm.mock import (
    QUERY_SUGGEST_PURPOSE,
    MockLLMClient,
    dev_default_responses,
)


async def test_echo_mode_returns_json_with_latest_user_content() -> None:
    client = MockLLMClient()
    result = await client.complete(
        model="claude-haiku-4-5",
        system="sys",
        messages=[Message(role="user", content="hello world")],
        purpose="test.echo",
    )
    parsed = json.loads(result.content)
    assert parsed["echo"] == "hello world"
    assert parsed["purpose"] == "test.echo"
    assert result.model == "claude-haiku-4-5"


async def test_scripted_string_response() -> None:
    client = MockLLMClient(scripted={"derive": '{"ok": true}'})
    result = await client.complete(
        model="claude-sonnet-4-6",
        system="",
        messages=[Message(role="user", content="irrelevant")],
        purpose="derive",
    )
    assert result.content == '{"ok": true}'


async def test_scripted_callable_sees_latest_user_content() -> None:
    seen: dict[str, str] = {}

    def responder(latest_user: str, _messages: list[Message]) -> str:
        seen["latest"] = latest_user
        return f"got:{latest_user}"

    client = MockLLMClient(scripted={"p": responder})
    result = await client.complete(
        model="claude-haiku-4-5",
        system="",
        messages=[
            Message(role="user", content="first"),
            Message(role="assistant", content="mid"),
            Message(role="user", content="second"),
        ],
        purpose="p",
    )
    assert seen["latest"] == "second"
    assert result.content == "got:second"


async def test_register_adds_scripted_response() -> None:
    client = MockLLMClient()
    client.register("late", "OK")
    result = await client.complete(
        model="claude-haiku-4-5",
        system="",
        messages=[Message(role="user", content="anything")],
        purpose="late",
    )
    assert result.content == "OK"


async def test_call_is_tracked() -> None:
    client = MockLLMClient()
    await client.complete(
        model="claude-haiku-4-5",
        system="sys",
        messages=[Message(role="user", content="hi")],
        purpose="tracked",
    )
    assert len(client.calls) == 1
    assert client.calls[0]["purpose"] == "tracked"
    assert client.calls[0]["model"] == "claude-haiku-4-5"


async def test_usage_and_cost_are_nonzero() -> None:
    client = MockLLMClient()
    result = await client.complete(
        model="claude-sonnet-4-6",
        system="some system prompt",
        messages=[Message(role="user", content="some reasonably long input string")],
        purpose="u",
    )
    assert result.usage.input_tokens > 0
    assert result.usage.output_tokens > 0
    assert result.cost_usd > 0


async def test_cache_system_hint_bumps_cache_creation_tokens() -> None:
    client = MockLLMClient()
    without = await client.complete(
        model="claude-sonnet-4-6",
        system="cached",
        messages=[Message(role="user", content="x")],
        purpose="nocache",
        cache_system=False,
    )
    with_cache = await client.complete(
        model="claude-sonnet-4-6",
        system="cached",
        messages=[Message(role="user", content="x")],
        purpose="cache",
        cache_system=True,
    )
    assert without.usage.cache_creation_input_tokens == 0
    assert with_cache.usage.cache_creation_input_tokens > 0


async def test_empty_messages_raises() -> None:
    client = MockLLMClient()
    with pytest.raises(ValueError):
        await client.complete(
            model="claude-haiku-4-5",
            system="",
            messages=[],
            purpose="empty",
        )


async def test_complete_json_parses_against_schema() -> None:
    from pydantic import BaseModel

    class Shape(BaseModel):
        name: str
        value: int

    client = MockLLMClient(scripted={"parsed": '{"name": "x", "value": 42}'})
    parsed, result = await complete_json(
        client,
        model="claude-sonnet-4-6",
        system="",
        messages=[Message(role="user", content="go")],
        schema=Shape,
        purpose="parsed",
    )
    assert parsed.name == "x"
    assert parsed.value == 42
    assert result.cost_usd > 0


async def test_complete_tool_use_returns_dict_from_scripted_json() -> None:
    client = MockLLMClient(scripted={"tool": '{"a": 1, "b": "two"}'})
    tool_input, result = await client.complete_tool_use(
        model="claude-haiku-4-5",
        system="",
        messages=[Message(role="user", content="x")],
        tool_name="return_X",
        tool_description="d",
        tool_input_schema={"type": "object"},
        purpose="tool",
    )
    assert tool_input == {"a": 1, "b": "two"}
    assert result.content == '{"a": 1, "b": "two"}'


async def test_complete_tool_use_prose_script_raises_missing_tool_call() -> None:
    """Prose scripted output models the deepseek 2026-08-05 flake — the model
    ignoring ``tool_choice`` and answering in plain text. The mock must raise
    the same typed ``MissingToolCallError`` the real parser does, so surface
    tests inherit the exact failure shape from the bug corpus."""
    client = MockLLMClient(
        scripted={"tool": "This title is clearly unrelated to DevOps/SRE engineering."}
    )
    with pytest.raises(MissingToolCallError, match="Expected a forced tool_call"):
        await client.complete_tool_use(
            model="deepseek-v3-2",
            system="",
            messages=[Message(role="user", content="x")],
            tool_name="return_TitleTriageResponse",
            tool_description="d",
            tool_input_schema={"type": "object"},
            purpose="tool",
        )


async def test_complete_tool_use_records_tool_name_in_call_log() -> None:
    client = MockLLMClient(scripted={"tool": "{}"})
    await client.complete_tool_use(
        model="claude-haiku-4-5",
        system="",
        messages=[Message(role="user", content="x")],
        tool_name="return_Foo",
        tool_description="d",
        tool_input_schema={"type": "object"},
        purpose="tool",
    )
    assert client.calls[0]["tool_name"] == "return_Foo"


# ---- dev-default responses (local `LLM_PROVIDER=mock` realism) ----------------


def test_dev_default_responses_covers_query_suggest() -> None:
    """The seed exposes the query-suggest purpose so local search fallback
    returns usable data instead of the bare echo."""
    assert QUERY_SUGGEST_PURPOSE in dev_default_responses()


def test_dev_default_responses_returns_a_fresh_dict_each_call() -> None:
    """Callers mutate their own copy — the seed must not be shared state."""
    a = dev_default_responses()
    a["extra"] = "x"
    assert "extra" not in dev_default_responses()


async def test_dev_default_query_suggest_echoes_query_as_first_suggestion() -> None:
    """Seeded mock synthesizes the query as the canonical first suggestion plus
    adjacent-seniority neighbours — a valid TargetSuggestions, not the echo."""
    client = MockLLMClient(scripted=dev_default_responses())
    parsed, _ = await complete_json(
        client,
        model="claude-sonnet-4-6",
        system="",
        messages=[Message(role="user", content="frontend engineer\n\nbackground")],
        schema=TargetSuggestions,
        purpose=QUERY_SUGGEST_PURPOSE,
    )
    assert parsed.suggestions
    assert parsed.suggestions[0].label == "Frontend Engineer"
    labels = {s.label for s in parsed.suggestions}
    assert "Senior Frontend Engineer" in labels  # a neighbour was added
    # No duplicate labels leak from the seniority-ladder dedup.
    assert len(labels) == len(parsed.suggestions)


async def test_dev_default_query_suggest_survives_blank_query() -> None:
    """An empty/whitespace query must still yield a valid suggestion, never a
    crash or an empty label (min_length=1 on TargetSuggestion.label)."""
    client = MockLLMClient(scripted=dev_default_responses())
    parsed, _ = await complete_json(
        client,
        model="claude-sonnet-4-6",
        system="",
        messages=[Message(role="user", content="   ")],
        schema=TargetSuggestions,
        purpose=QUERY_SUGGEST_PURPOSE,
    )
    assert parsed.suggestions
    assert all(s.label for s in parsed.suggestions)


def test_dev_default_job_analysis_is_schema_valid() -> None:
    """The dev-default analysis verdict must validate as ``JobAnalysis``.

    The 2026-08-05 e2e drive found the generic echo failing validation, so
    every mock-env analysis surfaced "Analysis failed" — local dev and CI
    could not drive the panel's flagship flow (#608). This pins the canned
    verdict to the real schema so model drift breaks the test, not the
    mock environments.
    """
    import json

    from app.models.analysis import JobAnalysis
    from app.services.llm.mock import JOB_ANALYSIS_PURPOSE, dev_default_responses

    source = dev_default_responses()[JOB_ANALYSIS_PURPOSE]
    assert callable(source)
    payload = json.loads(source("ignored", []))
    analysis = JobAnalysis.model_validate(payload)
    assert analysis.recommendation
    assert analysis.scorecard.skills_matched


def test_ats_hostile_resume_is_schema_valid_but_fails_the_real_linter() -> None:
    """Bug-corpus entry for #656: a resume the model got *right* by its own
    contract and wrong by the ATS linter's.

    Both halves are load-bearing. Schema-valid, or the flagged-draft path
    would never be reached (trace validation would reject it first). Actually
    lint-failing under the REAL linter, or a surface scripting it would think
    it was exercising the flagged path while quietly taking the success
    branch. A stubbed linter can't catch either mistake.
    """
    from app.models.tailor import TailoredResume
    from app.services.ats_lint import lint_markdown
    from app.services.llm.mock import ats_hostile_resume_json
    from app.services.tailor.markdown_render import to_markdown

    resume = TailoredResume.model_validate_json(ats_hostile_resume_json())
    result = lint_markdown(to_markdown(resume), document_type="resume")

    assert result.ok is False
    assert any(v.code == "no_tables" and v.severity == "error" for v in result.errors)


@pytest.mark.asyncio
async def test_country_name_job_fit_survives_the_real_parse_path() -> None:
    """Bug-corpus entry for #693: a grader response that is right everywhere
    except one field's FORMAT must not cost the whole grade.

    Driven through the real ``complete_json`` + ``JobFitResult`` path, not a
    direct model construction — the bug lived in the one-shot validation of
    the whole payload, so validating the model alone would miss the thing that
    actually broke (a surface would think it was covered while the real parse
    still died).
    """
    from app.services.fit.job_fit import JobFitResult
    from app.services.llm.client import complete_json
    from app.services.llm.mock import country_name_job_fit_json

    client = MockLLMClient(scripted={"fit.job": country_name_job_fit_json()})
    parsed, _result = await complete_json(
        client,
        model="claude-sonnet-4-6",
        system="grade this",
        messages=[Message(role="user", content="jd")],
        schema=JobFitResult,
        purpose="fit.job",
    )

    # The grade survives — and the malformed field is normalized, not dropped.
    assert parsed.fit_score == 82
    assert parsed.axes.title_fit == 95
    assert parsed.logistics is not None
    assert parsed.logistics.location_country == "IN"
    # The rest of the payload is untouched by normalization.
    assert parsed.logistics.location_city == "Bengaluru"
    assert parsed.logistics.remote_status == "hybrid"


def _dev_optimized():
    from app.models.experience import OptimizedPayload, Outcome, Role, Skill

    return OptimizedPayload(
        summary="Senior FE.",
        roles=[
            Role(
                id="fc",
                company="FightCamp",
                title="Senior Frontend Engineer",
                start="2021-11",
                end="2024-04",
                summary="Led the PDP rebuild.",
                skills=["React"],
                outcome_refs=["o1"],
            )
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


def test_dev_default_tailor_resume_survives_the_real_trace_validator() -> None:
    """Schema-valid is NOT enough here — ``validate_trace_refs`` RAISES on a
    ``source_role_ref`` that isn't a real ``Role.id``, and silently drops
    bullets carrying a number the source doesn't have. So a canned response
    would either explode or come back empty.

    Same gap #608 closed for ``job_analysis``: without a seed the echo fails
    validation and every mock-env tailoring dies with "Resume generation
    failed" — local dev and CI can't drive the flow at all. Re-found by a live
    drive on 2026-08-07, which is why this pins the validator, not the schema.
    """
    from app.models.tailor import ContactInfo, TailoredResume
    from app.services.llm.mock import TAILOR_RESUME_PURPOSE, dev_default_responses
    from app.services.tailor.tailor import build_user_message, validate_trace_refs

    optimized = _dev_optimized()
    prompt = build_user_message(
        optimized=optimized,
        job_description="We want a senior FE.",
        contact=ContactInfo(name="Daniel Joffe", email="d@example.com"),
        resume_type="generic",
        preferences_text=None,
        annotations_text=None,
        critique=None,
        page_budget=2,
    )

    source = dev_default_responses()[TAILOR_RESUME_PURPOSE]
    assert callable(source)
    resume = TailoredResume.model_validate_json(source(prompt, []))

    repaired, warnings = validate_trace_refs(resume, optimized)
    assert repaired.experience, "trace validation stripped every role"
    assert repaired.experience[0].source_role_ref == "fc"
    # The bullet survived — it traces to a real outcome and invents no number.
    assert repaired.experience[0].bullets, "trace validation dropped every bullet"
    assert repaired.skills, "every skill was rejected as unknown"


def test_dev_default_cover_letter_survives_the_real_ref_validator() -> None:
    from app.models.tailor import ContactInfo, TailoredCoverLetter
    from app.services.llm.mock import TAILOR_COVER_LETTER_PURPOSE, dev_default_responses
    from app.services.tailor.tailor import (
        build_cover_letter_user_message,
        validate_cover_letter_refs,
    )

    optimized = _dev_optimized()
    prompt = build_cover_letter_user_message(
        optimized=optimized,
        job_description="We want a senior FE.",
        company_name="Acme",
        contact=ContactInfo(name="Daniel Joffe", email="d@example.com"),
        role_title="Senior Frontend Engineer",
        preferences_text=None,
        annotations_text=None,
        critique=None,
    )

    source = dev_default_responses()[TAILOR_COVER_LETTER_PURPOSE]
    assert callable(source)
    letter = TailoredCoverLetter.model_validate_json(source(prompt, []))

    assert letter.recipient_company == "Acme"
    assert letter.paragraphs
    validate_cover_letter_refs(letter, optimized)
