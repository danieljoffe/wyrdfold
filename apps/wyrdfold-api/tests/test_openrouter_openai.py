"""OpenRouter OpenAI-compatible path (deepseek triage) — parse edge battery,
usage mapping, model routing, and error translation.

The parse helper is the accumulated bug corpus for the OpenAI shape: every way a
forced function call can come back wrong must fail LOUD (the caller's fallback
then engages) rather than leak a silently-wrong dict into scoring.
"""

import json
import logging

import httpx
import pytest

from app.models.llm import LLMUsage, Message
from app.services.llm.errors import (
    LLMRateLimitedError,
    LLMUpstreamUnavailableError,
    MissingToolCallError,
)
from app.services.llm.openrouter_client import (
    _OPENAI_SHAPED_MODELS,
    _OPENROUTER_OPENAI_URL,
    OpenRouterLLMClient,
    _inline_defs,
    _openai_usage,
    _parse_openai_tool_response,
)


def _tool_calls(args_str: str) -> list[dict]:
    return [{"function": {"name": "return_X", "arguments": args_str}}]


def _resp(
    tool_calls: list[dict],
    *,
    finish: str = "tool_calls",
    content: object = None,
    provider: str | None = None,
) -> dict:
    data: dict = {
        "choices": [
            {"finish_reason": finish, "message": {"content": content, "tool_calls": tool_calls}}
        ]
    }
    if provider is not None:
        data["provider"] = provider
    return data


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
    # OpenAI semantics: prompt_tokens INCLUDES the cached subset. Our pricing
    # meters input and cache reads as DISJOINT pools (Anthropic convention),
    # so the cached 40 must be carved OUT of input — recording 100+40 would
    # bill the cached tokens at full rate and again at the 0.1x discount.
    data = {
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "prompt_tokens_details": {"cached_tokens": 40},
        }
    }
    assert _openai_usage(data) == LLMUsage(
        input_tokens=60, output_tokens=20, cache_read_input_tokens=40
    )


def test_usage_fully_cached_prompt_floors_input_at_zero() -> None:
    data = {
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 5,
            "prompt_tokens_details": {"cached_tokens": 100},
        }
    }
    usage = _openai_usage(data)
    assert usage.input_tokens == 0
    assert usage.cache_read_input_tokens == 100


def test_usage_no_cache_details_charges_full_input() -> None:
    data = {"usage": {"prompt_tokens": 100, "completion_tokens": 20}}
    assert _openai_usage(data) == LLMUsage(input_tokens=100, output_tokens=20)


def test_usage_cache_decomposition_prices_hit_cheaper_than_miss() -> None:
    # End-to-end guard on the double-count: the SAME 1,000-token prompt must
    # cost LESS when 900 of it came from cache than when none did.
    from app.services.llm.pricing import calculate_cost

    miss = _openai_usage({"usage": {"prompt_tokens": 1000, "completion_tokens": 100}})
    hit = _openai_usage(
        {
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 100,
                "prompt_tokens_details": {"cached_tokens": 900},
            }
        }
    )
    cost_miss = calculate_cost("deepseek-v3-2", miss)
    cost_hit = calculate_cost("deepseek-v3-2", hit)
    assert cost_hit < cost_miss


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
    # No cost in this payload's usage → the static table priced it.
    assert result.cost_usd > 0
    assert result.cost_source == "estimated"
    # Forced function call, deepseek slug, temperature forwarded, system folded in.
    body = fake.posted["body"]
    assert body["model"] == "deepseek/deepseek-v3.2"
    assert body["tool_choice"] == {"type": "function", "function": {"name": "return_X"}}
    assert body["temperature"] == 0.0
    assert body["messages"][0] == {"role": "system", "content": "S"}
    # #935 provider routing. The ignore list is pinned as a LITERAL on
    # purpose: every slug here declared supports_tool_choice.function=false
    # on OpenRouter's endpoint metadata (2026-09-02), and this path forces a
    # named function on every call. require_parameters alone would NOT
    # exclude five of the six — they list tool_choice as supported; the
    # refusal is one level finer. (SambaNova lists neither tools nor
    # tool_choice, so require_parameters already filters it; kept in the
    # ignore defensively — the #980 review's correction.) Editing the source
    # list must be a conscious act here too.
    assert body["provider"] == {
        "require_parameters": True,
        "ignore": ["gmicloud", "streamlake", "atlas-cloud", "novita", "alibaba", "sambanova"],
    }


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


