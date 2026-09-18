"""Real Anthropic LLM client.

Production implementation of the LLMClient Protocol. Uses the official
`anthropic` SDK. Swap in via `LLM_PROVIDER=anthropic` env var; mock is
the default until you explicitly opt in.

Prompt caching note
-------------------
`cache_system=True` passes the system prompt as a list with
`cache_control: {"type": "ephemeral"}`. Anthropic requires a minimum
cacheable prefix of **4096 tokens for Opus 4.7 / 4.6 / Haiku 4.5** and
**2048 tokens for Sonnet 4.6**. Below that threshold caching silently
no-ops (no error, just `cache_creation_input_tokens: 0`). Our current
system prompts land below 4096 tokens, so caching will activate once
prompts grow — this is intentional plumbing, not an immediate cost win.

`Message.cache_prefix_chars` adds a second, message-level breakpoint:
the static per-target/per-user prefix of a user message is split into
its own text block with `cache_control` (see `_api_message_content`).
Combined with the cached system block, the whole static prompt prefix
(system + target context) counts toward the cacheable minimum.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator, Mapping
from typing import Any, cast

from app.models.llm import (
    LLMResult,
    LLMStreamDelta,
    LLMStreamEvent,
    LLMStreamFinal,
    LLMUsage,
    Message,
    ModelId,
)
from app.services.llm.errors import (
    LLMMalformedOutputError,
    LLMUpstreamUnavailableError,
    MissingToolCallError,
)
from app.services.llm.messages_transport import (
    MessagesResponse,
    MessagesTransport,
    MessagesUsage,
    SdkMessagesTransport,
    StreamFinal,
    StreamTextDelta,
    StreamUsageDelta,
    _reported_usage,  # noqa: F401  re-exported: tests/test_llm_anthropic.py imports it from here
    translate_or_reraise,
)
from app.services.llm.pricing import resolve_cost


def _llm_usage(usage: MessagesUsage) -> LLMUsage:
    return LLMUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_input_tokens=usage.cache_read_input_tokens,
        cache_creation_input_tokens=usage.cache_creation_input_tokens,
    )


def _api_message_content(message: Message) -> Any:
    """Serialize one Message for the API, honouring the cache marker.

    No marker → plain string content (unchanged legacy shape). With
    ``cache_prefix_chars`` set, the content is split into two text
    blocks at exactly that character boundary with ``cache_control:
    ephemeral`` on the first — Anthropic's documented incremental-
    caching pattern. Block concatenation is byte-identical to
    ``message.content``, so this is a cache marker, not a prompt
    change. A marker at/past the end of the content caches the whole
    message as a single block.
    """
    n = message.cache_prefix_chars
    if not n:
        return message.content
    cached_block = {
        "type": "text",
        "text": message.content[:n],
        "cache_control": {"type": "ephemeral"},
    }
    if n >= len(message.content):
        return [cached_block]
    return [cached_block, {"type": "text", "text": message.content[n:]}]


def _api_messages(messages: list[Message]) -> list[dict[str, Any]]:
    return [{"role": m.role, "content": _api_message_content(m)} for m in messages]


logger = logging.getLogger(__name__)

# Models whose Messages API still ACCEPTS a ``temperature`` (#1065).
#
# anthropic 1.0.0 removed ``temperature`` / ``top_p`` / ``top_k`` from
# ``messages.create()`` — passing one is a ``TypeError`` before any HTTP. The
# parameters are gone from the SDK signature, not from the API: Opus 4.7 and
# later return 400 on any value (the default included), Sonnet 5 rejects
# non-default values, and the 4.6 / 4.5 line still accepts them. The SDK's own
# 1.x upgrade guide (Step 6) says to forward via ``extra_body`` only where the
# call pins an accepting model AND visibly depends on the setting. We do:
# ``normalize_posting_title`` pins Sonnet 4.6, its output is the UNIQUE catalog
# dedup key, and its convergence eval was certified at temperature 0.
#
# Keyed by the INTERNAL ModelId, before ``_resolve_model`` — OpenRouter resolves
# ``claude-sonnet-4-6`` to ``anthropic/claude-sonnet-4.6``, so a check against the
# resolved slug would silently drop the hint on exactly the path this repairs.
#
# Rot direction is safe: a model missing from this set OMITS the hint (never a
# 400). Add a new id here only with evidence it accepts sampling params.
_TEMPERATURE_ACCEPTING_MODELS: frozenset[str] = frozenset({"claude-sonnet-4-6", "claude-haiku-4-5"})

# One structured log line per (process, model) the first time the hint is
# dropped — observable without flooding a hot loop.
_temperature_omission_logged: set[str] = set()


def _note_temperature_omitted(model: str, temperature: float) -> None:
    if model in _temperature_omission_logged:
        return
    _temperature_omission_logged.add(model)
    logger.info(
        "temperature hint not forwarded: model does not accept sampling params",
        extra={"model": model, "temperature": temperature},
    )


class AnthropicLLMClient:
    """Implements the LLMClient Protocol. Production-ready."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        timeout: float = 600.0,
        max_retries: int = 2,
        base_url: str | None = None,
        transport: MessagesTransport | None = None,
    ) -> None:
        # The wire path is a seam (#1067, ``messages_transport``). Default:
        # the Anthropic SDK, constructed lazily on the first call. ``base_url``
        # is the extension point for backends that speak the Anthropic API
        # shape behind a different host (OpenRouter, see openrouter_client.py);
        # None keeps the SDK at its built-in https://api.anthropic.com.
        self._transport: MessagesTransport = transport or SdkMessagesTransport(
            api_key=api_key,
            timeout=timeout,
            max_retries=max_retries,
            base_url=base_url,
        )

    def _transport_for(self, purpose: str, method: str) -> MessagesTransport:
        """Which transport serves this call. The base client has one;
        ``OpenRouterLLMClient`` routes by purpose (#1067 rollout knob)."""
        return self._transport

    @property
    def _client(self) -> Any:
        """The SDK client behind the default transport, built on first access.

        Kept for the suite, which patches ``messages.create`` /
        ``messages.stream`` on it; production code never touches it. Raises
        when the transport is not the SDK, which is the point: nothing may
        assume the SDK is there.
        """
        if isinstance(self._transport, SdkMessagesTransport):
            return self._transport.sdk_client
        raise AttributeError("this client's transport is not the Anthropic SDK")

    def _resolve_model(self, model: ModelId) -> str:
        """Translate an internal ModelId to the string the underlying
        API expects. Override in subclasses that need to remap (e.g.
        OpenRouter prepends a provider namespace + uses dotted versions).
        Default: pass through unchanged.
        """
        return model

    @staticmethod
    def _translate_or_reraise(exc: Any) -> None:
        """Kept for callers and tests; the translation lives on the transport
        seam now (``messages_transport.translate_or_reraise``)."""
        translate_or_reraise(exc)

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
        if not messages:
            raise ValueError("AnthropicLLMClient.complete requires at least one message")

        # System parameter: list form with cache_control when caching is requested;
        # plain string otherwise. Empty strings become "" (SDK accepts that).
        system_param: Any
        if cache_system and system:
            system_param = [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        else:
            system_param = system or ""

        api_messages = _api_messages(messages)

        start = time.perf_counter()
        transport = self._transport_for(purpose, "complete")
        response = await transport.create(
            model=cast(Any, self._resolve_model(model)),
            max_tokens=max_tokens,
            system=system_param,
            messages=cast(Any, api_messages),
        )
        latency_ms = int((time.perf_counter() - start) * 1000)

        # Join every text block. Thinking / tool-use blocks are not expected
        # on this call site (we don't enable thinking, we don't declare tools),
        # but we defensively filter to type=="text" rather than assuming
        # response.content[0].
        text_parts = [b.text or "" for b in response.content if b.type == "text"]
        content = "".join(text_parts)

        usage = _llm_usage(response.usage)
        cost, cost_source = resolve_cost(model, usage, reported=response.usage.reported)

        return LLMResult(
            content=content,
            model=model,
            usage=usage,
            cost_usd=cost,
            cost_source=cost_source,
            latency_ms=latency_ms,
            transport=transport.transport_id,
            provider=response.provider,
        )

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
        """Force the model to call a single tool whose input matches the
        provided JSON schema. The Anthropic API validates the tool input
        server-side before returning it, so we get a typed dict back rather
        than a JSON string the model may have shaped wrong.

        ``temperature`` is a best-effort hint. It is forwarded — via the SDK's
        ``extra_body`` escape hatch, never as a direct kwarg — only for models
        in ``_TEMPERATURE_ACCEPTING_MODELS``; for any other model it is omitted
        and logged once. ``None`` keeps the provider default. See #1065.
        """
        if not messages:
            raise ValueError("AnthropicLLMClient.complete_tool_use requires at least one message")

        system_param: Any
        if cache_system and system:
            system_param = [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        else:
            system_param = system or ""

        api_messages = _api_messages(messages)

        tool: dict[str, Any] = {
            "name": tool_name,
            "description": tool_description,
            "input_schema": tool_input_schema,
        }
        tool_choice: dict[str, Any] = {"type": "tool", "name": tool_name}

        create_kwargs: dict[str, Any] = {
            "model": cast(Any, self._resolve_model(model)),
            "max_tokens": max_tokens,
            "system": system_param,
            "messages": cast(Any, api_messages),
            "tools": cast(Any, [tool]),
            "tool_choice": cast(Any, tool_choice),
        }
        if temperature is not None:
            # Keyed on the internal id, NOT the resolved slug (see the
            # allowlist comment). ``extra_body`` is merged into the request
            # JSON as-is; a direct ``temperature=`` kwarg is a TypeError on
            # anthropic >= 1.0.
            if model in _TEMPERATURE_ACCEPTING_MODELS:
                create_kwargs["extra_body"] = {"temperature": temperature}
            else:
                _note_temperature_omitted(model, temperature)

        start = time.perf_counter()
        transport = self._transport_for(purpose, "complete_tool_use")
        response = await transport.create(**create_kwargs)
        latency_ms = int((time.perf_counter() - start) * 1000)

        # Find the tool_use block. The forced tool_choice guarantees one
        # exists, but we fail loud rather than silently if the response
        # somehow lacks it (model abort, refusal, API contract change).
        tool_input: dict[str, Any] | None = None
        for block in response.content:
            if block.type == "tool_use" and block.name == tool_name:
                tool_input = cast(dict[str, Any], block.input)
                break

        if tool_input is None:
            stop_reason = getattr(response, "stop_reason", "unknown")
            raise MissingToolCallError(
                f"Expected tool_use block for {tool_name!r}, got stop_reason="
                f"{stop_reason!r} with content blocks "
                f"{[b.type for b in response.content]!r}"
            )

        # A forced single-tool call that stopped on ``max_tokens`` truncated the
        # tool input mid-emission, so the parsed dict is incomplete. Fail loud
        # rather than return silently-truncated structured data — the caller's
        # fallback (poller → Pending, triage → fail-open) then engages. Most
        # truncations already trip the downstream pydantic validate in
        # ``complete_json``; this also catches the ones that stay schema-valid
        # (a list cut short, a value clipped). (#47)
        if getattr(response, "stop_reason", None) == "max_tokens":
            raise LLMMalformedOutputError(
                f"Tool input for {tool_name!r} was truncated at "
                f"max_tokens={max_tokens}; the structured response is incomplete",
                reason="truncated",
            )

        usage = _llm_usage(response.usage)
        cost, cost_source = resolve_cost(model, usage, reported=response.usage.reported)

        result = LLMResult(
            content=json.dumps(tool_input),
            model=model,
            usage=usage,
            cost_usd=cost,
            cost_source=cost_source,
            latency_ms=latency_ms,
            transport=transport.transport_id,
            provider=response.provider,
        )
        return tool_input, result

    async def stream(
        self,
        *,
        model: ModelId,
        system: str,
        messages: list[Message],
        purpose: str,
        max_tokens: int = 4096,
        cache_system: bool = False,
    ) -> AsyncIterator[LLMStreamEvent]:
        if not messages:
            raise ValueError("AnthropicLLMClient.stream requires at least one message")

        system_param: Any
        if cache_system and system:
            system_param = [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        else:
            system_param = system or ""

        api_messages = _api_messages(messages)

        # OpenRouter reports the billed cost on the ``message_delta`` event,
        # and the SDK's accumulated final message DROPS it — measured: the
        # raw SSE frame carries ``usage.cost`` but
        # ``final_message.usage.model_extra`` comes back as
        # ``{"speed": "standard"}`` alone. So we read it off the event as it
        # goes past. Iterating the event stream and filtering
        # ``content_block_delta``/``text_delta`` is exactly what the SDK's
        # ``text_stream`` helper does internally, so the text we yield is
        # unchanged.
        reported: Mapping[str, Any] | None = None

        start = time.perf_counter()
        # The transport translates handshake and mid-stream failures into the
        # typed hierarchy; this loop only interprets the normalised events.
        transport = self._transport_for(purpose, "stream")
        final_message: MessagesResponse | None = None
        events = transport.stream(
            model=cast(Any, self._resolve_model(model)),
            max_tokens=max_tokens,
            system=system_param,
            messages=cast(Any, api_messages),
        )
        # ``aclosing``: when OUR consumer stops early (the derive route's
        # disconnect path calls aclose() on this generator), the transport's
        # generator is closed here, synchronously, so its finally releases the
        # upstream socket now rather than whenever the event loop's
        # async-generator finalizer gets to it (release gate 2026-09-18).
        async with contextlib.aclosing(events):
            async for event in events:
                if isinstance(event, StreamTextDelta):
                    if event.text:
                        yield LLMStreamDelta(text=event.text)
                elif isinstance(event, StreamUsageDelta):
                    reported = event.reported or reported
                elif isinstance(event, StreamFinal):
                    final_message = event.message
        if final_message is None:
            # A transport that ends without its final frame lost the
            # connection mid-stream; the caller's retry path is the right one.
            raise LLMUpstreamUnavailableError()

        latency_ms = int((time.perf_counter() - start) * 1000)

        text_parts = [b.text or "" for b in final_message.content if b.type == "text"]
        content = "".join(text_parts)

        usage = _llm_usage(final_message.usage)
        # Fall back to the final message's own extras when no message_delta
        # carried a cost (direct Anthropic never does).
        cost, cost_source = resolve_cost(
            model, usage, reported=reported or final_message.usage.reported
        )

        yield LLMStreamFinal(
            result=LLMResult(
                content=content,
                model=model,
                usage=usage,
                cost_usd=cost,
                cost_source=cost_source,
                latency_ms=latency_ms,
                transport=transport.transport_id,
                provider=final_message.provider,
            )
        )
