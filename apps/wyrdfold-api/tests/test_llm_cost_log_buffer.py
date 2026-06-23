"""CostLogBuffer: enqueue / flush / start / stop semantics."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.services.llm.cost_log_buffer import CostLogBuffer


def _row(i: int) -> dict[str, Any]:
    return {"i": i, "purpose": "test", "cost_usd": 0.01 * i}


def _supabase_mock() -> MagicMock:
    sb = MagicMock()
    sb.table.return_value.insert.return_value.execute.return_value = MagicMock(data=[])
    return sb


def test_enqueue_appends_to_pending() -> None:
    buf = CostLogBuffer(max_size=10, flush_interval_s=60.0)
    buf.enqueue(_row(1))
    buf.enqueue(_row(2))
    assert buf.pending == 2


async def test_flush_writes_pending_rows_in_one_bulk_insert() -> None:
    buf = CostLogBuffer(max_size=100, flush_interval_s=60.0)
    sb = _supabase_mock()

    for i in range(5):
        buf.enqueue(_row(i))

    written = await buf.flush(sb)

    assert written == 5
    assert buf.pending == 0
    sb.table.assert_called_once_with("llm_costs")
    insert_arg = sb.table.return_value.insert.call_args.args[0]
    assert len(insert_arg) == 5
    assert insert_arg[0]["i"] == 0
    assert insert_arg[-1]["i"] == 4


async def test_flush_empty_buffer_is_noop() -> None:
    buf = CostLogBuffer()
    sb = _supabase_mock()
    written = await buf.flush(sb)
    assert written == 0
    sb.table.assert_not_called()


async def test_flush_failure_re_queues_rows_and_raises() -> None:
    buf = CostLogBuffer()
    sb = MagicMock()
    sb.table.return_value.insert.return_value.execute.side_effect = Exception("boom")

    buf.enqueue(_row(1))
    buf.enqueue(_row(2))

    with pytest.raises(Exception, match="boom"):
        await buf.flush(sb)

    # Rows must survive the failure so the next tick retries.
    assert buf.pending == 2


async def test_flush_failure_preserves_row_order_on_requeue() -> None:
    buf = CostLogBuffer()
    sb = MagicMock()
    sb.table.return_value.insert.return_value.execute.side_effect = Exception("boom")

    for i in range(3):
        buf.enqueue(_row(i))

    with pytest.raises(Exception):
        await buf.flush(sb)

    drained = buf._drain()
    assert [r["i"] for r in drained] == [0, 1, 2]


async def test_periodic_task_drains_buffer_on_interval() -> None:
    buf = CostLogBuffer(max_size=100, flush_interval_s=0.05)
    sb = _supabase_mock()

    buf.start(sb)
    try:
        buf.enqueue(_row(1))
        # Wait for at least one tick.
        for _ in range(20):
            await asyncio.sleep(0.05)
            if buf.pending == 0:
                break
        assert buf.pending == 0
        sb.table.return_value.insert.assert_called()
    finally:
        await buf.stop(sb)


async def test_max_size_triggers_early_flush() -> None:
    buf = CostLogBuffer(max_size=3, flush_interval_s=60.0)
    sb = _supabase_mock()

    buf.start(sb)
    try:
        for i in range(3):
            buf.enqueue(_row(i))
        # Hitting max_size sets the wakeup event; the flusher should
        # process well before the 60s interval.
        for _ in range(40):
            await asyncio.sleep(0.025)
            if buf.pending == 0:
                break
        assert buf.pending == 0
    finally:
        await buf.stop(sb)


async def test_stop_drains_remaining_rows() -> None:
    buf = CostLogBuffer(max_size=100, flush_interval_s=60.0)
    sb = _supabase_mock()

    buf.start(sb)
    buf.enqueue(_row(1))
    buf.enqueue(_row(2))

    await buf.stop(sb)
    assert buf.pending == 0
    insert_arg = sb.table.return_value.insert.call_args.args[0]
    assert len(insert_arg) == 2


def test_start_is_idempotent() -> None:
    buf = CostLogBuffer()
    sb = _supabase_mock()

    async def _go() -> None:
        buf.start(sb)
        first = buf._task
        buf.start(sb)
        # Second start is a noop while the task is alive.
        assert buf._task is first
        await buf.stop(sb)

    asyncio.run(_go())


async def test_enqueue_from_worker_thread_wakes_flusher() -> None:
    """Regression: ``enqueue`` is routinely called inside
    ``asyncio.to_thread`` workers. The early-flush wakeup must cross
    the thread boundary safely — ``asyncio.Event.set()`` is not
    thread-safe, so we use ``loop.call_soon_threadsafe``."""
    buf = CostLogBuffer(max_size=3, flush_interval_s=60.0)
    sb = _supabase_mock()

    buf.start(sb)
    try:
        # Fill exactly to max_size from a worker thread — this triggers
        # the cross-thread wakeup path. Without ``call_soon_threadsafe``
        # the flusher would either crash or only fire on the 60s tick.
        def _producer() -> None:
            for i in range(3):
                buf.enqueue(_row(i))

        await asyncio.to_thread(_producer)

        # Periodic task should drain well before the 60s interval.
        for _ in range(40):
            await asyncio.sleep(0.025)
            if buf.pending == 0:
                break
        assert buf.pending == 0
        sb.table.return_value.insert.assert_called()
    finally:
        await buf.stop(sb)


async def test_stop_does_not_lose_rows_to_cancelled_error() -> None:
    """Regression: an earlier version called ``task.cancel()`` to wind
    down the periodic task, but ``CancelledError`` inherits from
    ``BaseException`` and bypasses ``flush``'s ``except Exception``
    re-queue branch. Any rows drained mid-flush would be dropped.

    Cooperative shutdown lets the in-flight flush complete naturally,
    so when ``stop`` returns every enqueued row has either landed in
    the DB or been re-queued for the next process boot.
    """
    buf = CostLogBuffer(max_size=100, flush_interval_s=0.05)

    write_count = 0

    def _slow_execute() -> Any:
        nonlocal write_count
        # Simulate Supabase taking a beat to ack. Real ``execute`` is
        # sync, so we mirror that — ``flush`` already wraps the call in
        # ``to_thread`` so the loop stays responsive.
        import time

        time.sleep(0.05)
        write_count += 1
        return MagicMock(data=[])

    sb = MagicMock()
    sb.table.return_value.insert.return_value.execute.side_effect = _slow_execute

    buf.start(sb)
    for i in range(5):
        buf.enqueue(_row(i))
    # Give the periodic task a tick to start the first flush.
    await asyncio.sleep(0.06)

    await buf.stop(sb)

    # All five rows must be accounted for — either the in-flight flush
    # finished cleanly or stop's final flush picked them up. Either way
    # nothing should be sitting in ``_rows`` and ``execute`` must have
    # been called at least once.
    assert buf.pending == 0
    assert write_count >= 1


# ---------------------------------------------------------------------------
# Bounded memory: hard ``max_rows`` ceiling + chunked INSERT (audit #29 B)
# ---------------------------------------------------------------------------


def _failing_supabase() -> MagicMock:
    """A supabase mock whose INSERT always raises — simulates a sustained
    write outage so the buffer must re-queue every flush."""
    sb = MagicMock()
    sb.table.return_value.insert.return_value.execute.side_effect = Exception("db down")
    return sb


def test_enqueue_enforces_hard_ceiling_drop_oldest() -> None:
    """Past ``max_rows``, ``enqueue`` evicts the OLDEST row so memory is
    bounded. Without this guard ``_rows`` grows unbounded → OOM."""
    buf = CostLogBuffer(max_size=2, flush_interval_s=60.0, max_rows=5)
    for i in range(8):  # 3 more than the ceiling
        buf.enqueue(_row(i))

    assert buf.pending == 5
    assert buf.dropped == 3
    drained = buf._drain()
    # Oldest three (0,1,2) dropped; newest five retained, in order.
    assert [r["i"] for r in drained] == [3, 4, 5, 6, 7]


def test_enqueue_overflow_is_logged(caplog) -> None:
    """The drop must never be silent — operators need the signal."""
    import logging

    buf = CostLogBuffer(max_size=2, flush_interval_s=60.0, max_rows=3)
    with caplog.at_level(logging.ERROR):
        for i in range(5):
            buf.enqueue(_row(i))

    assert buf.dropped == 2
    assert any("at capacity" in r.message for r in caplog.records)


async def test_rows_never_exceed_ceiling_under_sustained_flush_failure() -> None:
    """THE regression for the OOM finding: a sustained Supabase outage
    makes every flush fail and re-queue, while new rows keep arriving.
    ``_rows`` must stay capped at ``max_rows`` no matter how long it lasts.
    """
    buf = CostLogBuffer(max_size=10, flush_interval_s=60.0, max_rows=50)
    sb = _failing_supabase()

    for cycle in range(20):  # 20 outage cycles
        for i in range(25):  # 25 new rows per cycle = 500 total enqueued
            buf.enqueue(_row(cycle * 100 + i))
        # Flush fails and re-queues; must not push us past the ceiling.
        with pytest.raises(Exception, match="db down"):
            await buf.flush(sb)
        assert buf.pending <= 50, f"ceiling breached at cycle {cycle}: {buf.pending}"

    assert buf.pending == 50
    # 500 enqueued, only 50 can be held → 450 dropped.
    assert buf.dropped == 450


def test_requeue_honors_ceiling() -> None:
    """A failed flush of a large drained batch combined with rows added
    during the flush must not push ``_rows`` past the ceiling."""
    buf = CostLogBuffer(max_size=100, flush_interval_s=60.0, max_rows=4)
    # Simulate: 4 rows already buffered (added "during" the flush) ...
    for i in range(100, 104):
        buf.enqueue(_row(i))
    assert buf.pending == 4
    # ... and a flush of 3 rows fails and tries to push them to the front.
    buf._requeue([_row(0), _row(1), _row(2)])
    assert buf.pending == 4  # still capped


async def test_flush_chunks_bulk_insert() -> None:
    """A large drained list is written in bounded chunks, not one giant
    INSERT — bounds statement / request-body size."""
    buf = CostLogBuffer(max_size=10_000, flush_interval_s=60.0, insert_batch_size=100)
    sb = _supabase_mock()

    for i in range(250):
        buf.enqueue(_row(i))

    written = await buf.flush(sb)

    assert written == 250
    assert buf.pending == 0
    insert = sb.table.return_value.insert
    # 250 rows / 100 per chunk = 3 inserts (100, 100, 50).
    assert insert.call_count == 3
    chunk_sizes = [len(call.args[0]) for call in insert.call_args_list]
    assert chunk_sizes == [100, 100, 50]


async def test_chunked_flush_partial_failure_requeues_only_uncommitted() -> None:
    """If chunk 3 fails, chunks 1-2 are already committed and must NOT be
    re-queued (no duplicate writes); only the failing + remaining rows
    come back, preserving order."""
    buf = CostLogBuffer(max_size=10_000, flush_interval_s=60.0, insert_batch_size=2)
    sb = MagicMock()

    calls: list[list[dict[str, Any]]] = []

    def _insert(rows: list[dict[str, Any]]) -> MagicMock:
        calls.append(rows)
        m = MagicMock()
        # Fail on the 3rd chunk (rows 4,5).
        if len(calls) == 3:
            m.execute.side_effect = Exception("chunk boom")
        else:
            m.execute.return_value = MagicMock(data=[])
        return m

    sb.table.return_value.insert.side_effect = _insert

    for i in range(6):  # 3 chunks of 2
        buf.enqueue(_row(i))

    with pytest.raises(Exception, match="chunk boom"):
        await buf.flush(sb)

    # First two chunks (rows 0-3) committed; only 4,5 re-queued in order.
    remaining = buf._drain()
    assert [r["i"] for r in remaining] == [4, 5]
