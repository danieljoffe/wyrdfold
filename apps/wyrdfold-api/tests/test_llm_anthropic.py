"""AnthropicLLMClient tests.

Mock the SDK's `messages.create` at the instance level. Verifies the
client builds the request correctly (cache_control shape, messages
passthrough), parses responses into LLMResult (text extraction, usage
fields including cache tokens), and handles edge cases (empty messages,
thinking blocks mixed with text).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.llm import LLMUsage, Message
from app.services.llm.anthropic_client import AnthropicLLMClient, _reported_usage
from app.services.llm.pricing import calculate_cost, reported_cost_usd


def _fake_response(
    *,
    text: str = "hello",
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_read: int = 0,
    cache_creation: int = 0,
    extra_blocks: list[Any] | None = None,
    usage_extra: dict[str, Any] | None = None,
) -> Any:
    """Build a mock response object shaped like Anthropic's SDK returns.

    ``usage_extra`` mirrors pydantic's ``model_extra`` — the fields the SDK
    could not type. A direct api.anthropic.com response has none; an
    OpenRouter one carries ``cost`` / ``cost_details`` / ``is_byok`` there
    (verified against a live response). ``None`` is the direct-Anthropic case
    and must stay a non-Mapping so the client sees "no extras".
    """
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = text

    blocks = [text_block] + (extra_blocks or [])

    response = MagicMock()
    response.content = blocks
    response.usage.input_tokens = input_tokens
    response.usage.output_tokens = output_tokens
    response.usage.cache_read_input_tokens = cache_read
    response.usage.cache_creation_input_tokens = cache_creation
    response.usage.model_extra = usage_extra if usage_extra is not None else MagicMock()
    return response


def _client_with_mocked_sdk(response: Any) -> tuple[AnthropicLLMClient, AsyncMock]:
    client = AnthropicLLMClient(api_key="test-key")
    create_mock = AsyncMock(return_value=response)
    client._client.messages.create = create_mock  # type: ignore[method-assign]
    return client, create_mock


async def test_complete_returns_parsed_result() -> None:
    client, _ = _client_with_mocked_sdk(
        _fake_response(text="response text", input_tokens=120, output_tokens=30)
    )
    result = await client.complete(
        model="claude-haiku-4-5",
        system="system prompt",
        messages=[Message(role="user", content="hi")],
        purpose="test",
    )
    assert result.content == "response text"
    assert result.model == "claude-haiku-4-5"
    assert result.usage.input_tokens == 120
    assert result.usage.output_tokens == 30


async def test_complete_passes_messages_to_sdk() -> None:
    client, create_mock = _client_with_mocked_sdk(_fake_response())
    await client.complete(
        model="claude-sonnet-4-6",
        system="sys",
        messages=[
            Message(role="user", content="hi"),
            Message(role="assistant", content="there"),
            Message(role="user", content="bye"),
        ],
        purpose="test",
    )
    kwargs = create_mock.call_args.kwargs
    assert kwargs["model"] == "claude-sonnet-4-6"
    assert kwargs["messages"] == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "there"},
        {"role": "user", "content": "bye"},
    ]


async def test_cache_system_true_uses_list_form_with_cache_control() -> None:
    client, create_mock = _client_with_mocked_sdk(_fake_response())
    await client.complete(
        model="claude-sonnet-4-6",
        system="cached system",
        messages=[Message(role="user", content="x")],
        purpose="test",
        cache_system=True,
    )
    system = create_mock.call_args.kwargs["system"]
    assert isinstance(system, list)
    assert len(system) == 1
    assert system[0]["type"] == "text"
    assert system[0]["text"] == "cached system"
    assert system[0]["cache_control"] == {"type": "ephemeral"}


async def test_cache_system_false_uses_plain_string() -> None:
    client, create_mock = _client_with_mocked_sdk(_fake_response())
    await client.complete(
        model="claude-sonnet-4-6",
        system="plain system",
        messages=[Message(role="user", content="x")],
        purpose="test",
        cache_system=False,
    )
    assert create_mock.call_args.kwargs["system"] == "plain system"


async def test_cache_system_true_but_empty_system_stays_empty_string() -> None:
    """Don't send an empty list — the SDK accepts "" for no-system."""
    client, create_mock = _client_with_mocked_sdk(_fake_response())
    await client.complete(
        model="claude-haiku-4-5",
        system="",
        messages=[Message(role="user", content="x")],
        purpose="test",
        cache_system=True,
    )
    assert create_mock.call_args.kwargs["system"] == ""


