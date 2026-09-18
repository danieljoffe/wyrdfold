"""The Messages-API transport seam (#1067, PR A).

Proves the three properties PR A exists for: the client no longer speaks to
``anthropic`` directly (the seam), the SDK is constructed lazily so a client
whose purposes are routed elsewhere never builds it (composition), and every
result carries typed transport provenance that ``cost_log`` merges into the
row (reconciliation from the ledger).
"""

from __future__ import annotations

import ast
import pathlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from anthropic import APIConnectionError, APIStatusError

from app.models.llm import LLMResult, LLMUsage, Message
from app.services.llm import cost_log
from app.services.llm import messages_transport as mt
from app.services.llm.anthropic_client import AnthropicLLMClient
from app.services.llm.errors import LLMQuotaExhaustedError, LLMUpstreamUnavailableError
from app.services.llm.mock import MockLLMClient
from app.services.llm.openrouter_client import OpenRouterLLMClient
from tests.support.sdk_fakes import tool_use_response, validating_create_mock

# ---- the seam ---------------------------------------------------------------


def test_anthropic_client_module_imports_nothing_from_the_sdk() -> None:
    """Item 2 of #1067: the client speaks to a transport, never to ``anthropic``.
    A future edit that reaches for the SDK directly reopens the drift surface
    this seam closes; this test is the tripwire."""
    src = pathlib.Path(mt.__file__).with_name("anthropic_client.py").read_text()
    offenders: list[str] = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "anthropic":
            offenders.append(f"from {node.module} import ...")
        if isinstance(node, ast.Import):
            offenders += [a.name for a in node.names if a.name.split(".")[0] == "anthropic"]
    assert offenders == []


