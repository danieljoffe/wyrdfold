"""Postgres advisory-lock guard for the scheduled poll.

The scheduled poll is the primary ingestion trigger. To make it safe to
run even with multiple API replicas — or with the legacy Vercel cron
still firing — we wrap each scheduled poll in a Postgres *session-level*
advisory lock (``pg_try_advisory_lock``). Only one holder at a time gets
the lock; everyone else gets ``False`` and skips that tick.

This is non-blocking and best-effort: ``pg_try_advisory_lock`` returns
immediately, so a busy tick never queues a second one. Job upserts are
idempotent regardless, so the lock is defense-in-depth against wasted
work and duplicate outbound fetches, not the sole correctness guarantee.

Lock lifetime caveat: a session-level advisory lock is held by the
Postgres backend connection that ran the RPC. The Supabase/PostgREST
client pools connections, so the lock outlives the single RPC call and
must be released explicitly — hence ``poll_advisory_lock`` is an async
context manager that always releases in its ``finally``. If release
fails (e.g. the connection was recycled), Postgres drops the lock when
that backend session ends anyway, so we never deadlock permanently.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

from supabase import Client

from app.supabase_pool import get_async_supabase

logger = logging.getLogger(__name__)


async def _lock_rpc(supabase: Client, fn: str, key: int) -> Any:
    """Run one advisory-lock RPC on the async client, sync-in-thread fallback (#57).

    Deliberately NOT routed through the ``db_write`` seam: the seam retries
    transient blips, but ``pg_try_advisory_lock`` is re-entrant per backend
    session — a retry after a lost *response* to a successful acquire would
    take the lock a second time, and the single release then leaks one hold
    count until that backend session recycles. Locks get exactly one attempt
    on either path; a lost response degrades to "skip this tick", which the
    callers already treat as harmless.
    """
    async_sb = get_async_supabase()
    if async_sb is not None:
        return await async_sb.rpc(fn, {"p_key": key}).execute()
    return await asyncio.to_thread(lambda: supabase.rpc(fn, {"p_key": key}).execute())


async def try_acquire_poll_lock(supabase: Client, key: int) -> bool:
    """Try to take the poll advisory lock. Returns True iff acquired.

    Best-effort: any error acquiring the lock returns False (skip the
    tick) rather than raising — a lock-RPC outage must not crash the
    scheduler, and skipping one tick is harmless (the next tick retries).
    """
    try:
        resp = await _lock_rpc(supabase, "try_poll_advisory_lock", key)
    except Exception:
        logger.exception("poll advisory lock acquire failed — skipping this tick")
        return False
    return bool(resp.data)


async def release_poll_lock(supabase: Client, key: int) -> None:
    """Release the poll advisory lock. Best-effort, never raises."""
    try:
        await _lock_rpc(supabase, "release_poll_advisory_lock", key)
    except Exception:
        logger.exception(
            "poll advisory lock release failed (key=%s) — Postgres will drop it "
            "when the backend session ends",
            key,
        )


@contextlib.asynccontextmanager
async def poll_advisory_lock(supabase: Client, key: int) -> AsyncIterator[bool]:
    """Async context manager wrapping the poll advisory lock.

    Yields True when the lock was acquired (caller should run the poll)
    and False when another holder has it (caller should skip). Always
    releases on exit when it acquired the lock.
    """
    acquired = await try_acquire_poll_lock(supabase, key)
    try:
        yield acquired
    finally:
        if acquired:
            await release_poll_lock(supabase, key)