async def test_cache_tokens_flow_through() -> None:
    client, _ = _client_with_mocked_sdk(_fake_response(cache_read=500, cache_creation=1200))
    result = await client.complete(
        model="claude-sonnet-4-6",
        system="sys",
        messages=[Message(role="user", content="x")],
        purpose="test",
    )
    assert result.usage.cache_read_input_tokens == 500
    assert result.usage.cache_creation_input_tokens == 1200


async def test_max_tokens_passed_to_sdk() -> None:
    client, create_mock = _client_with_mocked_sdk(_fake_response())
    await client.complete(
        model="claude-sonnet-4-6",
        system="sys",
        messages=[Message(role="user", content="x")],
        purpose="test",
        max_tokens=8192,
    )
    assert create_mock.call_args.kwargs["max_tokens"] == 8192


async def test_cost_calculated_from_usage() -> None:
    client, _ = _client_with_mocked_sdk(_fake_response(input_tokens=1_000_000, output_tokens=0))
    result = await client.complete(
        model="claude-sonnet-4-6",
        system="sys",
        messages=[Message(role="user", content="x")],
        purpose="test",
    )
    # Sonnet 4.6 = $3/MTok input, so 1M input tokens = $3.00
    assert result.cost_usd == pytest.approx(3.0, rel=1e-6)


async def test_latency_is_measured() -> None:
    client, _ = _client_with_mocked_sdk(_fake_response())
    result = await client.complete(
        model="claude-haiku-4-5",
        system="sys",
        messages=[Message(role="user", content="x")],
        purpose="test",
    )
    assert result.latency_ms >= 0


async def test_empty_messages_raises() -> None:
    client = AnthropicLLMClient(api_key="test-key")
    with pytest.raises(ValueError, match="at least one message"):
        await client.complete(
            model="claude-haiku-4-5",
            system="sys",
            messages=[],
            purpose="test",
        )


async def test_non_text_blocks_are_skipped_in_content() -> None:
    """Thinking / tool_use blocks shouldn't leak into LLMResult.content."""
    thinking_block = MagicMock()
    thinking_block.type = "thinking"
    thinking_block.thinking = "internal reasoning"

    client, _ = _client_with_mocked_sdk(
        _fake_response(text="visible text", extra_blocks=[thinking_block])
    )
    result = await client.complete(
        model="claude-opus-4-7",
        system="sys",
        messages=[Message(role="user", content="x")],
        purpose="test",
    )
    assert result.content == "visible text"
    assert "internal reasoning" not in result.content


def _fake_tool_use_response(
    *,
    tool_name: str,
    tool_input: dict[str, Any],
    input_tokens: int = 100,
    output_tokens: int = 50,
    stop_reason: str = "tool_use",
    usage_extra: dict[str, Any] | None = None,
) -> Any:
    """Build a mock response with a tool_use block matching the Anthropic SDK's shape."""
    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.name = tool_name
    tool_block.input = tool_input

    response = MagicMock()
    response.content = [tool_block]
    response.usage.input_tokens = input_tokens
    response.usage.output_tokens = output_tokens
    response.usage.cache_read_input_tokens = 0
    response.usage.cache_creation_input_tokens = 0
    response.usage.model_extra = usage_extra if usage_extra is not None else MagicMock()
    response.stop_reason = stop_reason
    return response


async def test_complete_tool_use_returns_input_dict() -> None:
    payload = {"name": "Daniel", "value": 42}
    client, create_mock = _client_with_mocked_sdk(
        _fake_tool_use_response(tool_name="return_Thing", tool_input=payload)
    )
    tool_input, result = await client.complete_tool_use(
        model="claude-sonnet-4-6",
        system="sys",
        messages=[Message(role="user", content="x")],
        tool_name="return_Thing",
        tool_description="Return a Thing.",
        tool_input_schema={"type": "object"},
        purpose="test",
    )
    assert tool_input == payload
    # Cost-log inspection should still see the structured payload as content.
    assert result.content == '{"name": "Daniel", "value": 42}'


