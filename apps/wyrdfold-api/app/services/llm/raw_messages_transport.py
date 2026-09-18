"""OpenRouter's Anthropic-compatible ``/api/v1/messages`` over httpx (#1067).

The SDK-free Messages transport: same request body the SDK path builds
(system blocks with ``cache_control``, forced ``tool_choice``, ``extra_body``
merged in), same headers the SDK sends (``x-api-key``, ``anthropic-version``,
both verified live on 2026-09-18), same typed errors, and the same normalised
response types as ``SdkMessagesTransport``. What it removes is the SDK from
the drift surface (#1065 / #905) and the ``httpx2`` second stack (#908).

``stream()`` lands in PR C; until then ``OpenRouterLLMClient`` routes every
stream purpose to the SDK regardless of the rollout knob.

Wire facts this parser relies on, from the recorded corpus in
``tests/fixtures/openrouter_messages/``:

- 2xx bodies are Anthropic-shaped plus extras: top-level ``provider``,
  ``usage.cost`` / ``cost_details`` / ``is_byok`` / ``cache_creation``.
- Bad requests are real HTTP 4xx with ``{"type": "error", "error": {...}}``
  bodies; a 200 carrying an error envelope has not been observed on this
  endpoint and is classified as a server fault if it ever appears, so it
  gets looked at rather than retried.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx

from app.models.llm import TransportId
from app.services.llm.errors import (
    LLMRequestRejectedError,
    LLMUpstreamUnavailableError,
    translate_api_status_error,
)
from app.services.llm.messages_transport import (
    ContentBlock,
    MessagesResponse,
    MessagesUsage,
    StreamEvent,
)
from app.services.llm.openrouter_http import post_json_with_retry

logger = logging.getLogger(__name__)

ANTHROPIC_VERSION = "2023-06-01"

# Request fields the SDK path passes as ``messages.create`` kwargs and that
# the wire accepts verbatim. ``extra_body`` is the SDK's escape hatch (the
# #1065 temperature allowlist rides on it) and is merged into the body.
_WIRE_FIELDS = ("model", "max_tokens", "system", "messages", "tools", "tool_choice")


def wire_body(params: Mapping[str, Any], *, stream: bool = False) -> dict[str, Any]:
    """The JSON body for one Messages call from the SDK-style kwargs."""
    body: dict[str, Any] = {k: params[k] for k in _WIRE_FIELDS if params.get(k) is not None}
    extra = params.get("extra_body")
    if isinstance(extra, Mapping):
        body.update(extra)
    if stream:
        body["stream"] = True
    unknown = set(params) - set(_WIRE_FIELDS) - {"extra_body"}
    if unknown:
        # A kwarg the SDK path would have accepted but the wire has no field
        # for. Fail loud: silently dropping it is how #1065 stayed invisible.
        raise TypeError(f"raw Messages transport: unsupported request field(s) {sorted(unknown)}")
    return body


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _block_from_wire(block: Mapping[str, Any]) -> ContentBlock:
    kind = block.get("type")
    kind = kind if isinstance(kind, str) else "unknown"
    if kind == "text":
        text = block.get("text")
        return ContentBlock(type=kind, text=text if isinstance(text, str) else "")
    if kind == "tool_use":
        name = block.get("name")
        return ContentBlock(
            type=kind,
            name=name if isinstance(name, str) else None,
            input=block.get("input"),
        )
    return ContentBlock(type=kind)


def response_from_wire(data: Mapping[str, Any]) -> MessagesResponse:
    """Normalise a 2xx ``/v1/messages`` body. ``usage.reported`` is the whole
    usage mapping, which is where OpenRouter's ``cost`` / ``cost_details`` /
    ``is_byok`` live, exactly what ``pricing.reported_cost_usd`` reads."""
    usage_raw = data.get("usage")
    usage: Mapping[str, Any] = usage_raw if isinstance(usage_raw, Mapping) else {}
    content_raw = data.get("content")
    content = [
        _block_from_wire(b)
        for b in (content_raw if isinstance(content_raw, list) else [])
        if isinstance(b, Mapping)
    ]
    stop_reason = data.get("stop_reason")
    provider = data.get("provider")
    return MessagesResponse(
        content=content,
        stop_reason=stop_reason if isinstance(stop_reason, str) else None,
        usage=MessagesUsage(
            input_tokens=_int(usage.get("input_tokens")),
            output_tokens=_int(usage.get("output_tokens")),
            cache_read_input_tokens=_int(usage.get("cache_read_input_tokens")),
            cache_creation_input_tokens=_int(usage.get("cache_creation_input_tokens")),
            reported=usage or None,
        ),
        provider=provider if isinstance(provider, str) else None,
    )


def classify_error_response(resp: httpx.Response) -> Exception:
    """A non-2xx, non-transient ``/v1/messages`` response, classified the way
    the SDK path's ``translate_or_reraise`` classifies an ``APIStatusError``:
    402 / 401 / 403 / 5xx keep their typed provider errors; anything else
    (400 bad request, 404 unknown model, 422) is OUR bug and becomes
    ``LLMRequestRejectedError`` (500 + Sentry, generic body) rather than a raw
    exception whose text a route could serialise (#1066)."""
    translated = translate_api_status_error(resp)
    if translated is not None:
        return translated
    detail = ""
    try:
        payload = resp.json()
        err = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(err, Mapping):
            detail = f"{err.get('type')}: {str(err.get('message'))[:200]}"
    except ValueError:
        detail = resp.text[:200]
    return LLMRequestRejectedError(
        f"OpenRouter /v1/messages rejected the request (status={resp.status_code}): {detail!r}",
        upstream_code=resp.status_code,
    )


class RawMessagesTransport:
    """``MessagesTransport`` over httpx against OpenRouter's ``/v1/messages``."""

    transport_id: TransportId = "messages_http"

    def __init__(
        self,
        *,
        api_key: str | None,
        timeout: float,
        max_retries: int,
        base_url: str,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = base_url.rstrip("/") + "/v1/messages"
        self._api_key = api_key or ""
        self._timeout = timeout
        self._max_retries = max_retries
        self._http = http  # injected in tests (httpx.MockTransport); pooled per instance otherwise
        # Sent on EVERY request by the transport itself, never left to the
        # client's defaults: an injected client (tests, the parity probe) must
        # not be able to supply them on the transport's behalf.
        self._headers = {
            "x-api-key": self._api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(self._timeout, connect=10.0))
        return self._http

    async def create(self, **params: Any) -> MessagesResponse:
        body = wire_body(params)
        resp = await post_json_with_retry(
            self._client(), self._url, body, max_retries=self._max_retries, headers=self._headers
        )
        if resp.status_code < 200 or resp.status_code >= 300:
            raise classify_error_response(resp)
        try:
            data = resp.json()
        except ValueError as exc:
            raise LLMUpstreamUnavailableError() from exc
        if not isinstance(data, dict):
            raise LLMUpstreamUnavailableError()
        if data.get("type") == "error":
            # Not observed on this endpoint (errors arrive as real 4xx); if a
            # 200 ever carries an envelope, surface it as a server fault so it
            # gets classified on evidence rather than retried blindly.
            err = data.get("error")
            msg = str(err.get("message"))[:200] if isinstance(err, Mapping) else str(err)[:200]
            raise LLMRequestRejectedError(
                f"OpenRouter /v1/messages returned 200 with an error envelope: {msg!r}",
                reason="unclassified_error_envelope",
            )
        return response_from_wire(data)

    async def stream(self, **params: Any) -> AsyncIterator[StreamEvent]:
        raise NotImplementedError(
            "raw Messages streaming lands in #1067 PR C; stream purposes stay on the SDK"
        )
        yield  # type: ignore[unreachable]  # pragma: no cover - async-generator marker
