"""``spawn_detached`` — real top-level tasks for request-triggered work.

Why this exists (and why the routes must not use starlette
``BackgroundTasks`` for seam-reaching work): under uvloop, a background
task fanning out concurrent requests on the pooled async Supabase client
deadlocks the httpx pool (#57 load test, 2026-07-15). See
``app/background.py`` for the full account.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from app import background


@pytest.mark.asyncio
async def test_spawned_task_runs_and_ref_is_released() -> None:
    ran = asyncio.Event()

    async def _body() -> None:
        ran.set()

    task = background.spawn_detached(_body(), name="t1")
    assert task in background._DETACHED  # strong ref held while pending
    await asyncio.wait_for(ran.wait(), timeout=2)
    await task
    # Done callbacks run on the loop's next tick.
    await asyncio.sleep(0)
    assert task not in background._DETACHED  # ...and dropped when done


@pytest.mark.asyncio
async def test_escaped_exception_is_logged_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The bodies catch their own errors; this is the safety net for one
    that escapes — it must be logged, never lost, never re-raised into the
    loop's exception handler as an un-retrieved task exception."""

    async def _explodes() -> None:
        raise RuntimeError("escaped!")

    with caplog.at_level(logging.ERROR, logger="app.background"):
        task = background.spawn_detached(_explodes(), name="boom")
        with pytest.raises(RuntimeError):
            await task
        await asyncio.sleep(0)

    assert any("boom" in r.message for r in caplog.records)
    assert task not in background._DETACHED


@pytest.mark.asyncio
async def test_cancelled_task_is_dropped_quietly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def _sleeps() -> None:
        await asyncio.sleep(30)

    with caplog.at_level(logging.ERROR, logger="app.background"):
        task = background.spawn_detached(_sleeps(), name="cancel-me")
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)

    assert task not in background._DETACHED
    assert not caplog.records  # cancellation is not an error
