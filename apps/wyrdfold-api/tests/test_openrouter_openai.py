"""OpenRouter OpenAI-compatible path (deepseek triage) — parse edge battery,
usage mapping, model routing, and error translation.

The parse helper is the accumulated bug corpus for the OpenAI shape: every way a
forced function call can come back wrong must fail LOUD (the caller's fallback
then engages) rather than leak a silently-wrong dict into scoring.
"""

import httpx
import pytest

from app.models.llm import LLMUsage, Message
from app.services.llm.errors import LLMRateLimitedError, LLMUpstreamUnavailableError
from app.services.llm.openrouter_client import (
    _OPENAI_SHAPED_MODELS,
    _OPENROUTER_OPENAI_URL,
    OpenRouterLLMClient,
    _openai_usage,
    _parse_openai_tool_response,
)


def _tool_calls(args_str: str) -> list[dict]:
    return [{"function": {"name": "return_X", "arguments": args_str}}]


def _resp(tool_calls: list[dict], *, finish: str = "tool_calls", content: object = None) -> dict:
    return {"choices": [{"finish_reason": finish, "message": {"content": content, "tool_calls": tool_calls}}]}


# ---- _parse_openai_tool_response edge battery -------------------------------


def test_parse_happy_path() -> None:
    data = _resp(_tool_calls('{"verdicts": [{"id": 1, "promising": true}]}'))
    out = _parse_openai_tool_response(data, tool_name="return_X", max_tokens=1000)
    assert out == {"verdicts": [{"id": 1, "promising": True}]}


def test_parse_no_choices_raises() -> None:
    with pytest.raises(ValueError, match="no choices"):
        _parse_openai_tool_response({"choices": []}, tool_name="return_X", max_tokens=1000)


def test_parse_missing_tool_call_raises() -> None:
    # Model refused / answered in prose instead of calling the forced function.
    data = _resp([], finish="stop", content="I can't help with that")
    with pytest.raises(ValueError, match="Expected a forced tool_call"):
        _parse_openai_tool_response(data, tool_name="return_X", max_tokens=1000)


def test_parse_truncated_at_length_raises() -> None:
    # A response stopped at the token cap must fail loud EVEN when the partial
    # arguments happen to parse as valid JSON (a list cut short, a value clipped).
    # Valid JSON here isolates the finish_reason=="length" guard — the JSON guard
    # alone would not catch this, so removing the length check fails this test.
    data = _resp(_tool_calls('{"verdicts": []}'), finish="length")
    with pytest.raises(ValueError, match="truncated"):
        _parse_openai_tool_response(data, tool_name="return_X", max_tokens=1000)


def test_parse_malformed_json_raises() -> None:
    data = _resp(_tool_calls("{not valid json"))
    with pytest.raises(ValueError, match="not valid JSON"):
        _parse_openai_tool_response(data, tool_name="return_X", max_tokens=1000)


def test_parse_fenced_json_raises() -> None:
    # Some models wrap arguments in a markdown fence — invalid JSON, fail loud.
    data = _resp(_tool_calls('```json\n{"a": 1}\n```'))
    with pytest.raises(ValueError, match="not valid JSON"):
        _parse_openai_tool_response(data, tool_name="return_X", max_tokens=1000)


def test_parse_non_object_json_raises() -> None:
    # Valid JSON but a list/scalar, not the object our schema needs.
    data = _resp(_tool_calls("[1, 2, 3]"))
    with pytest.raises(ValueError, match="not an object"):
        _parse_openai_tool_response(data, tool_name="return_X", max_tokens=1000)


def test_parse_empty_arguments_raises() -> None:
    data = _resp(_tool_calls(""))
    with pytest.raises(ValueError, match="not valid JSON"):
        _parse_openai_tool_response(data, tool_name="return_X", max_tokens=1000)


# ---- _openai_usage ----------------------------------------------------------


def test_usage_maps_openai_shape_with_cache() -> None:
    data = {
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "prompt_tokens_details": {"cached_tokens": 40},
        }
    }
    assert _openai_usage(data) == LLMUsage(
        input_tokens=100, output_tokens=20, cache_read_input_tokens=40
    )


def test_usage_missing_fields_default_zero() -> None:
    assert _openai_usage({}) == LLMUsage()


# ---- routing ----------------------------------------------------------------


def test_routing_membership() -> None:
    assert "deepseek-v3-2" in _OPENAI_SHAPED_MODELS
    assert "claude-haiku-4-5" not in _OPENAI_SHAPED_MODELS
    assert "claude-sonnet-4-6" not in _OPENAI_SHAPED_MODELS


