"""Async cost-log buffer for cron paths.

Background
----------
`cost_log.record(...)` writes one `llm_costs` row per LLM completion via
a synchronous Supabase INSERT. Each call is its own DB round-trip.

For interactive paths (analysis, tailor, conversation) that's fine — the
user is already waiting on the LLM, and the spend record needs to be
queryable immediately for the budget guard.

For cron paths (poller stage-3 LLM scoring, batch endpoints) the per-row
INSERT doubles the wall-clock cost of each completion. The poller fans
out N concurrent LLM calls under `LLM_CONCURRENCY = 3`, so each tick
turns into 2N Supabase round-trips on top of N Anthropic calls.

This buffer batches those writes:
  - `enqueue(row)` is O(1), appends to an in-memory list under a lock
  - `flush(supabase)` drains the buffer into a single bulk INSERT
  - `start(supabase)` spawns a background task that flushes every
    `flush_interval_s` seconds (or whenever the buffer reaches
    `max_size`, whichever comes first)
  - `stop(supabase)` drains and shuts down the background task

On flush failure the rows are re-queued so a transient Supabase outage
doesn't drop spend data — the next tick will retry. Pair with
provider-side spend alerts for a hard ceiling.

Bounded memory
--------------
Re-queueing on every failed flush means a *sustained* Supabase write
outage would otherwise grow ``_rows`` without bound until the process
OOMs — and the rows are lost on restart anyway. To stay alive (and keep
serving traffic) under that failure mode the buffer enforces a hard
``max_rows`` ceiling, independent of ``max_size``: once full, the OLDEST
buffered rows are evicted to make room (the budget guard cares most about
recent spend) and the drop is logged. The bulk INSERT is also chunked
into ``insert_batch_size`` slices so a single drained list of tens of
thousands of rows can't blow out the statement size / request body —
rows already committed in earlier chunks are never re-sent on a later
chunk's failure, so chunking introduces no duplicate-write risk.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from typing import Any

from supabase import Client

TABLE = "llm_costs"

_log = logging.getLogger(__name__)


class CostLogBuffer:
    def __init__(
        self,
        *,
        max_size: int = 100,
        flush_interval_s: float = 5.0,
        max_rows: int = 10_000,
        insert_batch_size: int = 500,
    ) -> None:
        self._max_size = max_size
        self._flush_interval_s = flush_interval_s
        # Hard capacity ceiling, independent of ``max_size`` (which only
        # gates the early-flush wakeup). Default 10k ≫ the 100-row
        # early-flush threshold: ~hundreds of bytes/row → low single-digit
        # MB worst case, so the process survives a sustained Supabase
        # outage instead of OOMing. Past this, the oldest rows are dropped.
        self._max_rows = max_rows
        # Chunk size for the bulk INSERT so one drained list can't produce
        # a single oversized statement / request body.
        self._insert_batch_size = insert_batch_size
        # Running count of rows dropped on overflow, for observability.
        self._dropped = 0
        self._rows: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        # Set when a row is enqueued; the background task waits on this
        # to flush early when the buffer is full.
        self._wakeup: asyncio.Event | None = None
        # Loop the periodic task runs on. ``enqueue`` is called from sync
        # code (often inside ``asyncio.to_thread`` workers), so it can't
        # touch ``_wakeup`` directly — ``asyncio.Event`` is not
        # thread-safe. We stash the loop in ``start`` and use
        # ``call_soon_threadsafe`` to signal across the thread boundary.
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._rows)

    @property
    def dropped(self) -> int:
        """Total rows evicted on overflow since process start."""
        with self._lock:
            return self._dropped

    def enqueue(self, row: dict[str, Any]) -> None:
        """Add one row to the buffer. Thread-safe; safe to call from
        sync code (e.g. inside `asyncio.to_thread`).

        If the buffer is already at its hard ``max_rows`` ceiling (a
        sustained flush outage), the oldest row is evicted to bound
        memory. The eviction is logged so the drop is never silent.
        """
        with self._lock:
            self._rows.append(row)
            dropped_now = 0
            while len(self._rows) > self._max_rows:
                # Drop oldest: the budget guard values recent spend most,
                # and these rows are lost on restart regardless.
                del self._rows[0]
                self._dropped += 1
                dropped_now += 1
            total_dropped = self._dropped
            full = len(self._rows) >= self._max_size
        if dropped_now:
            _log.error(
                "cost-log buffer at capacity (max_rows=%d); dropped %d oldest "
                "row(s), %d total dropped this process. Supabase writes are "
                "failing — check the DB and provider-side spend alerts.",
                self._max_rows,
                dropped_now,
                total_dropped,
            )
        if not full:
            return
        # The wakeup must be set from the loop's own thread —
        # ``asyncio.Event.set()`` is not thread-safe. Route through the
        # captured loop so the call lands on the right thread even when
        # ``enqueue`` runs under ``asyncio.to_thread``.
        loop = self._loop
        wakeup = self._wakeup
        if loop is None or wakeup is None:
            return
        # Loop may be closed (e.g. interpreter shutdown after the FastAPI
        # lifespan exited); in that case there's nothing to wake and the
        # row stays buffered for the next process boot.
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(wakeup.set)

    def _drain(self) -> list[dict[str, Any]]:
        with self._lock:
            rows, self._rows = self._rows, []
        return rows

    def _requeue(self, rows: list[dict[str, Any]]) -> None:
        """Push failed rows back to the front so retries preserve order.

        Honors the hard ``max_rows`` ceiling: if the re-queued rows plus
        whatever was enqueued during the flush exceed capacity, the
        oldest rows are dropped (logged) so a flush failure can never
        push the buffer past its bound.
        """
        dropped_now = 0
        with self._lock:
            self._rows = rows + self._rows
            while len(self._rows) > self._max_rows:
                del self._rows[0]
                self._dropped += 1
                dropped_now += 1
            total_dropped = self._dropped
        if dropped_now:
            _log.error(
                "cost-log buffer at capacity (max_rows=%d) on re-queue; "
                "dropped %d oldest row(s), %d total dropped this process.",
                self._max_rows,
                dropped_now,
                total_dropped,
            )

    async def flush(self, supabase: Client) -> int:
        """Drain the buffer and write the rows in chunked bulk INSERTs.

        The drained list is sliced into ``insert_batch_size`` chunks so a
        large backlog can't produce one oversized statement / request
        body. Returns the number of rows written. On the first failing
        chunk, the rows already committed in earlier chunks are dropped
        (they're durably written) and only the failing chunk + the
        remaining un-sent chunks are re-queued, then the error re-raises
        so the caller can log/retry. This makes chunking duplicate-safe.
        """
        rows = self._drain()
        if not rows:
            return 0

        def _insert(batch_rows: list[dict[str, Any]]) -> None:
            supabase.table(TABLE).insert(batch_rows).execute()

        written = 0
        batch = self._insert_batch_size
        for start in range(0, len(rows), batch):
            chunk = rows[start : start + batch]
            try:
                # Pass ``chunk`` as an argument (not a closure) so each
                # iteration's slice is bound explicitly — no loop-variable
                # capture, and mypy can infer the call's types.
                await asyncio.to_thread(_insert, chunk)
            except Exception:
                # Only re-queue what hasn't been committed: this failing
                # chunk plus everything after it. Earlier chunks are
                # already in Postgres, so dropping them avoids duplicates.
                remaining = rows[start:]
                self._requeue(remaining)
                _log.exception(
                    "cost-log buffer flush failed after %d row(s); "
                    "re-queued %d row(s)",
                    written,
                    len(remaining),
                )
                raise
            written += len(chunk)
        return written

    async def _run(self, supabase: Client) -> None:
        # `_wakeup` is created synchronously in `start` before this task
        # is scheduled, so it's never None here.
        wakeup = self._wakeup
        if wakeup is None:
            return
        try:
            while not self._stopping:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        wakeup.wait(), timeout=self._flush_interval_s
                    )
                wakeup.clear()
                with contextlib.suppress(Exception):
                    # `flush` already logs the exception; the loop just
                    # needs to keep going so the next tick retries.
                    await self.flush(supabase)
        finally:
            self._wakeup = None

    def start(self, supabase: Client) -> None:
        """Spawn the periodic flush task. Idempotent.

        Must be called from within a running asyncio loop (e.g. the
        FastAPI lifespan body). Creates the wakeup Event up-front so
        `enqueue(...)` calls that happen between `start()` and the
        task's first `await` still trigger an early flush at max-size.
        """
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        self._wakeup = asyncio.Event()
        # Capture the running loop so cross-thread ``enqueue`` calls can
        # schedule the wakeup via ``call_soon_threadsafe``.
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._run(supabase))

    async def stop(self, supabase: Client) -> None:
        """Drain the buffer and shut the periodic task down cooperatively.

        Avoids ``task.cancel()`` because ``CancelledError`` derives from
        ``BaseException``, so cancelling mid-``flush`` would bypass the
        ``except Exception`` re-queue branch and lose any rows that had
        been drained but not yet committed. Instead we set the stop
        flag, nudge the wakeup so the task notices immediately instead
        of waiting out the flush interval, and let the current
        iteration finish on its own.
        """
        self._stopping = True
        wakeup = self._wakeup
        loop = self._loop
        if wakeup is not None and loop is not None:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(wakeup.set)
        if self._task is not None:
            with contextlib.suppress(Exception):
                await self._task
            self._task = None
        with contextlib.suppress(Exception):
            await self.flush(supabase)
        self._loop = None


# Module-level singleton — every cron caller writes through the same
# buffer so a single bulk INSERT can collect rows from multiple
# concurrent LLM calls.
buffer = CostLogBuffer()
