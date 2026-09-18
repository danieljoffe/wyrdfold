"""The raw ``/v1/messages`` transport and its rollout routing (#1067 PR B)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from app.config import settings
from app.models.llm import LLMResult, LLMUsage, Message
from app.services.llm import cost_log, openrouter_http
from app.services.llm import raw_messages_transport as raw
from app.services.llm.errors import (
    LLMAuthError,
    LLMQuotaExhaustedError,
    LLMRateLimitedError,
    LLMRequestRejectedError,
    LLMUpstreamUnavailableError,
)
from app.services.llm.openrouter_client import OpenRouterLLMClient
from tests.support.messages_wire import (
    MESSAGES_URL,
    WireCapture,
    fixture,
    json_response,
    mock_http,
    wire_error,
    wire_text,
    wire_tool_use,
)
from tests.support.sdk_fakes import tool_use_response, validating_create_mock

_PARAMS: dict[str, Any] = {
    "model": "anthropic/claude-sonnet-4.6",
    "max_tokens": 64,
    "system": [{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}],
    "messages": [{"role": "user", "content": "x"}],
    "tools": [
        {
            "name": "return_NormalizedTitle",
            "description": "d",
            "input_schema": {
                "type": "object",
                "properties": {"label": {"$ref": "#/$defs/Label"}},
                "$defs": {"Label": {"type": "string"}},
            },
        }
    ],
    "tool_choice": {"type": "tool", "name": "return_NormalizedTitle"},
}


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


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    delays: list[float] = []

    async def _record(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(openrouter_http, "_sleep", _record)
    return delays


# ---- parsing the recorded corpus -------------------------------------------


async def test_parses_the_recorded_tool_use_exchange() -> None:
    rec = fixture("tool_use_with_defs.json")
    t = _transport(json_response(rec["status"], rec["body"]))
    out = await t.create(**_PARAMS)
    assert out.stop_reason == "tool_use"
    assert out.content[0].type == "tool_use"
    assert out.content[0].name == "return_NormalizedTitle"
    assert out.content[0].input == {"label": "Senior Product Manager"}
    assert out.usage.input_tokens == 724
    assert out.usage.output_tokens == 35
    assert out.usage.cache_read_input_tokens == 0
    assert out.provider == "Amazon Bedrock"
    # The whole usage mapping is the reported extras: exactly the keys
    # pricing.reported_cost_usd reads on the SDK path today.
    assert out.usage.reported is not None
    assert {"cost", "cost_details", "is_byok"} <= set(out.usage.reported)


async def test_parses_the_recorded_text_exchange() -> None:
    rec = fixture("text_complete.json")
    t = _transport(json_response(rec["status"], rec["body"]))
    out = await t.create(
        model="m", max_tokens=32, system="s", messages=[{"role": "user", "content": "x"}]
    )
    assert out.content[0].type == "text"
    assert out.content[0].text and out.content[0].text.startswith("A résumé")
    assert out.stop_reason == "max_tokens"


@pytest.mark.parametrize("name", ["error_400_empty_messages.json", "error_400_unknown_model.json"])
async def test_recorded_400s_are_request_rejections(name: str) -> None:
    rec = fixture(name)
    t = _transport(json_response(rec["status"], rec["body"]))
    with pytest.raises(LLMRequestRejectedError) as excinfo:
        await t.create(**_PARAMS)
    exc = excinfo.value
    assert exc.upstream_code == 400
    assert exc.reason == "request_rejected"
    assert rec["body"]["error"]["message"][:30] in exc.diagnostic
    assert "invalid" not in exc.user_message.lower()


# ---- request shape ---------------------------------------------------------


async def test_wire_body_headers_and_untouched_defs(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = WireCapture()
    t = _transport(json_response(200, wire_tool_use({"label": "x"})), capture=cap)
    await t.create(**_PARAMS, extra_body={"temperature": 0.0})
    headers, body = cap.requests[0]
    assert headers["x-api-key"] == "test-key"
    assert headers["anthropic-version"] == raw.ANTHROPIC_VERSION
    assert set(body) == {
        "model",
        "max_tokens",
        "system",
        "messages",
        "tools",
        "tool_choice",
        "temperature",
    }
    assert body["temperature"] == 0.0
    assert "stream" not in body
    # $defs pass through verbatim: the SDK path never inlined them either.
    assert body["tools"][0]["input_schema"]["$defs"] == {"Label": {"type": "string"}}
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_unsupported_request_field_fails_loud() -> None:
    with pytest.raises(TypeError, match="unsupported request field"):
        raw.wire_body({"model": "m", "max_tokens": 1, "messages": [], "temperature": 0.0})


def test_posts_to_the_messages_url() -> None:
    t = raw.RawMessagesTransport(
        api_key="k", timeout=1.0, max_retries=0, base_url="https://openrouter.ai/api/"
    )
    assert t._url == MESSAGES_URL


# ---- status classification -------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (402, LLMQuotaExhaustedError),
        (401, LLMAuthError),
        (403, LLMAuthError),
        (429, LLMRateLimitedError),
        (503, LLMUpstreamUnavailableError),
        (529, LLMUpstreamUnavailableError),
    ],
)
async def test_provider_statuses_keep_their_typed_errors(
    status: int, expected: type[Exception]
) -> None:
    t = _transport(json_response(status, wire_error(status, "VENDOR_TEXT")))
    with pytest.raises(expected) as excinfo:
        await t.create(**_PARAMS)
    assert "VENDOR_TEXT" not in getattr(excinfo.value, "user_message", "")


@pytest.mark.parametrize("status", [400, 404, 422])
async def test_request_rejections_are_server_faults(status: int) -> None:
    t = _transport(json_response(status, wire_error(status, "VENDOR_TEXT")))
    with pytest.raises(LLMRequestRejectedError) as excinfo:
        await t.create(**_PARAMS)
    assert excinfo.value.upstream_code == status
    assert "VENDOR_TEXT" in excinfo.value.diagnostic
    assert "VENDOR_TEXT" not in excinfo.value.user_message


async def test_200_with_an_error_envelope_is_a_server_fault() -> None:
    t = _transport(json_response(200, wire_error(400, "ZZ_MARKER")))
    with pytest.raises(LLMRequestRejectedError) as excinfo:
        await t.create(**_PARAMS)
    assert excinfo.value.reason == "unclassified_error_envelope"
    assert "ZZ_MARKER" in excinfo.value.diagnostic


async def test_non_json_200_is_upstream_unavailable() -> None:
    t = _transport(httpx.Response(200, text="<html>gateway</html>"))
    with pytest.raises(LLMUpstreamUnavailableError):
        await t.create(**_PARAMS)


# ---- retry policy (shared with the OpenAI shape) ----------------------------


async def test_retries_transient_and_honours_retry_after(_no_sleep: list[float]) -> None:
    cap = WireCapture()
    t = _transport(
        json_response(429, wire_error(429, "slow down"), headers={"Retry-After": "2"}),
        json_response(200, wire_tool_use({"label": "x"})),
        capture=cap,
        max_retries=2,
    )
    out = await t.create(**_PARAMS)
    assert out.content[0].input == {"label": "x"}
    assert len(cap.requests) == 2
    assert _no_sleep == [2.0]


async def test_retry_after_is_clamped(_no_sleep: list[float]) -> None:
    t = _transport(
        json_response(503, wire_error(503, "x"), headers={"Retry-After": "86400"}),
        json_response(200, wire_text("ok")),
        max_retries=1,
    )
    await t.create(**_PARAMS)
    assert _no_sleep == [60.0]


async def test_exhausted_429_stays_rate_limited_and_5xx_stays_upstream(
    _no_sleep: list[float],
) -> None:
    t = _transport(
        json_response(429, wire_error(429, "x")),
        json_response(429, wire_error(429, "x")),
        max_retries=1,
    )
    with pytest.raises(LLMRateLimitedError):
        await t.create(**_PARAMS)
    assert len(_no_sleep) == 1
    t = _transport(json_response(502, wire_error(502, "x")), max_retries=0)
    with pytest.raises(LLMUpstreamUnavailableError):
        await t.create(**_PARAMS)


async def test_409_is_transient_like_the_sdk(_no_sleep: list[float]) -> None:
    """The SDK retries 408/409/429/5xx; 409 was missing from the first cut
    (review blocker on #1074)."""
    t = _transport(
        json_response(409, wire_error(409, "conflict")),
        json_response(200, wire_text("ok")),
        max_retries=1,
    )
    out = await t.create(**_PARAMS)
    assert out.content[0].text == "ok"
    assert len(_no_sleep) == 1


async def test_any_5xx_is_transient_not_only_the_enumerated_ones(_no_sleep: list[float]) -> None:
    t = _transport(
        json_response(520, wire_error(520, "cloudflare-ish")),
        json_response(200, wire_text("ok")),
        max_retries=1,
    )
    await t.create(**_PARAMS)
    assert len(_no_sleep) == 1


async def test_x_should_retry_true_forces_a_retry_of_a_non_transient_status(
    _no_sleep: list[float],
) -> None:
    cap = WireCapture()
    t = _transport(
        json_response(400, wire_error(400, "try again"), headers={"x-should-retry": "true"}),
        json_response(200, wire_text("ok")),
        capture=cap,
        max_retries=1,
    )
    out = await t.create(**_PARAMS)
    assert out.content[0].text == "ok"
    assert len(cap.requests) == 2
    assert len(_no_sleep) == 1


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (503, LLMUpstreamUnavailableError),
        (429, LLMRateLimitedError),
        (409, LLMRequestRejectedError),
    ],
)
async def test_x_should_retry_false_stops_a_normally_transient_status(
    _no_sleep: list[float], status: int, expected: type[Exception]
) -> None:
    """Header precedence: ``x-should-retry: false`` on a status the policy
    would otherwise retry means one attempt, then classification as usual."""
    cap = WireCapture()
    t = _transport(
        json_response(status, wire_error(status, "stop"), headers={"x-should-retry": "false"}),
        json_response(200, wire_text("never reached")),
        capture=cap,
        max_retries=2,
    )
    with pytest.raises(expected):
        await t.create(**_PARAMS)
    assert len(cap.requests) == 1
    assert _no_sleep == []