async def test_complete_tool_use_raises_on_max_tokens_truncation() -> None:
    """A forced tool call that stops at ``max_tokens`` truncated the tool input
    mid-emission — fail loud so the caller's fallback engages instead of
    persisting silently-incomplete structured data (#47)."""
    client, _ = _client_with_mocked_sdk(
        _fake_tool_use_response(
            tool_name="return_Thing",
            tool_input={"name": "Dan"},  # present, but cut off at the limit
            stop_reason="max_tokens",
        )
    )
    with pytest.raises(ValueError, match="truncated"):
        await client.complete_tool_use(
            model="claude-sonnet-4-6",
            system="sys",
            messages=[Message(role="user", content="x")],
            tool_name="return_Thing",
            tool_description="Return a Thing.",
            tool_input_schema={"type": "object"},
            purpose="test",
        )


async def test_complete_tool_use_forces_tool_choice() -> None:
    client, create_mock = _client_with_mocked_sdk(
        _fake_tool_use_response(tool_name="return_X", tool_input={})
    )
    await client.complete_tool_use(
        model="claude-sonnet-4-6",
        system="sys",
        messages=[Message(role="user", content="x")],
        tool_name="return_X",
        tool_description="d",
        tool_input_schema={"type": "object", "properties": {}},
        purpose="test",
    )
    kwargs = create_mock.call_args.kwargs
    assert kwargs["tool_choice"] == {"type": "tool", "name": "return_X"}
    assert kwargs["tools"] == [
        {
            "name": "return_X",
            "description": "d",
            "input_schema": {"type": "object", "properties": {}},
        }
    ]


async def test_complete_tool_use_raises_when_no_tool_block() -> None:
    """If the API returns text instead of tool_use (refusal, abort), fail loud."""
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "I cannot answer that."
    response = MagicMock()
    response.content = [text_block]
    response.usage.input_tokens = 10
    response.usage.output_tokens = 5
    response.usage.cache_read_input_tokens = 0
    response.usage.cache_creation_input_tokens = 0
    response.stop_reason = "end_turn"

    client, _ = _client_with_mocked_sdk(response)
    with pytest.raises(ValueError, match="Expected tool_use block"):
        await client.complete_tool_use(
            model="claude-haiku-4-5",
            system="sys",
            messages=[Message(role="user", content="x")],
            tool_name="return_X",
            tool_description="d",
            tool_input_schema={"type": "object"},
            purpose="test",
        )


async def test_complete_json_uses_pydantic_schema_and_returns_typed_object() -> None:
    """End-to-end: pydantic schema → tool spec → API call → parsed object."""
    from pydantic import BaseModel

    from app.services.llm.client import complete_json

    class Contact(BaseModel):
        name: str
        email: str

    payload = {"name": "Daniel", "email": "a@b.com"}
    client, create_mock = _client_with_mocked_sdk(
        _fake_tool_use_response(tool_name="return_Contact", tool_input=payload)
    )
    parsed, _result = await complete_json(
        client,
        model="claude-sonnet-4-6",
        system="sys",
        messages=[Message(role="user", content="x")],
        schema=Contact,
        purpose="test",
    )
    assert isinstance(parsed, Contact)
    assert parsed.name == "Daniel"
    assert parsed.email == "a@b.com"
    # The tool sent to the API should carry the schema's JSON schema.
    sent_tool = create_mock.call_args.kwargs["tools"][0]
    assert sent_tool["name"] == "return_Contact"
    assert sent_tool["input_schema"]["properties"]["name"]["type"] == "string"


async def test_usage_without_cache_fields_defaults_to_zero() -> None:
    """Older responses might lack cache fields — don't crash."""
    response = MagicMock()
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "ok"
    response.content = [text_block]
    response.usage.input_tokens = 10
    response.usage.output_tokens = 5
    response.usage.cache_read_input_tokens = None
    response.usage.cache_creation_input_tokens = None

    client = AnthropicLLMClient(api_key="test-key")
    client._client.messages.create = AsyncMock(return_value=response)  # type: ignore[method-assign]

    result = await client.complete(
        model="claude-haiku-4-5",
        system="sys",
        messages=[Message(role="user", content="x")],
        purpose="test",
    )
    assert result.usage.cache_read_input_tokens == 0
    assert result.usage.cache_creation_input_tokens == 0


# ---- message-level cache markers (cache_prefix_chars) -----------------------


