"""Detached fire-and-forget tasks for request-triggered long-running work.

Starlette ``BackgroundTasks`` runs its callables inside the request's ASGI
cycle after the response goes out. Under uvloop, a background task that fans
out concurrent work on the pooled async Supabase client (#57
``POLLER_ASYNC_DB``) deadlocks: the httpx pool collapses to a single
connection and every seam call queues behind it indefinitely — the event
loop itself stays perfectly responsive, only the pool starves. Reproduced
deterministically (2026-07-15) with the poll's write herd at 10 sources x
1250 rows: the identical cycle completes in ~90s when awaited inline in the
request handler, when driven by APScheduler, or when spawned with
``asyncio.create_task`` — and wedges forever only as a starlette
``BackgroundTask`` on uvloop (uvicorn's default loop). Plain-asyncio runs
are unaffected, so this only bites the deployed shape.

So: request-triggered work that reaches the poll seam is spawned as a real
top-level task on the running loop instead. That is also the semantics these
endpoints actually want — the 202 responses exist so the caller does NOT
wait; the work should outlive the request machinery entirely (see
``run_force_poll_locked``'s "self-contained on purpose" contract).

``_DETACHED`` holds strong references: asyncio keeps only weak refs to
tasks, so a fire-and-forget task with no reference can be garbage-collected
mid-flight. The done callback drops the ref and logs any exception the body
let escape — each current body already catches its own errors, so this is a
safety net, not the primary handler.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)

_DETACHED: set[asyncio.Task[Any]] = set()


def spawn_detached(coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task[Any]:
    """Run ``coro`` as a named top-level task on the running event loop.

    Call from async request handlers only (requires a running loop). Returns
    the task, mainly so tests can await it deterministically.
    """
    task = asyncio.create_task(coro, name=name)
    _DETACHED.add(task)

    def _done(t: asyncio.Task[Any]) -> None:
        _DETACHED.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.error("detached task %r raised", name, exc_info=exc)

    task.add_done_callback(_done)
    return task
