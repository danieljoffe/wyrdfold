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
