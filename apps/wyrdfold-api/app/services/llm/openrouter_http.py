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

# The SDK's retry set: 408 (request timeout), 409 (conflict), 429 (rate
# limit) and every 5xx, including 529 (Anthropic's "overloaded"); 425 (too
# early) joins because ``app.http_client`` already treats it that way.
# ``should_retry`` also honours the ``x-should-retry`` response header in both
# directions, as the SDK does, and that header wins over the status.
TRANSIENT_STATUSES: frozenset[int] = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})
_BACKOFF_BASE_SECONDS = 0.5
_BACKOFF_CAP_SECONDS = 8.0

# Indirection so tests can patch the sleep without touching asyncio.
_sleep = asyncio.sleep


def should_retry(resp: httpx.Response) -> bool:
    """The SDK-equivalent retry decision for one response.

    ``x-should-retry: true`` forces a retry of any status and
    ``x-should-retry: false`` forbids one, exactly as the Anthropic SDK's
    ``_should_retry`` does; without the header, 408/409/425/429 and every 5xx
    are transient. Callers classify whatever is NOT retried.
    """
    # A success is never retried, whatever the header says: the SDK consults
    # ``x-should-retry`` only for error-status responses, and re-POSTing a
    # completed call would bill a duplicate completion (release gate 2026-09-18).
    if resp.is_success:
        return False
    directive = resp.headers.get("x-should-retry", "").strip().lower()
    if directive == "true":
        return True
    if directive == "false":
        return False
    return resp.status_code in TRANSIENT_STATUSES or resp.status_code >= 500


def retry_delay(resp: httpx.Response, attempt: int) -> float:
    """Seconds to wait before retrying ``resp``: ``Retry-After`` when the
    server sends one within the SDK's window (0 < seconds <= 60), else
    exponential backoff with jitter. A ``Retry-After`` beyond the window is
    IGNORED rather than clamped, as the Anthropic SDK does: a server asking
    for an hour gets the backoff, not a sixty-second park."""
    retry_after = _retry_after_seconds(resp)
    if retry_after is not None and 0 < retry_after <= _MAX_RETRY_AFTER_S:
        return retry_after
    return _backoff_seconds(attempt, _BACKOFF_BASE_SECONDS, _BACKOFF_CAP_SECONDS)


async def post_json_with_retry(
    client: httpx.AsyncClient,
    url: str,
    body: dict[str, Any],
    *,
    max_retries: int,
    headers: Mapping[str, str] | None = None,
) -> httpx.Response:
    """POST ``body`` as JSON; retry per ``should_retry`` and on transport errors.

    Delay between attempts is ``retry_delay``: ``Retry-After`` when the server
    sends one within the SDK's 60-second window, else exponential backoff
    with jitter. ``max_retries`` is the number of RETRIES, so the
    call is attempted ``max_retries + 1`` times. When retries are spent, the
    last response is classified: 429 stays ``LLMRateLimitedError``, 5xx stays
    ``LLMUpstreamUnavailableError``, and an exhausted 408/409/425 is reported
    as upstream-unavailable too (the SDK would raise its 4xx error class
    there; a 503 "try again" is the kinder outcome for a status that was, by
    definition, transient).
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
        if not should_retry(resp):
            return resp
        last = resp
        if attempt >= max_retries:
            break
        delay = retry_delay(resp, attempt)
        # WARNING, not INFO: prod hides INFO, and a retry storm must be
        # visible in the logs rather than surfacing only as per-source
        # budget cancellations downstream.
        logger.warning(
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
