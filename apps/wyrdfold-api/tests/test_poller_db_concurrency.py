"""The poll's DB-write fan-out must stay under a global concurrency cap.

Prod bug: the Stage-1/Stage-2 scoring loops ``asyncio.gather`` one
``to_thread`` per (row × target) with no bound, across POLL_CONCURRENCY
sources — a burst of hundreds of simultaneous writes against one shared
supabase client that drops the pooler connection. ``_db_to_thread`` routes
every poll write through a process-wide semaphore so the herd is bounded.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading

import pytest

from app.services import poller


@pytest.mark.asyncio
async def test_db_to_thread_caps_concurrency() -> None:
    """No more than DB_WRITE_CONCURRENCY of these run at once, even when we
    launch far more than that simultaneously.

    We install a thread pool larger than the cap so the *only* thing
    bounding overlap is ``_db_to_thread``'s semaphore — not the executor's
    worker count. Each worker holds a real lock-protected counter so the
    peak measurement is exact, and blocks on an event until the driver
    releases it, so overlap is deterministic rather than timing-dependent.
    """
    cap = poller.DB_WRITE_CONCURRENCY
    lock = threading.Lock()
    in_flight = 0
    peak = 0
    entered = threading.Event()
    release = threading.Event()

    def _blocking_write(i: int) -> int:
        nonlocal in_flight, peak
        with lock:
            in_flight += 1
            peak = max(peak, in_flight)
            if in_flight >= cap:
                # The semaphore has admitted a full batch — let them all go.
                entered.set()
        # Block until the driver confirms the batch overlapped, so we
        # genuinely hold `cap` workers concurrently.
        release.wait(timeout=5)
        with lock:
            in_flight -= 1
        return i

    # Executor with comfortably more workers than the cap, so the executor
    # is never the bottleneck.
    loop = asyncio.get_running_loop()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=cap * 3)
    loop.set_default_executor(executor)
    try:

        async def _one(i: int) -> int:
            return await poller._db_to_thread(_blocking_write, i)

        n = cap * 3
        task = asyncio.gather(*(_one(i) for i in range(n)))

        # Wait until a full cap-sized batch is concurrently in flight.
        await asyncio.to_thread(entered.wait, 5)
        release.set()
        results = await task
    finally:
        release.set()
        executor.shutdown(wait=True)

    assert sorted(results) == list(range(n))  # all completed, none lost
    assert peak == cap, f"expected peak == cap ({cap}), got {peak}"


@pytest.mark.asyncio
async def test_db_write_semaphore_is_per_loop() -> None:
    """A fresh event loop gets its own semaphore, not one bound to a dead
    loop (which would raise / mis-bind under pytest's per-test loops)."""
    sem1 = poller._db_write_semaphore()
    sem2 = poller._db_write_semaphore()
    # Same loop → same semaphore instance (so the cap is shared, not reset
    # per call site).
    assert sem1 is sem2

    loop = asyncio.get_running_loop()
    assert loop in poller._db_write_sems


def test_poll_concurrency_is_bounded_below_legacy() -> None:
    """Source fan-out was lowered from 10 to reduce the herd; guard the
    intent so a future bump is a deliberate edit, not an accident."""
    assert poller.POLL_CONCURRENCY <= 8
    assert poller.DB_WRITE_CONCURRENCY >= 1
