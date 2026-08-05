"""Retry transient HTTP failures on supabase-py calls.

supabase-py runs over httpx against the Cloudflare-fronted Supabase REST
endpoint. The poller flushes many sources in parallel, and even with the
service-role transport pinned to HTTP/1.1 (see ``app.supabase_pool`` — the
real fix for the HTTP/2 stream-corruption storm) a busy pooler can still
drop a connection mid-flight, surfaced as ``httpx.RemoteProtocolError:
Server disconnected`` or a broken pipe. The underlying request was
idempotent in every place we use this helper (upsert with ON CONFLICT,
UPDATE with a stable WHERE), so re-issuing the call is safe.

Each callsite wraps the bound ``.execute`` method of a built
postgrest query so the retry re-runs the same request without
rebuilding it.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

import httpx
from postgrest.exceptions import APIError

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Transient transport-level failures we'll retry. ``HTTPStatusError`` is
# deliberately excluded — those are protocol-level rejections (e.g. 4xx
# constraint violations) that retrying won't help.
_TRANSIENT_HTTP: tuple[type[Exception], ...] = (
    httpx.RemoteProtocolError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.ConnectError,
    httpx.TimeoutException,
)

# Postgres "canceling statement due to statement timeout", surfaced by
# PostgREST as an APIError with this SQLSTATE. Retryable ONLY on opt-in:
# under a contention spike a re-run after backoff often succeeds (the
# 2026-08-05 drive saw the same insights read fail then pass 23s later,
# #604), but a retry re-burns the full statement budget — hot write paths
# must not double their load silently.
_STATEMENT_TIMEOUT_CODE = "57014"


def is_statement_timeout(exc: BaseException) -> bool:
    """True when *exc* is PostgREST's surfacing of Postgres 57014."""
    return isinstance(exc, APIError) and getattr(exc, "code", None) == _STATEMENT_TIMEOUT_CODE


def _backoff_delay(attempt: int, base: float, cap: float) -> float:
    raw = base * (2**attempt)
    jitter: float = random.uniform(0, 0.15)  # noqa: S311 — non-cryptographic jitter
    return (raw if raw < cap else cap) + jitter


def execute_with_retry_sync(
    fn: Callable[[], T],
    *,
    label: str,
    retries: int = 2,
    backoff_base: float = 0.4,
    backoff_cap: float = 4.0,
) -> T:
    """Synchronous retry wrapper — call from inside an ``asyncio.to_thread``
    or any sync context. Retries on transient httpx failures with
    exponential backoff + jitter.
    """
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return fn()
        except _TRANSIENT_HTTP as exc:
            last_exc = exc
            if attempt == retries:
                logger.warning("supabase %s exhausted %d retries: %s", label, retries, exc)
                raise
            delay = _backoff_delay(attempt, backoff_base, backoff_cap)
            # Down-ranked to debug (was warning): under a concurrent poll
            # burst a transient blip retries on many rows at once, and one
            # warning per attempt floods Railway's 500-logs/sec budget. The
            # exhaustion case above stays at warning — that's the line that
            # actually signals a write we failed to land.
            logger.debug(
                "supabase %s: %s (attempt %d/%d), retrying in %.2fs",
                label,
                exc,
                attempt + 1,
                retries + 1,
                delay,
            )
            time.sleep(delay)
    raise last_exc or RuntimeError("unreachable")


async def execute_with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    label: str,
    retries: int = 2,
    backoff_base: float = 0.4,
    backoff_cap: float = 4.0,
    retry_statement_timeout: bool = False,
) -> T:
    """Await an async supabase-py call, retrying transient transport blips
    natively on the event loop (no executor thread). ``fn`` is the bound
    ``.execute`` of a built ``AsyncClient`` query — awaiting it does the I/O
    on the loop, which is the point for the #57 poll-write path (see
    ``app.services.db_write.poll_db_write``). The backoff is non-blocking
    (``asyncio.sleep``).

    To retry a *sync* call from async code, wrap ``execute_with_retry_sync``
    in ``db_to_thread`` instead — that path keeps the blocking call in a
    thread. Same transient/permanent split as the sync variant.

    ``retry_statement_timeout=True`` additionally retries Postgres 57014
    (statement timeout) with the same backoff — opt-in per call site; see
    ``is_statement_timeout`` for why it is not on by default.
    """
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return await fn()
        except Exception as exc:
            retryable = isinstance(exc, _TRANSIENT_HTTP) or (
                retry_statement_timeout and is_statement_timeout(exc)
            )
            if not retryable:
                raise
            last_exc = exc
            if attempt == retries:
                logger.warning("supabase %s exhausted %d retries: %s", label, retries, exc)
                raise
            delay = _backoff_delay(attempt, backoff_base, backoff_cap)
            logger.debug(
                "supabase %s: %s (attempt %d/%d), retrying in %.2fs",
                label,
                exc,
                attempt + 1,
                retries + 1,
                delay,
            )
            await asyncio.sleep(delay)
    raise last_exc or RuntimeError("unreachable")
