"""Poll-cycle DB-write routing — the async/sync seam + the write-herd cap.

Every write the poll cycle issues routes through :func:`poll_db_write`, which
picks the backend by the ``POLLER_ASYNC_DB`` flag:

- **flag on + async client up** — run the write natively on the event loop via
  the pooled HTTP/2 ``AsyncClient`` (#225). Async I/O doesn't occupy an
  executor thread, so the poll's write fan-out stops starving the threads that
  interactive requests need — the #57 regression this targets.
- **otherwise** — the #107 path: the sync client in a thread, under the
  cycle-wide write semaphore. This is also the fail-safe: a missing async
  client silently falls back to sync, so the flag never drops a write.

Both paths retry transient transport blips (idempotent writes only — see
:mod:`app.services.supabase_retry`) and are bounded by ``DB_WRITE_CONCURRENCY``
so the burst can't thundering-herd the Supabase pooler.

The semaphore + thread-runner live here (not in the poller) so every service
module that issues poll writes can share them without importing the poller.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from app.config import settings
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
    """Execute one poll-cycle write, async-on-loop or sync-in-thread by flag.

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
    if settings.poller_async_db:
        async_sb = get_async_supabase()
        if async_sb is not None:
            async with _db_write_semaphore():
                return await execute_with_retry(build(async_sb).execute, label=label)
    return await db_to_thread(lambda: execute_with_retry_sync(build(supabase).execute, label=label))
