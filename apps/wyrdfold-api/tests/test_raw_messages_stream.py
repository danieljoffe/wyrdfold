"""The raw ``/v1/messages`` stream (#1067 PR C)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.models.llm import Message
from app.services.llm import openrouter_http
from app.services.llm import raw_messages_transport as raw
from app.services.llm.errors import (
    LLMRateLimitedError,
    LLMRequestRejectedError,
    LLMUpstreamUnavailableError,
)
from app.services.llm.messages_transport import StreamFinal, StreamTextDelta, StreamUsageDelta
from app.services.llm.openrouter_client import OpenRouterLLMClient
from tests.support.messages_wire import (
    WireCapture,
    fixture_sse,
    json_response,
    mock_http,
    sse_frames,
    sse_response,
    wire_error,
)

_PARAMS: dict[str, Any] = {
    "model": "anthropic/claude-sonnet-4.6",
    "max_tokens": 24,
    "system": [{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}],
    "messages": [{"role": "user", "content": "Say hello in five words."}],
}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    delays: list[float] = []

    async def _record(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(openrouter_http, "_sleep", _record)
    return delays


def _transport(
    *steps: Any, capture: WireCapture | None = None, max_retries: int = 0
) -> raw.RawMessagesTransport:
    return raw.RawMessagesTransport(
        api_key="test-key",
        timeout=5.0,
        max_retries=max_retries,
        base_url="https://openrouter.ai/api",
        http=mock_http(*steps, capture=capture),
    )


async def _collect(t: raw.RawMessagesTransport) -> list[Any]:
    return [e async for e in t.stream(**_PARAMS)]


# ---- replaying the recorded corpus -----------------------------------------


async def test_replays_the_recorded_cache_create_stream() -> None:
    resp, _ = sse_response(fixture_sse("stream_cache_create.sse"))
    cap = WireCapture()
    events = await _collect(_transport(resp, capture=cap))
    assert cap.requests[0][1]["stream"] is True
    assert [e.text for e in events if isinstance(e, StreamTextDelta)] == [
        "Hello",
        " there",
        ",",
        " how",
        " are you?",
    ]
    usage_deltas = [e for e in events if isinstance(e, StreamUsageDelta)]
    assert len(usage_deltas) == 1 and usage_deltas[0].reported is not None
    assert {"cost", "cost_details", "is_byok"} <= set(usage_deltas[0].reported)
    final = events[-1]
    assert isinstance(final, StreamFinal)
    assert final.message.content[0].text == "Hello there, how are you?"
    assert final.message.stop_reason == "end_turn"
    assert final.message.provider == "Amazon Bedrock"
    assert final.message.usage.input_tokens == 12
    assert final.message.usage.output_tokens == 10
    assert final.message.usage.cache_creation_input_tokens == 2403
    assert final.message.usage.cache_read_input_tokens == 0
    # The final usage carries the message_delta extras (cost lives there).
    assert final.message.usage.reported is not None and "cost" in final.message.usage.reported


async def test_replays_the_recorded_cache_read_stream() -> None:
    resp, _ = sse_response(fixture_sse("stream_cache_read.sse"))
    final = (await _collect(_transport(resp)))[-1]
    assert isinstance(final, StreamFinal)
    assert final.message.usage.cache_read_input_tokens == 2403
    assert final.message.usage.cache_creation_input_tokens == 0


# ---- frame handling ---------------------------------------------------------


def _ok_frames(*, with_stop: bool = True, trailer: bool = True) -> bytes:
    frames: list[tuple[str, Any]] = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "usage": {
                        "input_tokens": 3,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                    },
                    "provider": "P",
                },
            },
        ),
        ("ping", {"type": "ping"}),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "hi"},
            },
        ),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"input_tokens": 3, "output_tokens": 1, "cost": 0.001234},
            },
        ),
    ]
    if with_stop:
        frames.append(("message_stop", {"type": "message_stop"}))
    if trailer:
        frames.append(("data", "[DONE]"))
    return sse_frames(*frames)


async def test_ping_and_done_trailer_are_handled_and_final_usage_is_merged() -> None:
    resp, _ = sse_response(_ok_frames())
    events = await _collect(_transport(resp))
    assert events[0] == StreamTextDelta(text="hi")
    final = events[-1]
    assert isinstance(final, StreamFinal)
    assert final.message.usage.input_tokens == 3
    assert final.message.usage.output_tokens == 1
    assert final.message.provider == "P"
    assert final.message.stop_reason == "end_turn"


async def test_stream_without_message_stop_is_upstream_loss() -> None:
    resp, _ = sse_response(_ok_frames(with_stop=False, trailer=False))
    with pytest.raises(LLMUpstreamUnavailableError):
        await _collect(_transport(resp))


@pytest.mark.parametrize(
    ("etype", "expected"),
    [
        ("overloaded_error", LLMUpstreamUnavailableError),
        ("api_error", LLMUpstreamUnavailableError),
        ("rate_limit_error", LLMRateLimitedError),
        ("invalid_request_error", LLMRequestRejectedError),
        ("something_new", LLMRequestRejectedError),
    ],
)
async def test_mid_stream_error_frames_map_to_the_typed_hierarchy(
    etype: str, expected: type[Exception]
) -> None:
    body = sse_frames(
        ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 3}}}),
        (
            "content_block_delta",
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "par"}},
        ),
        ("error", {"type": "error", "error": {"type": etype, "message": "ZZ_VENDOR_TEXT"}}),
    )
    resp, stream = sse_response(body)
    t = _transport(resp)
    got: list[Any] = []
    with pytest.raises(expected) as excinfo:
        async for e in t.stream(**_PARAMS):
            got.append(e)
    assert got == [StreamTextDelta(text="par")]
    assert stream.closed
    if isinstance(excinfo.value, LLMRequestRejectedError):
        assert "ZZ_VENDOR_TEXT" in excinfo.value.diagnostic
        assert "ZZ_VENDOR_TEXT" not in excinfo.value.user_message
        if etype == "something_new":
            assert excinfo.value.reason == "unclassified_error_envelope"


async def test_consumer_cancellation_closes_the_upstream_stream() -> None:
    resp, stream = sse_response(fixture_sse("stream_cache_create.sse"), chunk_size=16)
    t = _transport(resp)
    gen = t.stream(**_PARAMS)
    first = await gen.__anext__()
    assert isinstance(first, StreamTextDelta)
    assert not stream.closed
    await gen.aclose()
    assert stream.closed


async def test_mid_stream_transport_error_is_upstream_loss() -> None:
    class _Breaking(httpx.AsyncByteStream):
        async def __aiter__(self) -> Any:
            yield b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":1}}}\n\n'
            raise httpx.ReadTimeout("stalled")

        async def aclose(self) -> None:
            return None

    resp = httpx.Response(200, stream=_Breaking(), headers={"content-type": "text/event-stream"})
    with pytest.raises(LLMUpstreamUnavailableError):
        await _collect(_transport(resp))


# ---- handshake --------------------------------------------------------------


async def test_handshake_retries_transient_then_streams(_no_sleep: list[float]) -> None:
    ok, _ = sse_response(_ok_frames())
    t = _transport(
        json_response(429, wire_error(429, "slow"), headers={"Retry-After": "1"}),
        ok,
        max_retries=1,
    )
    events = await _collect(t)
    assert isinstance(events[-1], StreamFinal)
    assert _no_sleep == [1.0]


async def test_handshake_transport_error_retries_are_logged_at_warning(
    _no_sleep: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    ok, _ = sse_response(_ok_frames())
    t = _transport(httpx.ConnectError("refused"), ok, max_retries=1)
    with caplog.at_level("WARNING", logger="app.services.llm.raw_messages_transport"):
        events = await _collect(t)
    assert isinstance(events[-1], StreamFinal)
    hits = [
        r
        for r in caplog.records
        if "openrouter transport error ConnectError on stream handshake" in r.getMessage()
    ]
    assert len(hits) == 1 and hits[0].levelname == "WARNING"
    assert "attempt=1/2" in hits[0].getMessage()


async def test_handshake_status_retries_are_logged_at_warning(
    _no_sleep: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    """Review of #1077, round 2: the handshake's retryable-STATUS branch must
    log the same way the POST helper does, not only its transport-error branch."""
    ok, _ = sse_response(_ok_frames())
    t = _transport(
        json_response(429, wire_error(429, "slow"), headers={"Retry-After": "1"}), ok, max_retries=1
    )
    with caplog.at_level("WARNING", logger="app.services.llm.raw_messages_transport"):
        events = await _collect(t)
    assert isinstance(events[-1], StreamFinal)
    hits = [
        r
        for r in caplog.records
        if "openrouter transient status=429 on stream handshake" in r.getMessage()
    ]
    assert len(hits) == 1 and hits[0].levelname == "WARNING"
    assert "attempt=1/2" in hits[0].getMessage()
    assert "retrying in 1.00s" in hits[0].getMessage()
    assert "slow" not in hits[0].getMessage()  # no response-body content


async def test_handshake_rejection_is_a_server_fault() -> None:
    t = _transport(json_response(400, wire_error(400, "ZZ_VENDOR_TEXT")))
    with pytest.raises(LLMRequestRejectedError) as excinfo:
        await _collect(t)
    assert excinfo.value.upstream_code == 400
    assert "ZZ_VENDOR_TEXT" not in excinfo.value.user_message


async def test_exhausted_handshake_429_stays_rate_limited(_no_sleep: list[float]) -> None:
    t = _transport(json_response(429, wire_error(429, "x")), max_retries=0)
    with pytest.raises(LLMRateLimitedError):
        await _collect(t)


# ---- through the client -----------------------------------------------------


async def test_listed_stream_purpose_goes_raw_and_stamps_provenance() -> None:
    client = OpenRouterLLMClient(api_key="test-key", raw_purposes=frozenset({"p.stream"}))
    resp, _ = sse_response(fixture_sse("stream_cache_create.sse"))
    client._raw = _transport(resp)
    deltas: list[str] = []
    final = None
    async for event in client.stream(
        model="claude-sonnet-4-6",
        system="sys",
        messages=[Message(role="user", content="Say hello in five words.")],
        purpose="p.stream",
        max_tokens=24,
        cache_system=True,
    ):
        if event.type == "delta":
            deltas.append(event.text)
        else:
            final = event.result
    assert "".join(deltas) == "Hello there, how are you?"
    assert final is not None
    assert final.content == "Hello there, how are you?"
    assert final.transport == "messages_http"
    assert final.provider == "Amazon Bedrock"
    assert final.cost_source == "reported"
    assert final.usage.cache_creation_input_tokens == 2403
    assert final.usage.output_tokens == 10


def test_router_no_longer_pins_stream_to_the_sdk() -> None:
    client = OpenRouterLLMClient(api_key="k", raw_purposes=frozenset({"p"}))
    assert isinstance(client._transport_for("p", "stream"), raw.RawMessagesTransport)
    assert client._transport_for("q", "stream") is client._transport


# ---- release gate 2026-09-18 -------------------------------------------------


async def test_client_level_cancellation_closes_the_transport_synchronously() -> None:
    """The derive route closes the CLIENT generator on disconnect; the
    transport generator underneath must be closed in that same await, not by
    the event loop's async-generator finalizer a tick or two later."""
    resp, stream = sse_response(fixture_sse("stream_cache_create.sse"), chunk_size=16)
    client = OpenRouterLLMClient(api_key="k", raw_purposes=frozenset({"p"}))
    client._raw = _transport(resp)
    gen = client.stream(
        model="claude-sonnet-4-6",
        system="s",
        messages=[Message(role="user", content="x")],
        purpose="p",
    )
    first = await gen.__anext__()
    assert first.type == "delta"
    assert not stream.closed
    await gen.aclose()
    assert stream.closed  # immediately, with no further loop iteration


async def test_a_failing_close_does_not_mask_the_typed_stream_error() -> None:
    body = sse_frames(
        ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 3}}}),
        ("error", {"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}}),
    )
    resp, stream = sse_response(body)

    async def _boom() -> None:
        raise httpx.ReadError("close failed")

    stream.aclose = _boom  # type: ignore[method-assign]
    with pytest.raises(LLMRateLimitedError):
        await _collect(_transport(resp))