# ---- $defs inlining (the grammar-compile 400 corpus, prod 2026-07-19) --------
# OpenRouter's OpenAI-shaped grammar compiler can't resolve $defs/$ref (Pydantic
# emits them for nested models). Prod returned HTTP 200 with the body
# {'error': {'code': 400, 'message': 'Failed to compile json grammar: Cannot
# find field $defs in #/$defs/AxisScores'}} → grading systematically failed.


def test_inline_defs_dereferences_nested_model() -> None:
    sch = {
        "type": "object",
        "properties": {
            "axis": {"$ref": "#/$defs/AxisScores"},
            "n": {"type": "integer"},
        },
        "$defs": {"AxisScores": {"type": "object", "properties": {"a": {"type": "number"}}}},
    }
    out = _inline_defs(sch)
    assert "$defs" not in out
    assert json.dumps(out).count("$ref") == 0
    assert out["properties"]["axis"] == {
        "type": "object",
        "properties": {"a": {"type": "number"}},
    }


def test_inline_defs_passthrough_without_defs() -> None:
    sch = {"type": "object", "properties": {"n": {"type": "integer"}}}
    assert _inline_defs(sch) == sch


def test_inline_defs_recursive_ref_terminates() -> None:
    # A self-referential model can't be fully inlined — degrade to a permissive
    # object instead of recursing forever (our grading schemas aren't recursive).
    sch = {
        "type": "object",
        "properties": {"child": {"$ref": "#/$defs/Node"}},
        "$defs": {"Node": {"type": "object", "properties": {"child": {"$ref": "#/$defs/Node"}}}},
    }
    out = _inline_defs(sch)
    assert "$defs" not in out  # terminated, no RecursionError