async def test_transport_errors_retry_then_upstream_unavailable(_no_sleep: list[float]) -> None:
    t = _transport(httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), max_retries=1)
    with pytest.raises(LLMUpstreamUnavailableError):
        await t.create(**_PARAMS)
    assert len(_no_sleep) == 1


async def test_stream_is_not_implemented_until_pr_c() -> None:
    t = _transport()
    with pytest.raises(NotImplementedError, match="PR C"):
        async for _ in t.stream(model="m", max_tokens=1, system="", messages=[]):
            pass


# ---- routing through OpenRouterLLMClient ------------------------------------


def _client_with_raw(
    *steps: Any, purposes: frozenset[str], capture: WireCapture | None = None
) -> OpenRouterLLMClient:
    client = OpenRouterLLMClient(api_key="test-key", raw_purposes=purposes)
    client._raw = raw.RawMessagesTransport(
        api_key="test-key",
        timeout=5.0,
        max_retries=0,
        base_url="https://openrouter.ai/api",
        http=mock_http(*steps, capture=capture),
    )
    return client


async def test_listed_purpose_goes_raw_and_stamps_transport_provider_and_reported_cost() -> None:
    client = _client_with_raw(
        json_response(200, wire_tool_use({"label": "x"})), purposes=frozenset({"p.raw"})
    )
    tool_input, result = await client.complete_tool_use(
        model="claude-sonnet-4-6",
        system="s",
        messages=[Message(role="user", content="x")],
        tool_name="return_X",
        tool_description="d",
        tool_input_schema={"type": "object"},
        purpose="p.raw",
    )
    assert tool_input == {"label": "x"}
    assert result.transport == "messages_http"
    assert result.provider == "Amazon Bedrock"
    # OpenRouter's reported cost is honoured exactly as on the SDK path.
    assert result.cost_source == "reported"
    assert result.cost_usd == pytest.approx(0.001234)


