"""Shared httpx.AsyncClient with connection pooling.

Reuses TCP connections across ATS fetcher calls instead of creating
a fresh client per request. Closed on app shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable
from typing import Any, cast

import httpx

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None

# Identifies us to third-party job-board APIs. Some boards (Workday in
# particular) reject the default httpx UA outright.
DEFAULT_USER_AGENT = "wyrdfold-jobs/1.0 (+https://wyrdfold.com)"

# Connection-pool ceiling sized to the real fan-out of a poll cycle.
#
# The poller runs ``POLL_CONCURRENCY = 10`` source workers concurrently
# (app/services/poller.py). The SmartRecruiters and Workday fetchers each
# fan out ``_DETAIL_CONCURRENCY = 5`` per-posting detail fetches through
# THIS shared client. Worst case is all 10 workers being SR/Workday at
# once: 10 x 5 = 50 simultaneous detail requests. The previous ceiling of
# 20 meant ~30 of those queued behind the limit and timed out under the
# 15 s deadline, silently dropping postings.
#
# We size for that worst case plus headroom for the other callers that
# share this client: the scheduler tick, ad-hoc user-paste URL fetches,
# and source-discovery probes. (Per-user Supabase traffic uses a SEPARATE
# httpx pool in app/supabase_pool.py and is not counted here.)
#
#   50 (poll detail fan-out) + 14 (headroom) = 64
_POLL_DETAIL_FANOUT = 10 * 5  # POLL_CONCURRENCY x max(_DETAIL_CONCURRENCY)
MAX_CONNECTIONS = _POLL_DETAIL_FANOUT + 14  # = 64
MAX_KEEPALIVE_CONNECTIONS = 20

# Explicit per-phase timeouts. The single 15 s number used to govern
# every phase, including ``pool`` (waiting for a free connection). With
# the pool saturated that wait silently ate into the read budget and
# surfaced as an opaque timeout. Splitting the phases means a
# pool-acquisition stall raises ``PoolTimeout`` (a distinct, retryable
# transport error) instead of masquerading as a slow read — and with the
# ceiling sized above it should not trigger in normal operation.
HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=15.0, write=15.0, pool=5.0)


def get_http_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=HTTP_TIMEOUT,
            limits=httpx.Limits(
                max_connections=MAX_CONNECTIONS,
                max_keepalive_connections=MAX_KEEPALIVE_CONNECTIONS,
            ),
            follow_redirects=True,
            headers={"User-Agent": DEFAULT_USER_AGENT},
        )
    return _client


async def close_http_client() -> None:
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
    _client = None


# ---- User-URL fetch with size cap ------------------------------------------

# Hard cap on the response-body size we'll accept from a URL the user
# pasted in. Real Greenhouse / Lever / Workday job pages are tens of
# KB; 5 MB leaves ample headroom for one-off oddities while still
# refusing a multi-GB payload that would OOM the API. The 15s
# ``timeout`` on the shared client doesn't help here — a fast CDN can
# stream gigabytes within 15 seconds, and ``client.get()`` would
# buffer the entire body into memory before returning.
MAX_USER_FETCH_BYTES = 5 * 1024 * 1024


class ResponseTooLargeError(Exception):
    """Raised by ``get_with_size_cap`` when the body exceeds the cap.

    Carries the size we observed (``Content-Length`` advertised, or
    streamed bytes before we aborted) so callers can include it in
    user-facing error messages.
    """

    def __init__(self, message: str, *, size: int, limit: int) -> None:
        super().__init__(message)
        self.size = size
        self.limit = limit


class UnsafeURLError(Exception):
    """Raised by ``get_with_size_cap`` when the initial URL or any redirect
    hop fails the supplied ``validate_host`` check (an SSRF guard).

    Distinct from ``httpx.HTTPError`` so callers can map it to a 4xx
    ("refused for safety") rather than a generic fetch failure.
    """


# 3xx statuses that carry a ``Location`` we would otherwise auto-follow.
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})


async def _read_body_capped(resp: httpx.Response, max_bytes: int) -> bytes:
    """Stream ``resp``'s body, enforcing ``max_bytes``.

    Pre-checks ``Content-Length`` when present (cheap fail-fast), then
    enforces the cap against the actually-streamed byte count (catches
    missing or lying ``Content-Length`` headers). Raises
    ``ResponseTooLargeError`` if either trips.
    """
    advertised = resp.headers.get("content-length")
    if advertised is not None and advertised.isdigit():
        n = int(advertised)
        if n > max_bytes:
            raise ResponseTooLargeError(
                f"Content-Length {n} exceeds cap {max_bytes}",
                size=n,
                limit=max_bytes,
            )
    chunks: list[bytes] = []
    total = 0
    async for chunk in resp.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise ResponseTooLargeError(
                f"Streamed {total} bytes exceeds cap {max_bytes}",
                size=total,
                limit=max_bytes,
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def get_with_size_cap(
    url: str,
    *,
    max_bytes: int = MAX_USER_FETCH_BYTES,
    validate_host: Callable[[str], None] | None = None,
    max_redirects: int = 10,
) -> tuple[httpx.Response, bytes]:
    """GET ``url`` reading at most ``max_bytes`` of the body.

    Streams the response so a user-pasted URL pointing to a huge
    payload (GB-scale CDN downloads, infinite-stream endpoints) can't
    OOM the API the way ``client.get()`` would — its default behavior
    is to buffer the entire body before returning.

    SSRF (#110): when ``validate_host`` is given, redirects are followed
    **manually** with ``follow_redirects=False`` and ``validate_host`` is
    invoked against every hop's host *before* we connect — including each
    redirect target. This closes the gap left by httpx's built-in redirect
    following, which connects to an internal redirect target before any
    post-fetch host check can run. ``validate_host`` should raise on a
    disallowed host (e.g. ``app.services.validate.assert_safe_host``); the
    raise is surfaced as ``UnsafeURLError``, and a redirect to a non-http(s)
    scheme is rejected the same way. Without ``validate_host`` the behaviour
    is unchanged: a single request, redirects handled by the shared client.

    Raises ``ResponseTooLargeError`` past the size cap, ``UnsafeURLError``
    on a rejected host/scheme, and ``httpx.TooManyRedirects`` past
    ``max_redirects``. Network failures propagate as ``httpx.HTTPError``.

    Returns ``(response, body_bytes)``; the response's ``.text`` /
    ``.content`` are empty (stream consumed manually) — use ``body_bytes``.
    ``.status_code``, ``.url``, and ``.headers`` remain valid.
    """
    client = get_http_client()

    if validate_host is None:
        # Back-compat path: no SSRF gating requested (e.g. fixed internal
        # hosts). Single request; the shared client follows redirects.
        async with client.stream("GET", url) as resp:
            return resp, await _read_body_capped(resp, max_bytes)

    current = httpx.URL(url)
    for _ in range(max_redirects + 1):
        # Gate each hop BEFORE connecting. With follow_redirects=False httpx
        # connects only to ``current``, so validating current.host here
        # covers redirect targets too — not just the first/final URL.
        try:
            validate_host(current.host or "")
        except ValueError as exc:
            raise UnsafeURLError(str(exc)) from exc
        async with client.stream("GET", current, follow_redirects=False) as resp:
            if resp.status_code in _REDIRECT_CODES and "location" in resp.headers:
                current = current.join(resp.headers["location"])
                if current.scheme not in ("http", "https"):
                    raise UnsafeURLError(
                        f"redirect to non-http(s) scheme: {current.scheme!r}"
                    )
                continue
            return resp, await _read_body_capped(resp, max_bytes)

    raise httpx.TooManyRedirects(
        f"exceeded {max_redirects} redirects fetching {url}",
        request=httpx.Request("GET", url),
    )


# ---- Retry helper ----------------------------------------------------------

# 429 + 5xx are treated as transient and retried with exponential backoff.
# Other 4xx (401/403/404/422) are returned to the caller without retry — the
# caller decides whether to swallow (e.g. 404 = empty board) or surface.
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class FetchExhaustedError(Exception):
    """Raised by ``request_with_retry`` when all retry attempts fail.

    Carries the last response (if any) and the last exception so callers
    can inspect the failure mode without re-running the request.
    """

    def __init__(
        self,
        message: str,
        *,
        last_response: httpx.Response | None = None,
        last_exception: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.last_response = last_response
        self.last_exception = last_exception


# Module-level sleep alias so tests can patch it without touching the
# whole asyncio module. Production paths use ``asyncio.sleep`` directly.
_sleep = asyncio.sleep


async def request_with_retry(
    method: str,
    url: str,
    *,
    retries: int = 2,
    backoff_base: float = 1.0,
    backoff_cap: float = 8.0,
    timeout: float | None = None,  # noqa: ASYNC109 — forwarded to httpx, not asyncio.timeout
    **kwargs: Any,
) -> httpx.Response:
    """Issue an HTTP request with retries on transient failures.

    Retries on network errors and on 408/425/429/5xx with exponential
    backoff (``backoff_base * 2**attempt`` seconds, capped at
    ``backoff_cap``, plus up to 250 ms of jitter). Honors ``Retry-After``
    on 429 when the server provides it.

    Returns the final ``httpx.Response`` (which may itself be a non-2xx
    response if the status is non-retryable, e.g. 404). Raises
    ``FetchExhaustedError`` only when retries are spent on a transient
    failure, so callers don't have to distinguish "real 404" from "we
    gave up after 3 tries."
    """
    last_response: httpx.Response | None = None
    last_exc: Exception | None = None
    client = get_http_client()

    request_kwargs = dict(kwargs)
    if timeout is not None:
        request_kwargs["timeout"] = timeout

    method_lower = method.lower()
    method_func = getattr(client, method_lower, None)
    if method_func is None:
        raise ValueError(f"unsupported HTTP method: {method}")

    for attempt in range(retries + 1):
        try:
            resp = await method_func(url, **request_kwargs)
        except httpx.HTTPError as exc:
            # ``HTTPError`` is the umbrella for transport failures
            # (``TimeoutException``, ``NetworkError``, etc.). We never call
            # ``raise_for_status`` ourselves, so ``HTTPStatusError`` doesn't
            # reach this branch — non-2xx flows through the status-code check
            # below.
            last_exc = exc
            last_response = None
            if attempt == retries:
                break
            await _sleep(_backoff_seconds(attempt, backoff_base, backoff_cap))
            continue

        if resp.status_code not in _RETRYABLE_STATUS:
            return cast(httpx.Response, resp)

        last_response = resp
        last_exc = None
        if attempt == retries:
            break

        delay = _retry_after_seconds(resp) or _backoff_seconds(attempt, backoff_base, backoff_cap)
        logger.warning(
            "retrying %s %s after %s in %.2fs (attempt %d/%d)",
            method,
            url,
            resp.status_code,
            delay,
            attempt + 1,
            retries + 1,
        )
        await _sleep(delay)

    raise FetchExhaustedError(
        f"{method} {url} exhausted retries",
        last_response=last_response,
        last_exception=last_exc,
    )


def _backoff_seconds(attempt: int, base: float, cap: float) -> float:
    raw: float = base * (2**attempt)
    capped: float = raw if raw < cap else cap
    jitter: float = random.uniform(0, 0.25)  # noqa: S311 — non-cryptographic jitter
    return capped + jitter


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    """Parse a ``Retry-After`` header. Honors integer-seconds form only.

    HTTP-date form is ignored — it would need ``email.utils.parsedate_to_datetime``
    and a clock comparison, and job-board APIs that send ``Retry-After`` use
    the integer-seconds form in practice.
    """
    raw = resp.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None
