"""Poll-cycle DB routing — the async/sync seam + the write-herd cap.

Every write the poll cycle issues routes through :func:`poll_db_write`, and
every direct read through :func:`poll_db_read`. Both run natively on the event
loop via the pooled HTTP/2 ``AsyncClient`` (#225) — async I/O doesn't occupy an
executor thread, so the poll's DB fan-out stops starving the threads that
interactive requests need (the #57 regression this targets). The
``POLLER_ASYNC_DB`` flag that once gated this was removed in slice 4: the poll
cycle is unconditionally async now. FAIL-SAFE: when the async client isn't up,
the call silently falls back to the sync client in a thread (the #107 path), so
a query is never dropped — in practice prod always has the async client.

Writes additionally retry transient transport blips (idempotent writes only —
see :mod:`app.services.supabase_retry`) and are bounded by
``DB_WRITE_CONCURRENCY`` on both paths so the burst can't thundering-herd the
Supabase pooler. Reads skip the write semaphore (they never did hold it on the
sync path, and coupling read latency to the write herd would slow the cycle);
their concurrency is bounded by the source fan-out itself plus, on the async
path, the client's connection limits.

The semaphore + thread-runner live here (not in the poller) so every service
module that issues poll queries can share them without importing the poller.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from app.services.supabase_retry import execute_with_retry, execute_with_retry_sync
from app.supabase_pool import get_async_supabase

# Hard ceiling on concurrent supabase writes across the WHOLE poll cycle. The
# Stage-1/Stage-2 scoring loops ``asyncio.gather`` one write per (row x
# target), unbounded — across the source fan-out that is a burst of hundreds of
# simultaneous writes against one shared client, which is what drops the
# Supabase pooler connection (``Broken pipe`` / ``Server disconnected``). Every
# poll write routes through ``poll_db_write`` (or the raw ``db_to_thread``), so
# this global semaphore caps the burst regardless of how the fan-out is shaped.
DB_WRITE_CONCURRENCY = 12

# Per-event-loop write semaphore. Created lazily and keyed by the running loop
# so a fresh loop (each test, or a re-created worker loop) gets its own rather
# than one bound to a dead loop.
_db_write_sems: dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}


def _db_write_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _db_write_sems.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(DB_WRITE_CONCURRENCY)
        _db_write_sems[loop] = sem
    return sem


async def db_to_thread(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a blocking supabase call in a thread under the cycle-wide write
    semaphore, so the poll's write fan-out can't thundering-herd the pooler.
    Preserves the #107 ``to_thread`` convention (the blocking call never
    touches the event loop)."""
    async with _db_write_semaphore():
        return await asyncio.to_thread(fn, *args, **kwargs)


async def poll_db_write(
    supabase: Any,
    build: Callable[..., Any],
    *,
    label: str,
) -> Any:
    """Execute one poll-cycle write, async-on-loop (sync-in-thread fallback).

    ``build(client)`` receives a supabase client — the sync ``Client`` passed
    in ``supabase`` or the pooled ``AsyncClient`` — and returns a *built,
    unexecuted* postgrest query. supabase-py's sync and async query builders
    share the same chainable API, so one ``build`` closure works against
    either; this seam owns the only real differences (await vs. thread, and the
    matching retry variant). Returns the query's ``execute()`` response so
    callers can read ``.data`` / ``.count``.

    Backend selection + the fail-safe are described in the module docstring.
    Both paths are bounded by the write semaphore and retry transient blips —
    so use this only for idempotent writes (the poll's upserts / stable-WHERE
    updates all are).
    """
    async_sb = get_async_supabase()
    if async_sb is not None:
        async with _db_write_semaphore():
            return await execute_with_retry(build(async_sb).execute, label=label)
    # Fail-safe only: prod always has the async client (the flag was removed in
    # #57 slice 4 — the poll cycle is unconditionally async now). This sync path
    # survives for tests/local runs that don't init the async client.
    return await db_to_thread(lambda: execute_with_retry_sync(build(supabase).execute, label=label))


async def poll_db_read(
    supabase: Any,
    build: Callable[..., Any],
    *,
    label: str,
    retry_sync: bool = False,
) -> Any:
    """Execute one poll-cycle read, async-on-loop (sync-in-thread fallback).

    Same ``build(client)`` contract as :func:`poll_db_write`, minus the write
    semaphore: poll reads never held it on the sync path (only the write herd
    is capped), and serializing reads behind the write burst would slow the
    cycle for no pooler benefit — read concurrency is already bounded by the
    source fan-out (``POLL_CONCURRENCY``) and, on the async path, the pooled
    client's connection limits.

    ``retry_sync`` mirrors the pre-seam behavior of each call site: the few
    reads that already wrapped ``execute_with_retry_sync`` keep their retry on
    the sync path; the rest stay bare so the sync-fallback path is byte-for-byte
    today's behavior. The async path always retries — a re-issued read is
    harmless and the pooled h2 connection is where transient stream drops
    live.
    """
    async_sb = get_async_supabase()
    if async_sb is not None:
        return await execute_with_retry(build(async_sb).execute, label=label)
    if retry_sync:
        return await asyncio.to_thread(
            lambda: execute_with_retry_sync(build(supabase).execute, label=label)
        )
    return await asyncio.to_thread(build(supabase).execute)
