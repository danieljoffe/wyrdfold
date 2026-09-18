"""The Messages-API transport seam (#1067).

``AnthropicLLMClient`` builds Messages-API request bodies and interprets
Messages-API responses. HOW the bytes reach the endpoint is this seam:
:class:`SdkMessagesTransport` (the Anthropic SDK, today's behaviour) and, next,
a raw-httpx transport for OpenRouter's Anthropic-compatible
``/api/v1/messages`` that removes the SDK from the drift surface (#1065, #905)
without changing the wire shape our prompts, cache breakpoints and tool
schemas are built for.

Why a seam and not a subclass: ``OpenRouterLLMClient`` inherited the SDK by
construction, so a raw path alone would have left ``AsyncAnthropic`` imported
and instantiated on every OpenRouter client. The SDK transport builds its
client lazily, on the first call, so a client whose purposes are all routed
elsewhere never constructs it (``sdk_client`` is the one place that does).

The transport returns OUR types, normalised from whatever the wire library
hands back: :class:`MessagesResponse` / :class:`ContentBlock` /
:class:`MessagesUsage` for a completion, and :class:`StreamTextDelta` /
:class:`StreamUsageDelta` / :class:`StreamFinal` for a stream. The client
reads those and nothing else, so ``anthropic_client.py`` imports nothing from
``anthropic`` (guarded by a test). Normalisation is by attribute access, not
``model_dump()``, on purpose: the SDK's pydantic objects and the suite's
``MagicMock`` doubles both satisfy it, so the request-shape tests that bind
kwargs against the installed SDK signature (#1065) keep running unchanged.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from anthropic import APIConnectionError, APIStatusError, APITimeoutError, AsyncAnthropic

from app.models.llm import TransportId
from app.services.llm.errors import LLMUpstreamUnavailableError, translate_api_status_error


@dataclass(frozen=True)
class ContentBlock:
    """One block of a Messages-API response: ``text`` for ``type == "text"``,
    ``name`` / ``input`` for ``type == "tool_use"``; other types carry only
    ``type``."""

    type: str
    text: str | None = None
    name: str | None = None
    input: Any = None


@dataclass(frozen=True)
class MessagesUsage:
    """Token counts plus ``reported``: the provider's own billing extras
    (OpenRouter's ``cost`` / ``cost_details`` / ``is_byok``), ``None`` when the
    endpoint sends none (direct api.anthropic.com)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    reported: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class MessagesResponse:
    content: list[ContentBlock]
    stop_reason: str | None
    usage: MessagesUsage
    # OpenRouter names the upstream that served the call (a top-level extra,
    # verified live 2026-09-18); direct api.anthropic.com sends none.
    provider: str | None = None


@dataclass(frozen=True)
class StreamTextDelta:
    text: str


@dataclass(frozen=True)
class StreamUsageDelta:
    """A ``message_delta`` frame's usage extras. OpenRouter reports the billed
    cost here, and the SDK's accumulated final message DROPS it (measured:
    ``final.usage.model_extra`` comes back as ``{"speed": "standard"}`` alone),
    so the client keeps the last non-empty one it sees."""

    reported: Mapping[str, Any] | None


@dataclass(frozen=True)
class StreamFinal:
    message: MessagesResponse


StreamEvent = StreamTextDelta | StreamUsageDelta | StreamFinal


class MessagesTransport(Protocol):
    """What ``AnthropicLLMClient`` needs from a wire path. ``params`` are the
    Messages-API request fields as the SDK names them (``model``,
    ``max_tokens``, ``system``, ``messages``, ``tools``, ``tool_choice``,
    ``extra_body``); a transport that is not the SDK maps them onto the wire."""

    transport_id: TransportId

    async def create(self, **params: Any) -> MessagesResponse: ...

    def stream(self, **params: Any) -> AsyncGenerator[StreamEvent, None]: ...


# ---- Error translation -------------------------------------------------------


def translate_or_reraise(exc: APIStatusError) -> None:
    """Convert an SDK status error into a typed LLM service error when the
    status maps to one of our user-facing transient categories
    (402/429/5xx/auth). Otherwise re-raise so the unhandled-exception handler
    logs it as a 500: those status codes indicate a bug in our request, not a
    transient outage."""
    translated = translate_api_status_error(exc)
    if translated is None:
        raise exc
    raise translated from exc


# ---- SDK object normalisation ------------------------------------------------


def _reported_usage(sdk_usage: object) -> Mapping[str, Any] | None:
    """The provider-reported billing fields hanging off an SDK usage object.

    The Anthropic SDK types only Anthropic's own usage fields, so anything the
    server adds lands in pydantic's ``model_extra``. When the SDK points at
    OpenRouter (see ``openrouter_client``) that is where ``cost``,
    ``cost_details`` and ``is_byok`` arrive, verified against a live response.
    A direct api.anthropic.com call has no such extras, so this returns
    ``None`` and the caller falls back to the static table.

    Guarded with ``isinstance(Mapping)`` rather than a truthiness check
    because the suite's doubles are ``MagicMock``s, whose every attribute is
    itself a truthy Mock; a looser check would hand ``resolve_cost`` a Mock.
    """
    extra = getattr(sdk_usage, "model_extra", None)
    return extra if isinstance(extra, Mapping) else None


def _int_field(obj: object, name: str) -> int:
    """An int attribute or 0. ``None`` (the SDK omits cache fields on some
    responses) and Mock attributes (test doubles) both read as 0, which is
    what the previous ``getattr(..., 0) or 0`` idiom did for ``None``."""
    value = getattr(obj, name, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _usage_from_sdk(usage: object) -> MessagesUsage:
    return MessagesUsage(
        input_tokens=_int_field(usage, "input_tokens"),
        output_tokens=_int_field(usage, "output_tokens"),
        cache_read_input_tokens=_int_field(usage, "cache_read_input_tokens"),
        cache_creation_input_tokens=_int_field(usage, "cache_creation_input_tokens"),
        reported=_reported_usage(usage),
    )


def _block_from_sdk(block: object) -> ContentBlock:
    btype = getattr(block, "type", None)
    kind = btype if isinstance(btype, str) else "unknown"
    if kind == "text":
        text = getattr(block, "text", None)
        return ContentBlock(type=kind, text=text if isinstance(text, str) else "")
    if kind == "tool_use":
        name = getattr(block, "name", None)
        return ContentBlock(
            type=kind,
            name=name if isinstance(name, str) else None,
            input=getattr(block, "input", None),
        )
    return ContentBlock(type=kind)


def _response_from_sdk(response: object) -> MessagesResponse:
    stop_reason = getattr(response, "stop_reason", None)
    content = getattr(response, "content", None) or []
    extra = getattr(response, "model_extra", None)
    provider = extra.get("provider") if isinstance(extra, Mapping) else None
    return MessagesResponse(
        content=[_block_from_sdk(b) for b in content],
        stop_reason=stop_reason if isinstance(stop_reason, str) else None,
        usage=_usage_from_sdk(getattr(response, "usage", None)),
        provider=provider if isinstance(provider, str) else None,
    )


# ---- The SDK transport -------------------------------------------------------


class SdkMessagesTransport:
    """The Anthropic SDK as a transport. Constructs ``AsyncAnthropic`` on the
    first call, never in ``__init__`` (#1067 item 2)."""

    transport_id: TransportId = "anthropic_sdk"

    def __init__(
        self,
        *,
        api_key: str | None,
        timeout: float,
        max_retries: int,
        base_url: str | None,
    ) -> None:
        # ``base_url`` is the extension point for backends that speak the
        # Anthropic API shape behind a different host (OpenRouter). ``None``
        # keeps the SDK at its built-in https://api.anthropic.com endpoint.
        kwargs: dict[str, Any] = {
            "api_key": api_key,
            "timeout": timeout,
            "max_retries": max_retries,
        }
        if base_url is not None:
            kwargs["base_url"] = base_url
        self._kwargs = kwargs
        self._sdk: AsyncAnthropic | None = None

    @property
    def sdk_client(self) -> AsyncAnthropic:
        """The lazily built SDK client. Tests patch ``.messages.create`` /
        ``.messages.stream`` on it, exactly as they did on the old
        ``AnthropicLLMClient._client``."""
        if self._sdk is None:
            self._sdk = AsyncAnthropic(**self._kwargs)
        return self._sdk

    async def create(self, **params: Any) -> MessagesResponse:
        try:
            response = await self.sdk_client.messages.create(**params)
        except APIStatusError as exc:
            translate_or_reraise(exc)
            raise  # pragma: no cover - translate_or_reraise always raises
        except (APIConnectionError, APITimeoutError) as exc:
            raise LLMUpstreamUnavailableError() from exc
        return _response_from_sdk(response)

    async def stream(self, **params: Any) -> AsyncGenerator[StreamEvent, None]:
        # The SDK raises APIStatusError on the initial HTTP handshake (which
        # surfaces from ``async with .stream(...)``) and may raise mid-stream
        # on chunked-transfer errors; the whole region translates uniformly.
        # Iterating the event stream and filtering ``content_block_delta`` /
        # ``text_delta`` is exactly what the SDK's ``text_stream`` helper does
        # internally, so the text yielded is unchanged.
        try:
            async with self.sdk_client.messages.stream(**params) as stream:
                async for event in stream:
                    etype = getattr(event, "type", None)
                    if etype == "content_block_delta":
                        delta = getattr(event, "delta", None)
                        if getattr(delta, "type", None) == "text_delta":
                            text = getattr(delta, "text", None)
                            if isinstance(text, str) and text:
                                yield StreamTextDelta(text=text)
                    elif etype == "message_delta":
                        usage = getattr(event, "usage", None)
                        yield StreamUsageDelta(reported=_reported_usage(usage))
                final = await stream.get_final_message()
        except APIStatusError as exc:
            translate_or_reraise(exc)
            raise  # pragma: no cover - translate_or_reraise always raises
        except (APIConnectionError, APITimeoutError) as exc:
            raise LLMUpstreamUnavailableError() from exc
        yield StreamFinal(message=_response_from_sdk(final))