def test_sdk_client_is_constructed_lazily_and_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Constructing a client must not construct the SDK; the first call does,
    with exactly the kwargs the old eager constructor used."""
    spy = MagicMock(name="AsyncAnthropic")
    monkeypatch.setattr(mt, "AsyncAnthropic", spy)

    via_openrouter = OpenRouterLLMClient(api_key="sk-or-x")
    direct = AnthropicLLMClient(api_key="sk-ant-x", timeout=5.0, max_retries=1)
    assert spy.call_count == 0

    _ = via_openrouter._client
    assert spy.call_count == 1
    assert spy.call_args.kwargs == {
        "api_key": "sk-or-x",
        "timeout": 600.0,
        "max_retries": 3,
        "base_url": "https://openrouter.ai/api",
    }
    _ = via_openrouter._client  # cached, not rebuilt
    assert spy.call_count == 1

    _ = direct._client
    assert spy.call_count == 2
    assert spy.call_args.kwargs == {"api_key": "sk-ant-x", "timeout": 5.0, "max_retries": 1}


def test_client_refuses_to_expose_an_sdk_it_does_not_have() -> None:
    class _NotTheSdk:
        transport_id = "messages_http"

        async def create(self, **params: Any) -> mt.MessagesResponse:  # pragma: no cover
            raise NotImplementedError

        def stream(self, **params: Any) -> Any:  # pragma: no cover
            raise NotImplementedError

    client = AnthropicLLMClient(transport=_NotTheSdk())
    with pytest.raises(AttributeError, match="not the Anthropic SDK"):
        _ = client._client


# ---- normalisation of SDK-shaped objects -----------------------------------


def test_response_normalisation_from_sdk_shaped_objects() -> None:
    resp = tool_use_response({"ok": True}, tool_name="grade", input_tokens=7, output_tokens=3)
    resp.stop_reason = "tool_use"
    out = mt._response_from_sdk(resp)
    assert out.stop_reason == "tool_use"
    assert out.content == [mt.ContentBlock(type="tool_use", name="grade", input={"ok": True})]
    assert out.usage.input_tokens == 7
    assert out.usage.output_tokens == 3
    assert out.usage.cache_read_input_tokens == 0
    # ``model_extra`` on the double is a Mock, not a Mapping: no extras.
    assert out.usage.reported is None


def test_usage_extras_only_when_a_real_mapping_and_none_reads_as_zero() -> None:
    usage = MagicMock()
    usage.input_tokens = 1
    usage.output_tokens = 2
    usage.cache_read_input_tokens = None  # the SDK omits it on some responses
    usage.cache_creation_input_tokens = 4
    usage.model_extra = {"cost": 0.5, "is_byok": False}
    u = mt._usage_from_sdk(usage)
    assert u == mt.MessagesUsage(1, 2, 0, 4, reported={"cost": 0.5, "is_byok": False})


def test_blocks_of_unknown_type_and_missing_text_are_safe() -> None:
    thinking = MagicMock()
    thinking.type = "thinking"
    text = MagicMock()
    text.type = "text"
    del text.text  # a text block with no ``text`` attribute at all
    weird = MagicMock()
    weird.type = 42  # not a string
    out = mt._response_from_sdk(MagicMock(content=[thinking, text, weird], stop_reason=None))
    assert [b.type for b in out.content] == ["thinking", "text", "unknown"]
    assert out.content[1].text == ""
    assert out.stop_reason is None


# ---- the SDK transport ------------------------------------------------------


def _transport() -> mt.SdkMessagesTransport:
    return mt.SdkMessagesTransport(api_key="k", timeout=1.0, max_retries=0, base_url=None)


def _status_error(status: int) -> APIStatusError:
    request = httpx.Request("POST", "https://example.test/v1/messages")
    return APIStatusError("boom", response=httpx.Response(status, request=request), body=None)


async def test_create_translates_status_and_connection_errors() -> None:
    t = _transport()
    params: dict[str, Any] = {"model": "m", "max_tokens": 1, "system": "", "messages": []}

    t.sdk_client.messages.create = AsyncMock(side_effect=_status_error(402))  # type: ignore[method-assign]
    with pytest.raises(LLMQuotaExhaustedError):
        await t.create(**params)

    request = httpx.Request("POST", "https://example.test/v1/messages")
    t.sdk_client.messages.create = AsyncMock(side_effect=APIConnectionError(request=request))  # type: ignore[method-assign]
    with pytest.raises(LLMUpstreamUnavailableError):
        await t.create(**params)

    # A 400 is a bug in our request: re-raised untranslated so it stays a 500.
    t.sdk_client.messages.create = AsyncMock(side_effect=_status_error(400))  # type: ignore[method-assign]
    with pytest.raises(APIStatusError):
        await t.create(**params)


async def test_create_binds_kwargs_against_the_installed_sdk_signature() -> None:
    """The #1065 guard survives the seam: the transport passes params to the
    SDK verbatim, so the signature-validating fake still rejects a stale kwarg."""
    t = _transport()
    t.sdk_client.messages.create = validating_create_mock(tool_use_response({"a": 1}))  # type: ignore[method-assign]
    out = await t.create(
        model="claude-sonnet-4-6",
        max_tokens=8,
        system="s",
        messages=[{"role": "user", "content": "x"}],
    )
    assert out.content[0].input == {"a": 1}
    with pytest.raises(TypeError):
        await t.create(model="claude-sonnet-4-6", max_tokens=8, messages=[], temperature=0.0)


class _FakeStream:
    def __init__(self, events: list[Any], final: Any) -> None:
        self._events = events
        self._final = final

    async def __aenter__(self) -> _FakeStream:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    def __aiter__(self) -> Any:
        return self._gen()

    async def _gen(self) -> Any:
        for e in self._events:
            yield e

    async def get_final_message(self) -> Any:
        return self._final


async def test_stream_normalises_events_and_final_message() -> None:
    delta = MagicMock()
    delta.type = "content_block_delta"
    delta.delta.type = "text_delta"
    delta.delta.text = "hi"
    empty = MagicMock()
    empty.type = "content_block_delta"
    empty.delta.type = "text_delta"
    empty.delta.text = ""
    start = MagicMock()
    start.type = "content_block_start"
    md = MagicMock()
    md.type = "message_delta"
    md.usage.model_extra = {"cost": 0.01}
    final = MagicMock()
    tb = MagicMock()
    tb.type = "text"
    tb.text = "hi"
    final.content = [tb]
    final.stop_reason = "end_turn"
    final.usage.input_tokens = 3
    final.usage.output_tokens = 1
    final.usage.cache_read_input_tokens = 0
    final.usage.cache_creation_input_tokens = 0
    final.usage.model_extra = {"speed": "standard"}

    t = _transport()
    t.sdk_client.messages.stream = MagicMock(
        return_value=_FakeStream([delta, empty, start, md], final)
    )  # type: ignore[method-assign]
    events = [e async for e in t.stream(model="m", max_tokens=1, system="", messages=[])]
    assert events == [
        mt.StreamTextDelta(text="hi"),
        mt.StreamUsageDelta(reported={"cost": 0.01}),
        mt.StreamFinal(
            message=mt.MessagesResponse(
                content=[mt.ContentBlock(type="text", text="hi")],
                stop_reason="end_turn",
                usage=mt.MessagesUsage(3, 1, 0, 0, reported={"speed": "standard"}),
            )
        ),
    ]


async def test_stream_handshake_failure_is_translated() -> None:
    t = _transport()
    t.sdk_client.messages.stream = MagicMock(side_effect=_status_error(503))  # type: ignore[method-assign]
    with pytest.raises(LLMUpstreamUnavailableError):
        _ = [e async for e in t.stream(model="m", max_tokens=1, system="", messages=[])]


async def test_client_treats_a_stream_without_a_final_frame_as_upstream_loss() -> None:
    class _Truncated:
        transport_id = "messages_http"

        async def create(self, **params: Any) -> mt.MessagesResponse:  # pragma: no cover
            raise NotImplementedError

        async def stream(self, **params: Any) -> Any:
            yield mt.StreamTextDelta(text="partial")

    client = AnthropicLLMClient(transport=_Truncated())
    gen = client.stream(
        model="claude-sonnet-4-6",
        system="s",
        messages=[Message(role="user", content="x")],
        purpose="p",
    )
    first = await gen.__anext__()
    assert first.type == "delta"
    with pytest.raises(LLMUpstreamUnavailableError):
        await gen.__anext__()


# ---- provenance -------------------------------------------------------------


async def test_every_result_type_carries_its_transport() -> None:
    client = AnthropicLLMClient(api_key="k")
    client._client.messages.create = validating_create_mock(tool_use_response({"a": 1}))
    _, via_sdk = await client.complete_tool_use(
        model="claude-sonnet-4-6",
        system="s",
        messages=[Message(role="user", content="x")],
        tool_name="return_X",
        tool_description="d",
        tool_input_schema={"type": "object"},
        purpose="p",
    )
    assert via_sdk.transport == "anthropic_sdk"

    mock = MockLLMClient(scripted={"p": '{"a": 1}'})
    _, via_mock = await mock.complete_tool_use(
        model="claude-haiku-4-5",
        system="s",
        messages=[Message(role="user", content="x")],
        tool_name="return_X",
        tool_description="d",
        tool_input_schema={"type": "object"},
        purpose="p",
    )
    assert via_mock.transport == "mock"
    plain = await mock.complete(
        model="claude-haiku-4-5",
        system="s",
        messages=[Message(role="user", content="x")],
        purpose="p",
    )
    assert plain.transport == "mock"


def test_cost_log_merges_transport_into_every_row() -> None:
    def _result(transport: Any) -> LLMResult:
        return LLMResult(
            content="",
            model="claude-haiku-4-5",
            usage=LLMUsage(),
            cost_usd=0.0,
            latency_ms=0,
            transport=transport,
        )

    stamped = cost_log._row_for(
        user_id="u", purpose="p", result=_result("anthropic_sdk"), metadata={"k": "v"}
    )
    assert stamped["metadata"] == {
        "k": "v",
        "cost_source": "estimated",
        "transport": "anthropic_sdk",
    }
    # A result built without the field (pre-#1067 constructors) is recorded as
    # unknown rather than silently claiming a transport.
    legacy = cost_log._row_for(user_id="u", purpose="p", result=_result(None), metadata=None)
    assert legacy["metadata"] == {"cost_source": "estimated", "transport": "unknown"}