async def test_unlisted_purpose_stays_on_the_sdk() -> None:
    client = _client_with_raw(purposes=frozenset({"p.raw"}))
    client._client.messages.create = validating_create_mock(tool_use_response({"a": 1}))
    _, result = await client.complete_tool_use(
        model="claude-sonnet-4-6",
        system="s",
        messages=[Message(role="user", content="x")],
        tool_name="return_X",
        tool_description="d",
        tool_input_schema={"type": "object"},
        purpose="p.other",
    )
    assert result.transport == "anthropic_sdk"


async def test_stream_purposes_stay_on_the_sdk_even_when_listed() -> None:
    client = _client_with_raw(purposes=frozenset({"p.stream"}))
    assert client._transport_for("p.stream", "stream") is client._transport
    assert client._transport_for("p.stream", "complete") is client._raw


def test_default_client_reads_the_knob_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings, "llm_raw_transport_purposes", " target.fit_score , , tailor.resume "
    )
    assert settings.llm_raw_transport_purposes_set == frozenset(
        {"target.fit_score", "tailor.resume"}
    )
    client = OpenRouterLLMClient(api_key="k")
    assert client._raw_purposes == frozenset({"target.fit_score", "tailor.resume"})
    monkeypatch.setattr(settings, "llm_raw_transport_purposes", "")
    assert OpenRouterLLMClient(api_key="k")._raw_purposes == frozenset()


async def test_raw_transport_is_built_lazily_and_the_sdk_is_never_touched() -> None:
    client = OpenRouterLLMClient(api_key="k", raw_purposes=frozenset({"p"}))
    assert client._raw is None
    t = client._transport_for("p", "complete_tool_use")
    assert isinstance(t, raw.RawMessagesTransport)
    assert client._raw is t
    assert client._transport._sdk is None  # type: ignore[attr-defined]


# ---- provenance in the ledger -----------------------------------------------


def test_cost_row_carries_provider_when_the_gateway_names_one() -> None:
    def _result(provider: str | None) -> LLMResult:
        return LLMResult(
            content="",
            model="claude-sonnet-4-6",
            usage=LLMUsage(),
            cost_usd=0.0,
            latency_ms=0,
            transport="messages_http",
            provider=provider,
        )

    named = cost_log._row_for(
        user_id="u", purpose="p", result=_result("Amazon Bedrock"), metadata=None
    )
    assert named["metadata"] == {
        "cost_source": "estimated",
        "transport": "messages_http",
        "provider": "Amazon Bedrock",
    }
    anonymous = cost_log._row_for(user_id="u", purpose="p", result=_result(None), metadata=None)
    assert "provider" not in anonymous["metadata"]


def test_sdk_adapter_reads_provider_from_model_extra() -> None:
    from app.services.llm import messages_transport as mt

    resp = tool_use_response({"a": 1})
    resp.model_extra = {"provider": "Google Vertex"}
    assert mt._response_from_sdk(resp).provider == "Google Vertex"
    resp.model_extra = MagicMock()  # a Mock, not a Mapping
    assert mt._response_from_sdk(resp).provider is None