async def test_cache_prefix_chars_splits_message_into_two_blocks() -> None:
    """The marker splits content at exactly the byte boundary: block 0
    carries cache_control, block 1 is the remainder, and concatenation
    is identical to the original content (marker, not a prompt change)."""
    client, create_mock = _client_with_mocked_sdk(_fake_response())
    content = "STATIC target context\nDYNAMIC batch of titles"
    prefix_len = len("STATIC target context")
    await client.complete(
        model="claude-haiku-4-5",
        system="sys",
        messages=[Message(role="user", content=content, cache_prefix_chars=prefix_len)],
        purpose="test",
    )
    (msg,) = create_mock.call_args.kwargs["messages"]
    blocks = msg["content"]
    assert isinstance(blocks, list)
    assert len(blocks) == 2
    assert blocks[0] == {
        "type": "text",
        "text": "STATIC target context",
        "cache_control": {"type": "ephemeral"},
    }
    assert blocks[1] == {"type": "text", "text": "\nDYNAMIC batch of titles"}
    assert blocks[0]["text"] + blocks[1]["text"] == content


async def test_no_cache_prefix_keeps_plain_string_content() -> None:
    client, create_mock = _client_with_mocked_sdk(_fake_response())
    await client.complete(
        model="claude-haiku-4-5",
        system="sys",
        messages=[Message(role="user", content="plain")],
        purpose="test",
    )
    (msg,) = create_mock.call_args.kwargs["messages"]
    assert msg["content"] == "plain"


async def test_cache_prefix_covering_whole_message_uses_single_block() -> None:
    client, create_mock = _client_with_mocked_sdk(_fake_response())
    await client.complete(
        model="claude-haiku-4-5",
        system="sys",
        messages=[Message(role="user", content="all static", cache_prefix_chars=999)],
        purpose="test",
    )
    (msg,) = create_mock.call_args.kwargs["messages"]
    assert msg["content"] == [
        {
            "type": "text",
            "text": "all static",
            "cache_control": {"type": "ephemeral"},
        }
    ]


