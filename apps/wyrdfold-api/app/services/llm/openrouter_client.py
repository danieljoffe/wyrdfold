"""OpenRouter-backed LLMClient — Anthropic-shaped AND OpenAI-shaped routing.

Originally PR A: a thin subclass of ``AnthropicLLMClient`` that pointed the
Anthropic SDK at OpenRouter's Anthropic-compatible ``/messages`` endpoint and
remapped ModelIds to OpenRouter slugs. That path is unchanged for Claude models.

This module now also carries the deferred "PR B": non-Anthropic models
(DeepSeek, later GPT/Gemini) can't use the Anthropic ``/messages`` shape, so
``complete_tool_use`` **routes by model** — Anthropic models keep the inherited
SDK path; OpenAI-shaped models (``_OPENAI_SHAPED_MODELS``) go through
OpenRouter's OpenAI-compatible ``/chat/completions`` with forced function
calling (the OpenAI analogue of Anthropic's forced ``tool_choice``). The tool
arguments come back as a JSON string we parse + validate, then wrap in the same
``LLMResult`` shape so cost-logging and callers are provider-agnostic.

Only ``complete_tool_use`` is wired for OpenAI-shaped models — that's the one
structured-output path triage/grading use (via ``complete_json``). ``complete``
and ``stream`` raise for those models until a caller needs them.

ZDR (Zero Data Retention) is enabled account-wide via the OpenRouter dashboard.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any, cast

import httpx

from app.models.llm import LLMResult, LLMStreamEvent, LLMUsage, Message, ModelId
from app.services.llm.anthropic_client import AnthropicLLMClient
from app.services.llm.errors import (
    LLMUpstreamUnavailableError,
    MissingToolCallError,
    translate_api_status_error,
)
from app.services.llm.pricing import calculate_cost

# Anthropic SDK appends "/v1/messages" to base_url internally, so omit /v1 here.
_OPENROUTER_BASE_URL = "https://openrouter.ai/api"
# The OpenAI-compatible endpoint is the full path (no SDK to append /v1).
_OPENROUTER_OPENAI_URL = "https://openrouter.ai/api/v1/chat/completions"

# Map our internal ModelId to OpenRouter's namespaced slugs (dotted versions).
# Extend this dict when ModelId gains new entries.
_MODEL_SLUG_MAP: dict[str, str] = {
    "claude-opus-4-7": "anthropic/claude-opus-4.7",
    "claude-sonnet-4-6": "anthropic/claude-sonnet-4.6",
    "claude-haiku-4-5": "anthropic/claude-haiku-4.5",
    "deepseek-v3-2": "deepseek/deepseek-v3.2",
}

# Models that must use the OpenAI-compatible /chat/completions endpoint
# (function calling) rather than the Anthropic /messages endpoint.
_OPENAI_SHAPED_MODELS: frozenset[str] = frozenset({"deepseek-v3-2"})

# HTTP statuses worth a retry (transient); others translate + raise immediately.
_TRANSIENT_STATUSES: frozenset[int] = frozenset({429, 500, 502, 503, 504, 529})

logger = logging.getLogger(__name__)
_BACKOFF_BASE_SECONDS = 0.5


def _inline_defs(schema: dict[str, Any]) -> dict[str, Any]:
    """Return *schema* with every ``$ref: #/$defs/X`` replaced by its inlined
    definition and the ``$defs`` block removed — a self-contained schema.

    Pydantic v2 emits ``$defs`` + ``$ref`` for nested models (e.g. a
    ``JobFitResult`` with a nested ``AxisScores``). OpenRouter's OpenAI-shaped
    structured-output grammar compiler can't resolve those — it 400s with
    ``Failed to compile json grammar: Cannot find field $defs in
    #/$defs/AxisScores`` (HTTP 200, error in the body), which systematically
    breaks grading on a ``$defs``-bearing schema. Inlining sidesteps it; the
    result is equivalent JSON Schema, valid for the Anthropic path too (so this
    is safe if ever shared). Self-referential models can't be inlined — ours
    aren't — so a back-reference degrades to a permissive ``{"type": "object"}``.
    """
    defs = schema.get("$defs")
    if not isinstance(defs, dict) or not defs:
        return schema

    def resolve(node: Any, seen: frozenset[str]) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                name = ref[len("#/$defs/") :]
                if name in seen:  # recursive model — leave permissive
                    return {"type": "object"}
                return resolve(defs.get(name, {"type": "object"}), seen | {name})
            return {k: resolve(v, seen) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [resolve(v, seen) for v in node]
        return node

    body = {k: v for k, v in schema.items() if k != "$defs"}
    return cast("dict[str, Any]", resolve(body, frozenset()))


def _parse_openai_tool_response(
    data: dict[str, Any], *, tool_name: str, max_tokens: int
) -> dict[str, Any]:
    """Extract the forced tool call's arguments from an OpenAI-compatible
    chat/completions response. Fails loud on every way the structured
    contract can break — the caller's fallback engages rather than a
    silently-wrong dict flowing downstream (mirrors the Anthropic path, #47).
    """
    choices = data.get("choices") or []
    if not choices:
        raise ValueError(f"OpenRouter returned no choices for {tool_name!r}: {str(data)[:300]!r}")
    choice = choices[0]
    finish = choice.get("finish_reason")
    message = choice.get("message") or {}
    tool_calls = message.get("tool_calls") or []
    if not tool_calls:
        raise MissingToolCallError(
            f"Expected a forced tool_call for {tool_name!r}, got finish_reason="
            f"{finish!r}, content={str(message.get('content'))[:200]!r}"
        )
    # A tool call cut off at the token cap emitted a truncated argument string;
    # the parsed dict would be incomplete. Fail loud (matches Anthropic's
    # stop_reason==max_tokens guard).
    if finish == "length":
        raise ValueError(
            f"Tool input for {tool_name!r} was truncated at max_tokens={max_tokens}; "
            "the structured response is incomplete"
        )
    args_str = tool_calls[0].get("function", {}).get("arguments", "")
    try:
        tool_input = json.loads(args_str)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(
            f"Tool arguments for {tool_name!r} were not valid JSON: {str(exc)[:120]}"
        ) from exc
    if not isinstance(tool_input, dict):
        raise ValueError(
            f"Tool arguments for {tool_name!r} decoded to "
            f"{type(tool_input).__name__}, not an object"
        )
    return tool_input


def _openai_usage(data: dict[str, Any]) -> LLMUsage:
    """Map OpenAI-shaped usage onto our LLMUsage. ``cached_tokens`` (when the
    provider reports it) counts as a cache read; OpenAI-shaped providers don't
    surface a separate cache-write line, so creation stays 0.

    OpenAI semantics: ``prompt_tokens`` INCLUDES the cached subset, while our
    pricing meters input and cache reads as disjoint pools (Anthropic
    convention — ``calculate_cost`` charges input at full rate PLUS cache
    reads at 0.1x). Subtract the cached subset from input so a cache hit is
    billed once at the discount, not full rate + discount again."""
    u = data.get("usage") or {}
    cached = int((u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0)
    prompt = int(u.get("prompt_tokens", 0) or 0)
    return LLMUsage(
        input_tokens=max(0, prompt - cached),
        output_tokens=int(u.get("completion_tokens", 0) or 0),
        cache_read_input_tokens=cached,
        cache_creation_input_tokens=0,
    )


class OpenRouterLLMClient(AnthropicLLMClient):
    """LLMClient via OpenRouter — Anthropic ``/messages`` for Claude models,
    OpenAI ``/chat/completions`` for OpenAI-shaped models (DeepSeek)."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        timeout: float = 600.0,
        max_retries: int = 3,
    ) -> None:
        super().__init__(
            api_key=api_key,
            timeout=timeout,
            max_retries=max_retries,
            base_url=_OPENROUTER_BASE_URL,
        )
        # The Anthropic SDK owns the /messages path; the OpenAI path needs its
        # own long-timeout httpx client (the shared app client's 15s read
        # timeout would abort DeepSeek's ~20-30s calls).
        self._api_key = api_key
        self._timeout = timeout
        self._max_retries = max_retries
        self._openai_http: httpx.AsyncClient | None = None

    def _resolve_model(self, model: ModelId) -> str:
        slug = _MODEL_SLUG_MAP.get(model)
        if slug is None:
            raise ValueError(
                f"No OpenRouter slug mapped for ModelId={model!r}. "
                f"Add it to _MODEL_SLUG_MAP in openrouter_client.py."
            )
        return slug

    def _openai_client(self) -> httpx.AsyncClient:
        if self._openai_http is None:
            self._openai_http = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout, connect=10.0),
                headers={"Authorization": f"Bearer {self._api_key or ''}"},
            )
        return self._openai_http

    async def complete_tool_use(
        self,
        *,
        model: ModelId,
        system: str,
        messages: list[Message],
        tool_name: str,
        tool_description: str,
        tool_input_schema: dict[str, Any],
        purpose: str,
        max_tokens: int = 4096,
        cache_system: bool = False,
        temperature: float | None = None,
    ) -> tuple[dict[str, Any], LLMResult]:
        if model in _OPENAI_SHAPED_MODELS:
            try:
                return await self._openai_tool_use(
                    model=model,
                    system=system,
                    messages=messages,
                    tool_name=tool_name,
                    tool_description=tool_description,
                    tool_input_schema=tool_input_schema,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
            except MissingToolCallError:
                # Stochastic prose-instead-of-tool-call flake: one fresh
                # attempt, then fail loud as before (the caller's fallback —
                # triage defers, grading skips — engages on the second miss).
                logger.warning(
                    "forced tool_call missing for %s (model answered in prose) — "
                    "retrying once",
                    tool_name,
                )
                return await self._openai_tool_use(
                    model=model,
                    system=system,
                    messages=messages,
                    tool_name=tool_name,
                    tool_description=tool_description,
                    tool_input_schema=tool_input_schema,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
        return await super().complete_tool_use(
            model=model,
            system=system,
            messages=messages,
            tool_name=tool_name,
            tool_description=tool_description,
            tool_input_schema=tool_input_schema,
            purpose=purpose,
            max_tokens=max_tokens,
            cache_system=cache_system,
            temperature=temperature,
        )

    async def complete(
        self,
        *,
        model: ModelId,
        system: str,
        messages: list[Message],
        purpose: str,
        max_tokens: int = 4096,
        cache_system: bool = False,
    ) -> LLMResult:
        if model in _OPENAI_SHAPED_MODELS:
            raise NotImplementedError(
                f"{model!r} is OpenAI-shaped and currently supports only "
                "complete_tool_use (structured output), not complete()."
            )
        return await super().complete(
            model=model,
            system=system,
            messages=messages,
            purpose=purpose,
            max_tokens=max_tokens,
            cache_system=cache_system,
        )

    def stream(
        self,
        *,
        model: ModelId,
        system: str,
        messages: list[Message],
        purpose: str,
        max_tokens: int = 4096,
        cache_system: bool = False,
    ) -> AsyncIterator[LLMStreamEvent]:
        if model in _OPENAI_SHAPED_MODELS:
            raise NotImplementedError(f"{model!r} is OpenAI-shaped and does not support stream().")
        return super().stream(
            model=model,
            system=system,
            messages=messages,
            purpose=purpose,
            max_tokens=max_tokens,
            cache_system=cache_system,
        )

    async def _openai_tool_use(
        self,
        *,
        model: ModelId,
        system: str,
        messages: list[Message],
        tool_name: str,
        tool_description: str,
        tool_input_schema: dict[str, Any],
        max_tokens: int,
        temperature: float | None,
    ) -> tuple[dict[str, Any], LLMResult]:
        if not messages:
            raise ValueError("OpenRouter OpenAI-shaped path requires at least one message")

        openai_messages: list[dict[str, str]] = []
        if system:
            openai_messages.append({"role": "system", "content": system})
        openai_messages.extend({"role": m.role, "content": m.content} for m in messages)

        body: dict[str, Any] = {
            "model": self._resolve_model(model),
            "messages": openai_messages,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "description": tool_description,
                        # Inline $defs/$ref: OpenRouter's grammar compiler can't
                        # resolve them and 400s on a nested-model schema.
                        "parameters": _inline_defs(tool_input_schema),
                    },
                }
            ],
            # Force exactly this function — the OpenAI analogue of Anthropic's
            # forced tool_choice, so we always get structured arguments back.
            "tool_choice": {"type": "function", "function": {"name": tool_name}},
            "max_tokens": max_tokens,
        }
        if temperature is not None:
            body["temperature"] = temperature

        http = self._openai_client()
        start = time.perf_counter()
        resp: httpx.Response | None = None
        for attempt in range(self._max_retries + 1):
            try:
                resp = await http.post(_OPENROUTER_OPENAI_URL, json=body)
                resp.raise_for_status()
                break
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status in _TRANSIENT_STATUSES and attempt < self._max_retries:
                    await asyncio.sleep(_BACKOFF_BASE_SECONDS * (2**attempt))
                    continue
                translated = translate_api_status_error(exc.response)
                if translated is not None:
                    raise translated from exc
                raise  # 4xx application bug (bad schema/request) → bubble as 500
            except httpx.TransportError as exc:  # timeouts + connection errors
                if attempt < self._max_retries:
                    await asyncio.sleep(_BACKOFF_BASE_SECONDS * (2**attempt))
                    continue
                raise LLMUpstreamUnavailableError() from exc

        if resp is None:  # unreachable: the loop breaks with resp or raises
            raise LLMUpstreamUnavailableError()
        latency_ms = int((time.perf_counter() - start) * 1000)

        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise LLMUpstreamUnavailableError() from exc

        # OpenRouter sometimes returns HTTP 200 with the upstream error in the
        # BODY (a transient 5xx/timeout, or a grammar-compile 400) — the
        # status-based retry above never saw it, and _parse_openai_tool_response
        # would surface it as a confusing "no choices". Detect it: map transient
        # codes to the retryable upstream error (engages the caller's fallback);
        # surface the rest clearly with the provider message.
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict):
            code = err.get("code")
            if isinstance(code, int) and code in _TRANSIENT_STATUSES:
                raise LLMUpstreamUnavailableError()
            raise ValueError(
                f"OpenRouter error body for {tool_name!r} (code={code}): "
                f"{str(err.get('message'))[:200]!r}"
            )

        tool_input = _parse_openai_tool_response(data, tool_name=tool_name, max_tokens=max_tokens)
        usage = _openai_usage(data)
        cost = calculate_cost(model, usage)
        result = LLMResult(
            content=json.dumps(tool_input),
            model=model,
            usage=usage,
            cost_usd=cost,
            latency_ms=latency_ms,
        )
        return tool_input, result