@pytest.mark.asyncio
async def test_openai_path_posts_inlined_schema(monkeypatch) -> None:
    """Regression: a nested-model schema must go OUT inlined (no $defs/$ref) so
    the grammar compiler doesn't 400."""
    client = OpenRouterLLMClient(api_key="sk-test")
    payload = {
        "choices": [
            {"finish_reason": "tool_calls", "message": {"tool_calls": _tool_calls('{"ok": true}')}}
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    fake = _FakeHttp(
        httpx.Response(200, request=httpx.Request("POST", _OPENROUTER_OPENAI_URL), json=payload)
    )
    monkeypatch.setattr(client, "_openai_client", lambda: fake)

    schema = {
        "type": "object",
        "properties": {"axis": {"$ref": "#/$defs/AxisScores"}},
        "$defs": {"AxisScores": {"type": "object"}},
    }
    await client.complete_tool_use(
        model="deepseek-v3-2",
        system="S",
        messages=[Message(role="user", content="U")],
        tool_name="return_X",
        tool_description="d",
        tool_input_schema=schema,
        purpose="relevance.title_triage",
        temperature=0.0,
    )
    params = fake.posted["body"]["tools"][0]["function"]["parameters"]
    assert "$defs" not in params
    assert json.dumps(params).count("$ref") == 0


# ---- HTTP-200-with-error-body corpus (prod 2026-07-19) ----------------------
# OpenRouter can return HTTP 200 with the upstream error in the body, which the
# status-based retry never sees. Map transient codes to the retryable upstream
# error; surface the rest clearly (not a confusing "no choices").


def _error_body_client(monkeypatch, body: dict) -> "OpenRouterLLMClient":
    client = OpenRouterLLMClient(api_key="sk-test")
    fake = _FakeHttp(
        httpx.Response(200, request=httpx.Request("POST", _OPENROUTER_OPENAI_URL), json=body)
    )
    monkeypatch.setattr(client, "_openai_client", lambda: fake)
    return client


async def _call(client: "OpenRouterLLMClient") -> object:
    return await client.complete_tool_use(
        model="deepseek-v3-2",
        system="S",
        messages=[Message(role="user", content="U")],
        tool_name="return_X",
        tool_description="d",
        tool_input_schema={"type": "object"},
        purpose="relevance.title_triage",
        temperature=0.0,
    )


@pytest.mark.asyncio
async def test_transient_error_body_maps_to_upstream_unavailable(monkeypatch) -> None:
    client = _error_body_client(
        monkeypatch, {"error": {"message": "The operation was aborted", "code": 504}}
    )
    with pytest.raises(LLMUpstreamUnavailableError):
        await _call(client)


@pytest.mark.asyncio
async def test_grammar_400_error_body_surfaces_clearly(monkeypatch) -> None:
    client = _error_body_client(
        monkeypatch,
        {
            "error": {
                "message": "Failed to compile json grammar: Cannot find field $defs",
                "code": 400,
            }
        },
    )
    # NOT a confusing "no choices" — a clear error-body message with the code.
    with pytest.raises(ValueError, match=r"error body.*code=400"):
        await _call(client)


# ---- prose-instead-of-tool-call retry (prod 2026-08-05) ---------------------
# DeepSeek intermittently ignores ``tool_choice`` and answers in prose with
# finish_reason='stop'. The flake is stochastic, so complete_tool_use retries
# exactly this shape ONCE; every other parse failure still fails immediately.


class _FakeHttpSeq:
    """Returns canned responses in sequence, recording every posted body."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = list(responses)
        self.posted: list[dict] = []

    async def post(self, url: str, json: dict) -> httpx.Response:
        self.posted.append({"url": url, "body": json})
        return self._responses.pop(0)


def _http_resp(payload: dict) -> httpx.Response:
    return httpx.Response(200, request=httpx.Request("POST", _OPENROUTER_OPENAI_URL), json=payload)


_PROSE = _resp([], finish="stop", content="This title is clearly unrelated to DevOps/SRE...")
_GOOD = {
    "choices": [
        {"finish_reason": "tool_calls", "message": {"tool_calls": _tool_calls('{"ok": true}')}}
    ],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2},
}


@pytest.mark.asyncio
async def test_missing_tool_call_retries_once_and_succeeds(monkeypatch) -> None:
    client = OpenRouterLLMClient(api_key="sk-test")
    fake = _FakeHttpSeq([_http_resp(_PROSE), _http_resp(_GOOD)])
    monkeypatch.setattr(client, "_openai_client", lambda: fake)

    out, _ = await _call(client)

    assert out == {"ok": True}
    assert len(fake.posted) == 2  # first attempt prose, one retry, done


@pytest.mark.asyncio
async def test_missing_tool_call_twice_fails_loud_after_one_retry(monkeypatch) -> None:
    client = OpenRouterLLMClient(api_key="sk-test")
    fake = _FakeHttpSeq([_http_resp(_PROSE), _http_resp(_PROSE)])
    monkeypatch.setattr(client, "_openai_client", lambda: fake)

    with pytest.raises(MissingToolCallError, match="Expected a forced tool_call"):
        await _call(client)
    assert len(fake.posted) == 2  # exactly one retry, never a loop


@pytest.mark.asyncio
async def test_other_parse_failures_do_not_retry(monkeypatch) -> None:
    # Malformed tool arguments are a DIFFERENT contract break (not the prose
    # flake) — they must fail on the first attempt, no retry spend.
    bad_args = _resp(_tool_calls("{not valid json"))
    client = OpenRouterLLMClient(api_key="sk-test")
    fake = _FakeHttpSeq([_http_resp(bad_args), _http_resp(_GOOD)])
    monkeypatch.setattr(client, "_openai_client", lambda: fake)

    with pytest.raises(ValueError, match="not valid JSON"):
        await _call(client)
    assert len(fake.posted) == 1


# ---- Salvaging a tool call the model wrote as XML prose (#821) --------------
#
# DeepSeek answers a forced tool call by writing Anthropic's XML invocation
# syntax into ``content`` instead of emitting ``tool_calls``. Prod logged 88 of
# these in one 16h window — EVERY one with ``finish_reason='stop'``, i.e. a
# complete answer we were throwing away, retrying at full cost, and losing
# anyway when the retry reproduced it (21 of 25 distinct titles recurred).
#
# The payloads below are the real logged ones.

_REAL_TRIAGE_CONTENT = (
    '<invoke name="return_TitleTriageResponse">\n'
    '<parameter name="verdicts" string="false">'
    '[{"id": 1, "promising": false, "confidence": 85, "title_prefix": "Data Scientist – Cyber"}]'
    "</parameter>\n</invoke>"
)
_REAL_TAGS_CONTENT = (
    '<invoke name="return_QualificationTags">\n'
    '<parameter name="is_us" string="false">true</parameter>\n'
    '<parameter name="us_confidence" string="false">100</parameter>\n'
    '<parameter name="role_family" string="true">engineering</parameter>\n'
    "</invoke>"
)


def test_prose_tool_call_is_salvaged_not_discarded() -> None:
    data = _resp([], finish="stop", content=_REAL_TRIAGE_CONTENT)
    out = _parse_openai_tool_response(data, tool_name="return_TitleTriageResponse", max_tokens=1000)
    assert out == {
        "verdicts": [
            {
                "id": 1,
                "promising": False,
                "confidence": 85,
                "title_prefix": "Data Scientist – Cyber",
            }
        ]
    }


def test_prose_salvage_names_the_responding_provider(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#935: the deterministic prose-tool-call failures track which endpoint
    served the call. Every salvage names the provider so the hit rate is
    correlatable per provider — that correlation is what the routing
    ignore-list was derived from, and drift in the endpoint pool shows up in
    these lines first."""
    data = _resp([], finish="stop", content=_REAL_TRIAGE_CONTENT, provider="GMICloud")
    with caplog.at_level(logging.WARNING, logger="app.services.llm.openrouter_client"):
        _parse_openai_tool_response(data, tool_name="return_TitleTriageResponse", max_tokens=1000)
    assert any("provider='GMICloud'" in r.message for r in caplog.records)


def test_missing_tool_call_failure_names_the_responding_provider() -> None:
    """The unsalvageable case carries the provider too — the paid retry was
    'a coin flip on a deterministic failure', and provider-labelled failures
    are how that determinism becomes visible in prod logs."""
    data = _resp([], finish="stop", content="I refuse.", provider="SambaNova")
    with pytest.raises(MissingToolCallError, match="provider='SambaNova'"):
        _parse_openai_tool_response(data, tool_name="return_X", max_tokens=1000)


def test_prose_salvage_decodes_json_and_string_parameters() -> None:
    """``string="true"`` marks a raw string; anything else is JSON — so
    ``true``/``100`` must not come back as the strings "true"/"100"."""
    data = _resp([], finish="stop", content=_REAL_TAGS_CONTENT)
    out = _parse_openai_tool_response(data, tool_name="return_QualificationTags", max_tokens=1000)
    assert out == {"is_us": True, "us_confidence": 100, "role_family": "engineering"}


def test_prose_salvage_refuses_a_truncated_block() -> None:
    """No closing ``</invoke>`` ⇒ the response was cut off. Refuse.

    QualificationTags has a default for EVERY field plus a tolerate-malformed
    pass, so a partial dict would validate silently and write a confidently
    wrong tag. Better to raise and let the retry/fallback run.
    """
    truncated = _REAL_TAGS_CONTENT[: _REAL_TAGS_CONTENT.index('<parameter name="role_family"')]
    data = _resp([], finish="stop", content=truncated)
    with pytest.raises(ValueError, match="Expected a forced tool_call"):
        _parse_openai_tool_response(data, tool_name="return_QualificationTags", max_tokens=1000)


def test_prose_salvage_refuses_when_a_parameter_failed_to_parse() -> None:
    """A ``<parameter`` that opened but never closed must not be silently
    dropped from an otherwise-complete block."""
    content = (
        '<invoke name="return_QualificationTags">\n'
        '<parameter name="is_us" string="false">true</parameter>\n'
        '<parameter name="role_family" string="true">engineering\n'
        "</invoke>"
    )
    data = _resp([], finish="stop", content=content)
    with pytest.raises(ValueError, match="Expected a forced tool_call"):
        _parse_openai_tool_response(data, tool_name="return_QualificationTags", max_tokens=1000)


def test_prose_salvage_refuses_a_different_tools_payload() -> None:
    """The block names another tool — accepting it would answer the wrong
    question with a well-formed dict."""
    data = _resp([], finish="stop", content=_REAL_TRIAGE_CONTENT)
    with pytest.raises(ValueError, match="Expected a forced tool_call"):
        _parse_openai_tool_response(data, tool_name="return_QualificationTags", max_tokens=1000)


def test_plain_prose_refusal_still_raises() -> None:
    """No XML at all — the original behaviour is untouched."""
    data = _resp([], finish="stop", content="I can't help with that")
    with pytest.raises(ValueError, match="Expected a forced tool_call"):
        _parse_openai_tool_response(data, tool_name="return_X", max_tokens=1000)


def test_structured_tool_calls_still_win_over_content() -> None:
    """Salvage is a fallback, never a preference: a real tool_call must be used
    even when the model also echoed XML into content."""
    data = _resp(
        _tool_calls('{"verdicts": [{"id": 9}]}'),
        finish="tool_calls",
        content=_REAL_TRIAGE_CONTENT,
    )
    out = _parse_openai_tool_response(data, tool_name="return_X", max_tokens=1000)
    assert out == {"verdicts": [{"id": 9}]}


# ---- #850: the OTHER prose shape — bare JSON, no XML wrapper -----------------

_TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {"verdicts": {"type": "array"}},
    "required": ["verdicts"],
}


def _prose_json(content: str) -> dict:
    return _resp([], finish="stop", content=content)


def test_salvages_a_bare_json_object_written_into_content() -> None:
    """The exact prod failure (#850): reasoning prose, then the answer as plain
    JSON, no tool_calls and no XML. Captured verbatim from the Railway logs."""
    content = (
        'This is a clear case of different role function. The target is "Senior '
        'Frontend Engineer" which is an engineering role focused on frontend '
        'development. The candidate title "Intern, AI Prototyping" is an '
        "internship position (different seniority level).\n\n"
        '{\n  "verdicts": [\n'
        '    {"id": 1, "promising": false, "confidence": 95, "title_prefix": "Intern, AI"}\n'
        "  ]\n}"
    )
    out = _parse_openai_tool_response(
        _prose_json(content),
        tool_name="return_TitleTriageResponse",
        max_tokens=1000,
        tool_input_schema=_TRIAGE_SCHEMA,
    )
    assert out["verdicts"][0]["id"] == 1
    assert out["verdicts"][0]["promising"] is False


def test_refuses_an_object_missing_a_required_key() -> None:
    """All-or-nothing. A bare object names no tool, and our models default every
    field — so a fragment would validate silently into a confident wrong answer.
    The schema's required keys are the only thing standing in the way."""
    with pytest.raises(MissingToolCallError):
        _parse_openai_tool_response(
            _prose_json('Thinking...\n{"confidence": 95}'),
            tool_name="return_TitleTriageResponse",
            max_tokens=1000,
            tool_input_schema=_TRIAGE_SCHEMA,
        )


def test_prefers_the_last_object_so_a_worked_example_cannot_win() -> None:
    """Models reason first and answer last. An example echoed from the prompt
    appears earlier, so taking the first match would return the wrong payload."""
    content = (
        'For example the shape is {"verdicts": [{"id": 99, "promising": true}]}.\n'
        'My actual answer:\n{"verdicts": [{"id": 1, "promising": false}]}'
    )
    out = _parse_openai_tool_response(
        _prose_json(content),
        tool_name="return_TitleTriageResponse",
        max_tokens=1000,
        tool_input_schema=_TRIAGE_SCHEMA,
    )
    assert out["verdicts"][0]["id"] == 1


def test_a_truncated_object_is_not_salvaged() -> None:
    """A response cut at the token cap has no closing brace, so no span is
    emitted and it raises — a half-read batch must never look complete."""
    with pytest.raises(MissingToolCallError):
        _parse_openai_tool_response(
            _prose_json('Reasoning...\n{"verdicts": [{"id": 1, "promis'),
            tool_name="return_TitleTriageResponse",
            max_tokens=1000,
            tool_input_schema=_TRIAGE_SCHEMA,
        )


def test_braces_inside_strings_do_not_unbalance_the_scan() -> None:
    """A JD quoting "{tech}" must not break the brace matching."""
    content = 'Note the title says "{Remote}" here.\n{"verdicts": [{"id": 7, "promising": true}]}'
    out = _parse_openai_tool_response(
        _prose_json(content),
        tool_name="return_TitleTriageResponse",
        max_tokens=1000,
        tool_input_schema=_TRIAGE_SCHEMA,
    )
    assert out["verdicts"][0]["id"] == 7


def test_no_schema_means_no_json_salvage() -> None:
    """Callers that don't pass a schema keep the old behaviour exactly — the
    guard is the schema, so without one we refuse rather than guess."""
    with pytest.raises(MissingToolCallError):
        _parse_openai_tool_response(
            _prose_json('{"verdicts": []}'),
            tool_name="return_TitleTriageResponse",
            max_tokens=1000,
        )


# ---- reported cost on the OpenAI-shaped path (#933) -------------------------
#
# This is the path prod actually runs on: LLM_PROVIDER=openrouter with all
# three phase models set to deepseek-v3-2, which is OpenAI-shaped. The slug is
# sent UNPINNED and OpenRouter serves it from 14 endpoints spanning
# $0.209/$0.310 to $3.00/$4.50 per Mtok, so no static rate can be right.
# Usage block shape below is copied from a live response.


def _payload_with_usage(usage: dict) -> dict:
    return {
        "choices": [
            {"finish_reason": "tool_calls", "message": {"tool_calls": _tool_calls('{"ok": true}')}}
        ],
        "usage": usage,
    }


async def _run_openai_path(monkeypatch, payload: dict):
    client = OpenRouterLLMClient(api_key="sk-test")
    fake = _FakeHttp(
        httpx.Response(200, request=httpx.Request("POST", _OPENROUTER_OPENAI_URL), json=payload)
    )
    monkeypatch.setattr(client, "_openai_client", lambda: fake)
    _, result = await client.complete_tool_use(
        model="deepseek-v3-2",
        system="S",
        messages=[Message(role="user", content="U")],
        tool_name="return_X",
        tool_description="d",
        tool_input_schema={"type": "object"},
        purpose="relevance.title_triage",
    )
    return result


@pytest.mark.asyncio
async def test_openai_path_records_the_cost_openrouter_reported(monkeypatch) -> None:
    from app.services.llm.pricing import calculate_cost

    usage = {
        "prompt_tokens": 306,
        "completion_tokens": 32,
        "cost": 9.2694e-05,
        "is_byok": False,
        "cost_details": {"upstream_inference_cost": 9.2694e-05},
    }
    # Anti-vacuous: the payload really carries a cost, and the table really
    # would have produced a different figure — so "reported" can only be
    # reached by reading it off the response.
    assert usage["cost"] == 9.2694e-05
    assert calculate_cost("deepseek-v3-2", _openai_usage({"usage": usage})) != pytest.approx(
        9.2694e-05
    )

    result = await _run_openai_path(monkeypatch, _payload_with_usage(usage))
    assert result.cost_usd == pytest.approx(9.2694e-05)
    assert result.cost_source == "reported"


@pytest.mark.asyncio
async def test_openai_path_falls_back_to_the_table_without_a_cost(monkeypatch) -> None:
    from app.services.llm.pricing import calculate_cost

    usage = {"prompt_tokens": 306, "completion_tokens": 32}
    # Anti-vacuous: the cost really IS absent.
    assert "cost" not in usage

    result = await _run_openai_path(monkeypatch, _payload_with_usage(usage))
    assert result.cost_source == "estimated"
    assert result.cost_usd == pytest.approx(
        calculate_cost("deepseek-v3-2", _openai_usage({"usage": usage}))
    )


@pytest.mark.asyncio
async def test_openai_path_reported_cost_survives_cache_hit_bookkeeping(monkeypatch) -> None:
    """`cost` is already net of caching (measured: a Haiku cache-write billed
    1.25x and the following cache-read 0.1x, both matching `cost` exactly), so
    the reported figure is used AS IS — not re-derived from the token split."""
    usage = {
        "prompt_tokens": 1000,
        "completion_tokens": 100,
        "prompt_tokens_details": {"cached_tokens": 900},
        "cost": 0.000123,
        "is_byok": False,
    }
    result = await _run_openai_path(monkeypatch, _payload_with_usage(usage))
    # Token split still recorded for reporting…
    assert result.usage.cache_read_input_tokens == 900
    assert result.usage.input_tokens == 100
    # …but the cost is the billed one, untouched by our cache arithmetic.
    assert result.cost_usd == pytest.approx(0.000123)
    assert result.cost_source == "reported"
