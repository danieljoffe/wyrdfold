"""One HTTP POST-with-retry for both OpenRouter wire shapes (#1067 item 1).

The OpenAI-shaped path used to carry its own loop: a fixed status set on a
fixed backoff, no ``Retry-After``. Moving the Claude routes off the SDK onto
that loop would have been a downgrade (the SDK honours ``retry-after`` and
retries 408/409/429/5xx with jitter). This helper reuses the clamped, jittered
``Retry-After`` primitives from ``app.http_client`` so both shapes get the
same, SDK-equivalent policy.

Contract: returns the final ``httpx.Response`` for the caller to classify
(2xx, or a non-transient error status such as 400/402/404) and raises
``LLMUpstreamUnavailableError`` only when retries are spent on a transient
status or on a transport error. Classification (typed provider conditions,
request rejections, envelopes) stays with the caller, where the wire shape is
known.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

import httpx

from app.http_client import _MAX_RETRY_AFTER_S, _backoff_seconds, _retry_after_seconds
from app.services.llm.errors import LLMUpstreamUnavailableError, translate_api_status_error

logger = logging.getLogger(__name__)

# 408/425 (request timeout / too early) join the LLM set the OpenAI path
# already retried; 529 is Anthropic's "overloaded".
TRANSIENT_STATUSES: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504, 529})
_BACKOFF_BASE_SECONDS = 0.5
_BACKOFF_CAP_SECONDS = 8.0

# Indirection so tests can patch the sleep without touching asyncio.
_sleep = asyncio.sleep


async def post_json_with_retry(
    client: httpx.AsyncClient,
    url: str,
    body: dict[str, Any],
    *,
    max_retries: int,
    headers: Mapping[str, str] | None = None,
) -> httpx.Response:
    """POST ``body`` as JSON; retry transient statuses and transport errors.

    Delay between attempts honours ``Retry-After`` when the server sends one
    (integer seconds, clamped to ``_MAX_RETRY_AFTER_S``), else exponential
    backoff with jitter. ``max_retries`` is the number of RETRIES, so the
    call is attempted ``max_retries + 1`` times.
    """
    last: httpx.Response | None = None
    for attempt in range(max_retries + 1):
        try:
            # ``headers`` only when the caller owns them (the raw Messages
            # transport); the OpenAI shape relies on its client's defaults.
            resp = await (
                client.post(url, json=body, headers=headers)
                if headers is not None
                else client.post(url, json=body)
            )
        except httpx.TransportError as exc:  # timeouts + connection errors
            if attempt < max_retries:
                await _sleep(_backoff_seconds(attempt, _BACKOFF_BASE_SECONDS, _BACKOFF_CAP_SECONDS))
                continue
            raise LLMUpstreamUnavailableError() from exc
        if resp.status_code not in TRANSIENT_STATUSES:
            return resp
        last = resp
        if attempt >= max_retries:
            break
        retry_after = _retry_after_seconds(resp)
        delay = (
            min(retry_after, _MAX_RETRY_AFTER_S)
            if retry_after is not None
            else _backoff_seconds(attempt, _BACKOFF_BASE_SECONDS, _BACKOFF_CAP_SECONDS)
        )
        logger.info(
            "openrouter transient status=%s attempt=%d/%d; retrying in %.2fs",
            resp.status_code,
            attempt + 1,
            max_retries + 1,
            delay,
        )
        await _sleep(delay)
    # Retries spent on a transient status: classify the LAST response the way
    # the SDK path classifies its final APIStatusError, so a 429 stays
    # ``LLMRateLimitedError`` (``fit_refresh`` stops its sweep on exactly that
    # class) and a 5xx/529 stays ``LLMUpstreamUnavailableError``.
    if last is None:  # pragma: no cover - the loop always sets it before breaking
        raise LLMUpstreamUnavailableError()
    translated = translate_api_status_error(last)
    if translated is not None:
        raise translated
    raise LLMUpstreamUnavailableError(upstream_status=last.status_code)
