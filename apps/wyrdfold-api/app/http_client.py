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
# The poller runs up to 10 source workers concurrently (the historical
# ``POLL_CONCURRENCY`` upper bound; the live value was later lowered to
# bound the Supabase write herd — see app/services/poller.py). The
# SmartRecruiters and Workday fetchers each fan out
# ``_DETAIL_CONCURRENCY = 5`` per-posting detail fetches through THIS
# shared client. Worst case is all 10 workers being SR/Workday at once:
# 10 x 5 = 50 simultaneous detail requests. The previous ceiling of 20
# meant ~30 of those queued behind the limit and timed out under the 15 s
# deadline, silently dropping postings.
#
# We deliberately size for the 10-worker upper bound (not the current,
# lower POLL_CONCURRENCY) so a future bump back to 10 needs no pool
# resize; a lower live value just leaves more headroom. Plus headroom for
# the other callers that share this client: the scheduler tick, ad-hoc
# user-paste URL fetches, and source-discovery probes. (Per-user Supabase
# traffic uses a SEPARATE httpx pool in app/supabase_pool.py and is not
# counted here.)
#
#   50 (poll detail fan-out, 10-worker upper bound) + 14 (headroom) = 64
_POLL_DETAIL_FANOUT = 10 * 5  # max-POLL_CONCURRENCY x max(_DETAIL_CONCURRENCY)
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


# SSRF-safe pooled client for user-supplied / externally-influenced URL
# fetches. Same pool/timeout config as the default client, but its transport
# pins the validated resolved IP at connect time so a DNS rebind between the
# ``assert_safe_host`` check and the socket connect can't land on an internal
# address (#29 R1 M2 / #192). Separate from the poller's ``_client`` so the hot
# poll path is unchanged; used by ``get_with_size_cap``'s gated path.
_safe_client: httpx.AsyncClient | None = None


def get_safe_http_client() -> httpx.AsyncClient:
    global _safe_client
    if _safe_client is None or _safe_client.is_closed:
        from app.services.safe_http import build_ssrf_safe_transport

        _safe_client = httpx.AsyncClient(
            transport=build_ssrf_safe_transport(),
            timeout=HTTP_TIMEOUT,
            limits=httpx.Limits(
                max_connections=MAX_CONNECTIONS,
                max_keepalive_connections=MAX_KEEPALIVE_CONNECTIONS,
            ),
            follow_redirects=True,
            headers={"User-Agent": DEFAULT_USER_AGENT},
        )
    return _safe_client


async def close_safe_http_client() -> None:
    global _safe_client
    if _safe_client and not _safe_client.is_closed:
        await _safe_client.aclose()
    _safe_client = None


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
    if validate_host is None:
        # Back-compat path: no SSRF gating requested (e.g. fixed internal
        # hosts). Single request on the default client; it follows redirects.
        async with get_http_client().stream("GET", url) as resp:
            return resp, await _read_body_capped(resp, max_bytes)

    # SSRF-gated path: use the IP-pinning client so a rebind between the
    # per-hop ``validate_host`` check below and the connect can't reach an
    # internal address (#192 R1 M2). The manual per-hop check stays — it
    # rejects fast, before any socket, with a clean message.
    client = get_safe_http_client()
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
                    raise UnsafeURLError(f"redirect to non-http(s) scheme: {current.scheme!r}")
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

# Upper bound on a honored ``Retry-After``. A remote server (a job board we
# don't control, sometimes a user-supplied careers URL) could otherwise send
# ``Retry-After: 86400`` and park a poll worker until the poll-cycle watchdog
# cancels the whole cycle — starving every source not yet polled that tick
# (hardening review 2026-07-21, Perf-F3). Sits well under the cycle timeout yet
# above any legitimate short back-off.
_MAX_RETRY_AFTER_S = 60.0


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

        retry_after = _retry_after_seconds(resp)
        if retry_after is None:
            delay = _backoff_seconds(attempt, backoff_base, backoff_cap)
        else:
            # Honor Retry-After, but clamp so no single server can park the
            # worker arbitrarily long (Perf-F3). Explicit None check (not `or`)
            # so a legitimate ``Retry-After: 0`` means "retry now", not "fall
            # back to backoff".
            delay = min(retry_after, _MAX_RETRY_AFTER_S)
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


def json_or_none(resp: httpx.Response, *, source: str) -> Any | None:
    """Decode a JSON body, or ``None`` when the response wasn't JSON at all.

    Every ATS fetcher called ``resp.json()`` bare on a 200. That is fine until a
    board answers 200 with something that isn't JSON — an HTML interstitial, a
    WAF challenge, a maintenance page, an empty body — and then the poll dies
    with ``JSONDecodeError: Expecting value: line 1 column 1 (char 0)`` raised
    six frames deep inside httpx. Prod logged that repeatedly against Workday
    boards; it reads as a code bug in the traceback when it is really an
    upstream serving HTML.

    Returning ``None`` rather than a partial harvest is the load-bearing part —
    a partial result would sail straight past the poller's stale-archive guard
    and delist live rows.

    This is the seam the PER-POSTING DETAIL fetchers use: they warn and drop
    that one posting. LIST fetches go through ``board_list_json`` below, which
    turns the same ``None`` into a ``BoardFetchError`` — an unparseable list
    response is a failed fetch, not a board with no open roles.
    """
    try:
        return resp.json()
    except ValueError:  # json.JSONDecodeError subclasses ValueError
        body = resp.text[:120].replace("\n", " ")
        logger.warning(
            "%s returned %d with a non-JSON body (content-type=%r): %r",
            source,
            resp.status_code,
            resp.headers.get("content-type"),
            body,
        )
        return None


class BoardFetchError(Exception):
    """An ATS **list** fetch did not come back with a usable board listing.

    Every list fetcher used to collapse every failure mode — 404, 410, 422, a
    5xx that outlived its retries, a WAF challenge served as 200/text-html —
    into ``return []``. The poller cannot tell that apart from "this board has
    no open roles today", so it recorded a SUCCESSFUL poll and reset
    ``consecutive_failures`` to 0. ``_record_source_failure`` only ever fires
    from an exception handler, so ``source_failure_disable_threshold`` could
    never fire for this failure class and a dead board was re-polled every
    cycle forever (prod: comcast answering 410 Gone).

    Raising instead routes the failure into the accounting the poller already
    has. Deliberately NOT raised for a 200 that carries zero postings: that is
    a legitimately empty board and must keep resetting the counter.

    ``status`` is the HTTP status we saw, or ``None`` when the request never
    produced a response (transport failure past its retries).
    """

    def __init__(self, message: str, *, source: str, status: int | None = None) -> None:
        super().__init__(message)
        self.source = source
        self.status = status


def board_list_json(resp: httpx.Response, *, source: str) -> Any:
    """Decode an ATS **list** response, or raise ``BoardFetchError``.

    The single gate every list fetcher runs its response through, so "the board
    answered with a listing" is judged identically across providers.

    Note what is NOT handled here: 408/425/429/5xx never reach this function on
    their first occurrence — ``request_with_retry`` retries them with backoff
    and only raises ``FetchExhaustedError`` once they are spent. That is the
    transient-vs-sustained split; a blip costs no failure count at all, and a
    sustained outage still has to clear the disable threshold on top.
    """
    if resp.status_code != 200:
        raise BoardFetchError(
            f"{source} returned {resp.status_code}",
            source=source,
            status=resp.status_code,
        )
    data = json_or_none(resp, source=source)
    if data is None:
        raise BoardFetchError(
            f"{source} returned 200 with a non-JSON body",
            source=source,
            status=resp.status_code,
        )
    return data