async def test_cache_prefix_chars_applies_to_tool_use_path() -> None:
    client = AnthropicLLMClient(api_key="test-key")
    create_mock = AsyncMock(
        return_value=_fake_tool_use_response(tool_name="grade", tool_input={"ok": True})
    )
    client._client.messages.create = create_mock  # type: ignore[method-assign]
    await client.complete_tool_use(
        model="claude-sonnet-4-6",
        system="sys",
        messages=[Message(role="user", content="AB", cache_prefix_chars=1)],
        tool_name="grade",
        tool_description="d",
        tool_input_schema={"type": "object"},
        purpose="test",
    )
    (msg,) = create_mock.call_args.kwargs["messages"]
    assert msg["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert msg["content"][0]["text"] + msg["content"][1]["text"] == "AB"


async def test_temperature_forwarded_to_sdk_when_set() -> None:
    client, create_mock = _client_with_mocked_sdk(
        _fake_tool_use_response(tool_name="return_X", tool_input={})
    )
    await client.complete_tool_use(
        model="claude-sonnet-4-6",
        system="s",
        messages=[Message(role="user", content="x")],
        tool_name="return_X",
        tool_description="d",
        tool_input_schema={"type": "object", "properties": {}},
        purpose="test",
        temperature=0.0,
    )
    assert create_mock.call_args.kwargs["temperature"] == 0.0


async def test_temperature_omitted_when_none() -> None:
    """``None`` keeps the provider default — we must not send temperature=None."""
    client, create_mock = _client_with_mocked_sdk(
        _fake_tool_use_response(tool_name="return_X", tool_input={})
    )
    await client.complete_tool_use(
        model="claude-sonnet-4-6",
        system="s",
        messages=[Message(role="user", content="x")],
        tool_name="return_X",
        tool_description="d",
        tool_input_schema={"type": "object", "properties": {}},
        purpose="test",
    )
    assert "temperature" not in create_mock.call_args.kwargs


async def test_complete_json_pins_temperature_to_zero() -> None:
    """The structured-output path (grading / triage / derive / learner) is
    deterministic by default — complete_json pins temperature to 0 (#47)."""
    from pydantic import BaseModel

    from app.services.llm.client import complete_json

    class _Thing(BaseModel):
        name: str

    client, create_mock = _client_with_mocked_sdk(
        _fake_tool_use_response(tool_name="return__Thing", tool_input={"name": "x"})
    )
    await complete_json(
        client,
        model="claude-sonnet-4-6",
        system="s",
        messages=[Message(role="user", content="x")],
        schema=_Thing,
        purpose="test",
    )
    assert create_mock.call_args.kwargs["temperature"] == 0.0


# ---- provider-reported cost vs the static table (#933) ----------------------
#
# `OpenRouterLLMClient` subclasses this client, so Claude models reached via
# OpenRouter come back through the Anthropic-shaped path here. OpenRouter adds
# `cost` to the usage object; the SDK types only Anthropic's own fields, so it
# lands in pydantic's `model_extra`. Shape verified against a live response:
#
#   usage.model_extra == {"speed": "standard", "cost": 0.000824,
#                         "is_byok": False, "cost_details": {...}}
#
# Direct api.anthropic.com has no such field, so the table must remain the
# fallback there.


def _usage_extra(cost: float) -> dict[str, Any]:
    return {
        "speed": "standard",
        "cost": cost,
        "is_byok": False,
        "cost_details": {"upstream_inference_cost": cost},
    }


# Deliberately NOT what the table produces for these tokens. That is the whole
# point of #933: OpenRouter picks an endpoint per call, so the billed rate is
# a routing outcome. A run routed somewhere pricier than the fallback rate
# bills more, and only the reported figure knows it.
_ROUTED_COST = 0.0031
_OPENROUTER_USAGE_EXTRA: dict[str, Any] = _usage_extra(_ROUTED_COST)
_USAGE_664_32 = LLMUsage(input_tokens=664, output_tokens=32)
# The extras the SDK leaves on an accumulated streamed message: no cost.
_NO_COST_EXTRA: dict[str, Any] = {"speed": "standard"}


async def test_complete_records_the_cost_openrouter_reported() -> None:
    client, _ = _client_with_mocked_sdk(
        _fake_response(input_tokens=664, output_tokens=32, usage_extra=_OPENROUTER_USAGE_EXTRA)
    )
    # Anti-vacuous: a cost really IS present, and it really does differ from
    # what the table would have produced (that gap IS the bug).
    assert "cost" in _OPENROUTER_USAGE_EXTRA
    assert calculate_cost("claude-haiku-4-5", _USAGE_664_32) != pytest.approx(_ROUTED_COST)

    result = await client.complete(
        model="claude-haiku-4-5",
        system="sys",
        messages=[Message(role="user", content="x")],
        purpose="test",
    )
    assert result.cost_usd == pytest.approx(_ROUTED_COST)
    assert result.cost_source == "reported"


async def test_complete_falls_back_to_the_table_without_a_reported_cost() -> None:
    """Direct api.anthropic.com sends no cost — the table must still work."""
    response = _fake_response(input_tokens=664, output_tokens=32)
    # Anti-vacuous: prove the fixture really carries NO usable cost before
    # asserting which branch ran.
    assert reported_cost_usd(_reported_usage(response.usage)) is None

    client, _ = _client_with_mocked_sdk(response)
    result = await client.complete(
        model="claude-haiku-4-5",
        system="sys",
        messages=[Message(role="user", content="x")],
        purpose="test",
    )
    assert result.cost_source == "estimated"
    assert result.cost_usd == pytest.approx(calculate_cost("claude-haiku-4-5", _USAGE_664_32))


async def test_complete_tool_use_records_the_reported_cost() -> None:
    """The tool-forced path is the one grading/triage actually uses."""
    client, _ = _client_with_mocked_sdk(
        _fake_tool_use_response(
            tool_name="return_Thing",
            tool_input={"name": "x"},
            input_tokens=664,
            output_tokens=32,
            usage_extra=_OPENROUTER_USAGE_EXTRA,
        )
    )
    assert calculate_cost("claude-haiku-4-5", _USAGE_664_32) != pytest.approx(_ROUTED_COST)

    _, result = await client.complete_tool_use(
        model="claude-haiku-4-5",
        system="sys",
        messages=[Message(role="user", content="x")],
        tool_name="return_Thing",
        tool_description="d",
        tool_input_schema={"type": "object"},
        purpose="test",
    )
    assert result.cost_usd == pytest.approx(_ROUTED_COST)
    assert result.cost_source == "reported"


async def test_complete_tool_use_falls_back_to_the_table() -> None:
    response = _fake_tool_use_response(
        tool_name="return_Thing", tool_input={"name": "x"}, input_tokens=664, output_tokens=32
    )
    assert reported_cost_usd(_reported_usage(response.usage)) is None

    client, _ = _client_with_mocked_sdk(response)
    _, result = await client.complete_tool_use(
        model="claude-haiku-4-5",
        system="sys",
        messages=[Message(role="user", content="x")],
        tool_name="return_Thing",
        tool_description="d",
        tool_input_schema={"type": "object"},
        purpose="test",
    )
    assert result.cost_source == "estimated"
    assert result.cost_usd == pytest.approx(calculate_cost("claude-haiku-4-5", _USAGE_664_32))


# ---- streaming: the SDK drops the cost, so read it off the event ------------


def _text_delta_event(text: str) -> Any:
    event = MagicMock()
    event.type = "content_block_delta"
    event.delta.type = "text_delta"
    event.delta.text = text
    return event


def _message_delta_event(usage_extra: dict[str, Any] | None) -> Any:
    event = MagicMock()
    event.type = "message_delta"
    event.usage.model_extra = usage_extra if usage_extra is not None else MagicMock()
    return event


class _FakeStream:
    """Stands in for the SDK's AsyncMessageStream.

    Reproduces the MEASURED behaviour: OpenRouter puts `cost` on the
    message_delta event, and the SDK's accumulated final message DROPS it
    (`final.usage.model_extra` comes back as `{"speed": "standard"}` alone).
    So the final message here deliberately carries no cost.
    """

    def __init__(self, events: list[Any], final: Any) -> None:
        self._events = events
        self._final = final

    async def __aenter__(self) -> _FakeStream:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def __aiter__(self) -> AsyncIterator[Any]:
        for event in self._events:
            yield event

    async def get_final_message(self) -> Any:
        return self._final


def _stream_client(events: list[Any], final: Any) -> AnthropicLLMClient:
    client = AnthropicLLMClient(api_key="test-key")
    client._client.messages.stream = MagicMock(  # type: ignore[method-assign]
        return_value=_FakeStream(events, final)
    )
    return client


async def _drain(client: AnthropicLLMClient) -> tuple[str, Any]:
    text: list[str] = []
    final: Any = None
    async for event in client.stream(
        model="claude-haiku-4-5",
        system="sys",
        messages=[Message(role="user", content="x")],
        purpose="test",
    ):
        if event.type == "delta":
            text.append(event.text)
        else:
            final = event.result
    return "".join(text), final


async def test_stream_reads_the_cost_off_the_message_delta_event() -> None:
    """The accumulated final message loses OpenRouter's extras, so a client
    that only looked there would silently estimate every streamed call."""
    final = _fake_response(text="ignored", input_tokens=664, output_tokens=32)
    final.usage.model_extra = _NO_COST_EXTRA  # exactly what the SDK leaves

    # Anti-vacuous, both directions: the final message really has NO cost, and
    # the message_delta event really HAS one that differs from the estimate.
    assert reported_cost_usd(_reported_usage(final.usage)) is None
    assert _OPENROUTER_USAGE_EXTRA["cost"] == _ROUTED_COST
    assert calculate_cost("claude-haiku-4-5", _USAGE_664_32) != pytest.approx(_ROUTED_COST)

    client = _stream_client(
        [
            _text_delta_event("Hel"),
            _text_delta_event("lo"),
            _message_delta_event(_OPENROUTER_USAGE_EXTRA),
        ],
        final,
    )
    text, result = await _drain(client)

    assert text == "Hello"  # text streaming is unchanged by the cost hook
    assert result.cost_usd == pytest.approx(_ROUTED_COST)
    assert result.cost_source == "reported"


async def test_stream_without_a_reported_cost_uses_the_table() -> None:
    final = _fake_response(text="ignored", input_tokens=664, output_tokens=32)
    final.usage.model_extra = _NO_COST_EXTRA

    client = _stream_client([_text_delta_event("Hi"), _message_delta_event(_NO_COST_EXTRA)], final)
    text, result = await _drain(client)

    assert text == "Hi"
    assert result.cost_source == "estimated"
    assert result.cost_usd == pytest.approx(calculate_cost("claude-haiku-4-5", _USAGE_664_32))