class _FakeHttp:
    """Records the posted body and returns a canned httpx.Response."""

    def __init__(self, response: httpx.Response) -> None:
        self._response = response
        self.posted: dict = {}

    async def post(self, url: str, json: dict) -> httpx.Response:
        self.posted = {"url": url, "body": json}
        return self._response


@pytest.mark.asyncio
async def test_deepseek_routes_through_openai_path(monkeypatch) -> None:
    client = OpenRouterLLMClient(api_key="sk-test")
    payload = {
        "choices": [
            {"finish_reason": "tool_calls", "message": {"tool_calls": _tool_calls('{"ok": true}')}}
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    }
    fake = _FakeHttp(
        httpx.Response(200, request=httpx.Request("POST", _OPENROUTER_OPENAI_URL), json=payload)
    )
    monkeypatch.setattr(client, "_openai_client", lambda: fake)

    out, result = await client.complete_tool_use(
        model="deepseek-v3-2",
        system="S",
        messages=[Message(role="user", content="U")],
        tool_name="return_X",
        tool_description="d",
        tool_input_schema={"type": "object"},
        purpose="relevance.title_triage",
        temperature=0.0,
    )

    assert out == {"ok": True}
    assert result.model == "deepseek-v3-2"
    assert result.cost_usd > 0  # priced via PRICING["deepseek-v3-2"]
    # Forced function call, deepseek slug, temperature forwarded, system folded in.
    body = fake.posted["body"]
    assert body["model"] == "deepseek/deepseek-v3.2"
    assert body["tool_choice"] == {"type": "function", "function": {"name": "return_X"}}
    assert body["temperature"] == 0.0
    assert body["messages"][0] == {"role": "system", "content": "S"}


@pytest.mark.asyncio
async def test_openai_path_translates_rate_limit(monkeypatch) -> None:
    # 429 (after retries exhausted) → typed LLMRateLimitedError, not a raw 429.
    client = OpenRouterLLMClient(api_key="sk-test", max_retries=0)
    fake = _FakeHttp(
        httpx.Response(429, request=httpx.Request("POST", _OPENROUTER_OPENAI_URL), json={"e": "rl"})
    )
    monkeypatch.setattr(client, "_openai_client", lambda: fake)
    with pytest.raises(LLMRateLimitedError):
        await client.complete_tool_use(
            model="deepseek-v3-2",
            system="S",
            messages=[Message(role="user", content="U")],
            tool_name="return_X",
            tool_description="d",
            tool_input_schema={"type": "object"},
            purpose="p",
        )


@pytest.mark.asyncio
async def test_openai_path_timeout_becomes_upstream_unavailable(monkeypatch) -> None:
    client = OpenRouterLLMClient(api_key="sk-test", max_retries=0)

    class _TimeoutHttp:
        async def post(self, url: str, json: dict) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=httpx.Request("POST", url))

    monkeypatch.setattr(client, "_openai_client", lambda: _TimeoutHttp())
    with pytest.raises(LLMUpstreamUnavailableError):
        await client.complete_tool_use(
            model="deepseek-v3-2",
            system="S",
            messages=[Message(role="user", content="U")],
            tool_name="return_X",
            tool_description="d",
            tool_input_schema={"type": "object"},
            purpose="p",
        )


@pytest.mark.asyncio
async def test_claude_model_routes_to_anthropic_super(monkeypatch) -> None:
    # A Claude model must route to the inherited Anthropic path, never the
    # OpenAI one. Patch the parent method so we assert routing without network.
    from app.models.llm import LLMResult
    from app.services.llm.anthropic_client import AnthropicLLMClient

    client = OpenRouterLLMClient(api_key="sk-test")
    monkeypatch.setattr(
        client, "_openai_client", lambda: pytest.fail("Claude must not use the OpenAI path")
    )
    seen: dict = {}

    async def _fake_super(self: object, **kwargs: object) -> tuple[dict, LLMResult]:
        seen["model"] = kwargs["model"]
        return {"routed": "anthropic"}, LLMResult(
            content="{}",
            model="claude-haiku-4-5",
            usage=LLMUsage(),
            cost_usd=0.0,
            latency_ms=1,
        )

    monkeypatch.setattr(AnthropicLLMClient, "complete_tool_use", _fake_super)
    out, _ = await client.complete_tool_use(
        model="claude-haiku-4-5",
        system="S",
        messages=[Message(role="user", content="U")],
        tool_name="return_X",
        tool_description="d",
        tool_input_schema={"type": "object"},
        purpose="p",
    )
    assert out == {"routed": "anthropic"}
    assert seen["model"] == "claude-haiku-4-5"
