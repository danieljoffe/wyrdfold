"""Poll-write seam + write-herd cap (``app.services.db_write``).

Covers the two things #57 leans on:

- ``db_to_thread`` bounds the poll's write fan-out at ``DB_WRITE_CONCURRENCY``
  no matter how wide the ``asyncio.gather`` is (the prod pooler-drop bug), and
  the semaphore is per-event-loop (safe under pytest's per-test loops).
- ``poll_db_write`` picks the backend by ``POLLER_ASYNC_DB``: async client on
  the loop when the flag is on and the client is up, else the sync client in a
  thread — the latter also being the fail-safe when the async client is absent.
  Both paths retry transient transport blips.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from app.services import db_write


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip retry backoff sleeps (sync + async) so retry tests don't idle."""
    import app.services.supabase_retry as retry

    monkeypatch.setattr(retry.time, "sleep", lambda _s: None)

    async def _instant(_s: float) -> None:
        return None

    monkeypatch.setattr(retry.asyncio, "sleep", _instant)


# ---- write-herd cap --------------------------------------------------------


@pytest.mark.asyncio
async def test_db_to_thread_caps_concurrency() -> None:
    """No more than DB_WRITE_CONCURRENCY run at once, even when we launch far
    more than that simultaneously.

    Install a thread pool larger than the cap so the *only* thing bounding
    overlap is the semaphore — not the executor's worker count. Each worker
    holds a lock-protected counter (exact peak) and blocks on an event until
    the driver releases it, so overlap is deterministic, not timing-dependent.
    """
    cap = db_write.DB_WRITE_CONCURRENCY
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
                entered.set()
        release.wait(timeout=5)
        with lock:
            in_flight -= 1
        return i

    loop = asyncio.get_running_loop()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=cap * 3)
    loop.set_default_executor(executor)
    try:

        async def _one(i: int) -> int:
            return await db_write.db_to_thread(_blocking_write, i)

        n = cap * 3
        task = asyncio.gather(*(_one(i) for i in range(n)))
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
    """A fresh event loop gets its own semaphore, not one bound to a dead loop
    (which would mis-bind under pytest's per-test loops)."""
    sem1 = db_write._db_write_semaphore()
    sem2 = db_write._db_write_semaphore()
    assert sem1 is sem2  # same loop → shared cap, not reset per call site

    loop = asyncio.get_running_loop()
    assert loop in db_write._db_write_sems


# ---- backend selection (POLLER_ASYNC_DB) -----------------------------------


class _Recorder:
    """A supabase query builder whose table/update/eq/lt chain records the
    query shape and returns self; ``execute`` fails ``fail_times`` then
    returns a response. Subclasses supply sync vs. async ``execute``."""

    def __init__(self, fail_times: int = 0) -> None:
        self.ops: list[tuple[Any, ...]] = []
        self.executed = 0
        self.fail_times = fail_times

    def table(self, name: str) -> _Recorder:
        self.ops.append(("table", name))
        return self

    def update(self, payload: dict[str, Any]) -> _Recorder:
        self.ops.append(("update", payload))
        return self

    def eq(self, col: str, val: Any) -> _Recorder:
        self.ops.append(("eq", col, val))
        return self

    def lt(self, col: str, val: Any) -> _Recorder:
        self.ops.append(("lt", col, val))
        return self

    def _result(self) -> Any:
        self.executed += 1
        if self.executed <= self.fail_times:
            raise httpx.RemoteProtocolError("transient blip")
        return MagicMock(data=[{"ok": True}])


class _SyncClient(_Recorder):
    def execute(self) -> Any:
        return self._result()


class _AsyncClient(_Recorder):
    async def execute(self) -> Any:
        return self._result()


def _scores_update(c: Any) -> Any:
    return c.table("scores").update({"score": 1}).eq("job_posting_id", "j").eq("target_id", "t")


@pytest.mark.asyncio
async def test_flag_on_with_async_client_writes_on_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async_client = _AsyncClient()
    monkeypatch.setattr(db_write, "get_async_supabase", lambda: async_client)
    sync_client = _SyncClient()

    resp = await db_write.poll_db_write(sync_client, _scores_update, label="t")

    assert resp.data == [{"ok": True}]
    assert async_client.executed == 1  # async client took the write
    assert sync_client.executed == 0  # ...and the sync client was untouched
    assert ("table", "scores") in async_client.ops
    assert ("eq", "job_posting_id", "j") in async_client.ops


@pytest.mark.asyncio
async def test_flag_on_but_no_async_client_falls_back_to_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(db_write, "get_async_supabase", lambda: None)  # not up
    sync_client = _SyncClient()

    resp = await db_write.poll_db_write(sync_client, _scores_update, label="t")

    assert resp.data == [{"ok": True}]
    assert sync_client.executed == 1  # fail-safe: sync took the write


@pytest.mark.asyncio
async def test_no_async_client_falls_back_to_sync_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    looked_up: list[int] = []
    monkeypatch.setattr(db_write, "get_async_supabase", lambda: looked_up.append(1))
    sync_client = _SyncClient()

    await db_write.poll_db_write(sync_client, _scores_update, label="t")

    assert sync_client.executed == 1
    assert looked_up == [1]  # async consulted, returned None → sync fallback took the write


@pytest.mark.asyncio
async def test_sync_path_retries_transient_blip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(db_write, "get_async_supabase", lambda: None)
    sync_client = _SyncClient(fail_times=1)

    resp = await db_write.poll_db_write(sync_client, _scores_update, label="t")

    assert resp.data == [{"ok": True}]
    assert sync_client.executed == 2  # one blip, one retry


@pytest.mark.asyncio
async def test_async_path_retries_transient_blip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async_client = _AsyncClient(fail_times=1)
    monkeypatch.setattr(db_write, "get_async_supabase", lambda: async_client)

    resp = await db_write.poll_db_write(_SyncClient(), _scores_update, label="t")

    assert resp.data == [{"ok": True}]
    assert async_client.executed == 2  # retried on the loop


# ---- poll_db_read -----------------------------------------------------------


def _sources_select(c: Any) -> Any:
    return c.table("sources").update({"x": 1}).eq("enabled", True)


@pytest.mark.asyncio
async def test_read_flag_on_uses_async_client(monkeypatch: pytest.MonkeyPatch) -> None:
    async_client = _AsyncClient()
    monkeypatch.setattr(db_write, "get_async_supabase", lambda: async_client)
    sync_client = _SyncClient()

    resp = await db_write.poll_db_read(sync_client, _sources_select, label="r")

    assert resp.data == [{"ok": True}]
    assert async_client.executed == 1
    assert sync_client.executed == 0


@pytest.mark.asyncio
async def test_read_flag_on_no_async_client_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(db_write, "get_async_supabase", lambda: None)
    sync_client = _SyncClient()

    resp = await db_write.poll_db_read(sync_client, _sources_select, label="r")

    assert resp.data == [{"ok": True}]
    assert sync_client.executed == 1


@pytest.mark.asyncio
async def test_read_flag_off_sync_no_retry_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag-off reads must be byte-for-byte today's behavior: bare execute in
    a thread, NO retry — a transient blip propagates to the caller exactly as
    the pre-seam ``asyncio.to_thread(query.execute)`` did."""
    monkeypatch.setattr(db_write, "get_async_supabase", lambda: None)
    sync_client = _SyncClient(fail_times=1)

    with pytest.raises(httpx.RemoteProtocolError):
        await db_write.poll_db_read(sync_client, _sources_select, label="r")
    assert sync_client.executed == 1  # one attempt, no retry


@pytest.mark.asyncio
async def test_read_flag_off_retry_sync_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Call sites that already wrapped their read in the sync retry helper
    keep that behavior via ``retry_sync=True``."""
    monkeypatch.setattr(db_write, "get_async_supabase", lambda: None)
    sync_client = _SyncClient(fail_times=1)

    resp = await db_write.poll_db_read(sync_client, _sources_select, label="r", retry_sync=True)

    assert resp.data == [{"ok": True}]
    assert sync_client.executed == 2


@pytest.mark.asyncio
async def test_read_flag_on_always_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    async_client = _AsyncClient(fail_times=1)
    monkeypatch.setattr(db_write, "get_async_supabase", lambda: async_client)

    resp = await db_write.poll_db_read(_SyncClient(), _sources_select, label="r")

    assert resp.data == [{"ok": True}]
    assert async_client.executed == 2


@pytest.mark.asyncio
async def test_read_does_not_hold_the_write_semaphore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read must proceed even when the write herd has the semaphore fully
    saturated — reads never queued behind writes pre-seam, and coupling them
    would stretch the cycle."""
    monkeypatch.setattr(db_write, "get_async_supabase", lambda: None)
    sem = db_write._db_write_semaphore()
    holds = [await sem.acquire() for _ in range(db_write.DB_WRITE_CONCURRENCY)]
    try:
        sync_client = _SyncClient()
        resp = await asyncio.wait_for(
            db_write.poll_db_read(sync_client, _sources_select, label="r"),
            timeout=2.0,
        )
        assert resp.data == [{"ok": True}]
        assert sync_client.executed == 1
    finally:
        for _ in holds:
            sem.release()


# ---- key-set-grouped bulk upsert (#928) ------------------------------------


class _RecordingUpsertClient:
    """Records each ``.upsert(...).execute()`` and echoes the payload back the
    way PostgREST's ``RETURNING`` does, so the aggregation can be asserted."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict[str, Any]], str]] = []
        self._table = ""
        self._pending: list[dict[str, Any]] = []

    def table(self, name: str) -> _RecordingUpsertClient:
        self._table = name
        return self

    def upsert(self, rows: list[dict[str, Any]], on_conflict: str) -> _RecordingUpsertClient:
        self.calls.append((self._table, [dict(r) for r in rows], on_conflict))
        self._pending = [dict(r) for r in rows]
        return self

    def execute(self) -> Any:
        return MagicMock(data=list(self._pending))


@pytest.fixture
def _sync_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the seam to the sync path so the recording client is the one used."""
    monkeypatch.setattr(db_write, "get_async_supabase", lambda: None)


@pytest.mark.asyncio
async def test_upsert_splits_a_heterogeneous_batch_by_key_set(_sync_only: None) -> None:
    """#928: PostgREST builds ONE statement from the union of a bulk payload's
    keys, so a key one row supplies is written (as NULL) to the rows that
    omitted it. Partitioning by key-set is what makes the omission real."""
    client = _RecordingUpsertClient()
    rows = [
        {"external_id": "a", "title": "A"},  # board silent
        {"external_id": "b", "title": "B", "is_remote": True},  # board speaks
        {"external_id": "c", "title": "C"},  # board silent
    ]
    # PRECONDITION: the batch really is heterogeneous — otherwise this would
    # pass against the broken code for the wrong reason.
    assert len({frozenset(r) for r in rows}) == 2

    returned = await db_write.poll_db_upsert(
        client, table="jobs", rows=rows, on_conflict="source_id,external_id", label="t"
    )

    assert len(client.calls) == 2
    for _table, payload, _oc in client.calls:
        assert len({frozenset(r) for r in payload}) == 1, "a statement mixed key-sets"
    # Grouping preserves first-appearance order and every row lands exactly once.
    assert [[r["external_id"] for r in p] for _t, p, _o in client.calls] == [["a", "c"], ["b"]]
    assert all(t == "jobs" and oc == "source_id,external_id" for t, _p, oc in client.calls)
    # Splitting the batch must not reshuffle the RESULT: Phase 2's daily-cap
    # trim resolves residual ties by position, so a reordered result would
    # quietly change which equally-ranked postings get graded.
    assert [r["external_id"] for r in returned] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_upsert_returns_rows_in_the_callers_order(_sync_only: None) -> None:
    """Explicitly: the result order is the INPUT order, not the group order —
    including when the groups interleave and the last input row is in the
    first group."""
    client = _RecordingUpsertClient()
    rows = [
        {"source_id": "s", "external_id": "1", "is_remote": True},
        {"source_id": "s", "external_id": "2"},
        {"source_id": "s", "external_id": "3", "is_remote": False},
        {"source_id": "s", "external_id": "4"},
    ]

    returned = await db_write.poll_db_upsert(
        client, table="jobs", rows=rows, on_conflict="source_id,external_id", label="t"
    )

    # PRECONDITION: the groups really did interleave, so group order != input
    # order and the assertion below has something to catch.
    assert [[r["external_id"] for r in p] for _t, p, _o in client.calls] == [
        ["1", "3"],
        ["2", "4"],
    ]
    assert [r["external_id"] for r in returned] == ["1", "2", "3", "4"]


@pytest.mark.asyncio
async def test_upsert_issues_one_statement_for_a_homogeneous_batch(_sync_only: None) -> None:
    """The common case must not pay extra round trips."""
    client = _RecordingUpsertClient()
    rows = [
        {"external_id": "a", "title": "A", "is_remote": True},
        {"external_id": "b", "title": "B", "is_remote": False},
    ]

    returned = await db_write.poll_db_upsert(
        client, table="jobs", rows=rows, on_conflict="source_id,external_id", label="t"
    )

    assert len(client.calls) == 1
    assert [r["external_id"] for r in returned] == ["a", "b"]


@pytest.mark.asyncio
async def test_upsert_raises_on_a_duplicate_conflict_key_across_key_sets(
    _sync_only: None,
) -> None:
    """The failure grouping would otherwise SWALLOW.

    Two rows sharing a conflict key raise Postgres' "cannot affect row a second
    time" in one statement. With different key-sets they land in different
    statements, both succeed, and the later group silently wins — leaving this
    helper less fail-fast than the plain bulk upsert it replaces, in exactly the
    heterogeneous case it exists for. It must raise instead.
    """
    client = _RecordingUpsertClient()
    rows = [
        {"source_id": "s", "external_id": "dup", "title": "A"},
        {"source_id": "s", "external_id": "dup", "title": "B", "is_remote": True},
    ]
    # PRECONDITIONS: same conflict key, different key-sets — i.e. grouping
    # really would split them and hide the collision.
    assert rows[0]["external_id"] == rows[1]["external_id"]
    assert rows[0]["source_id"] == rows[1]["source_id"]
    assert len({frozenset(r) for r in rows}) == 2

    with pytest.raises(ValueError, match="duplicate"):
        await db_write.poll_db_upsert(
            client, table="jobs", rows=rows, on_conflict="source_id,external_id", label="t"
        )
    # Nothing was written — the guard runs before any statement is issued.
    assert client.calls == []


@pytest.mark.asyncio
async def test_upsert_raises_on_a_duplicate_conflict_key_within_one_key_set(
    _sync_only: None,
) -> None:
    """Same-key-set duplicates would have reached Postgres and errored there.
    The guard reports them earlier and identically, so the helper's contract
    doesn't depend on how the key-sets happen to fall."""
    client = _RecordingUpsertClient()
    rows = [
        {"source_id": "s", "external_id": "dup", "title": "A"},
        {"source_id": "s", "external_id": "dup", "title": "B"},
    ]
    assert len({frozenset(r) for r in rows}) == 1  # PRECONDITION: one group

    with pytest.raises(ValueError, match="duplicate"):
        await db_write.poll_db_upsert(
            client, table="jobs", rows=rows, on_conflict="source_id,external_id", label="t"
        )
    assert client.calls == []


@pytest.mark.asyncio
async def test_upsert_guard_names_columns_but_not_their_values(_sync_only: None) -> None:
    """The message goes to the platform log. This helper is generic — a conflict
    key elsewhere could be an email — so it must not echo the values (#885)."""
    rows = [
        {"user_id": "u", "email": "person@example.com"},
        {"user_id": "u", "email": "person@example.com", "extra": 1},
    ]
    with pytest.raises(ValueError) as exc:
        await db_write.poll_db_upsert(
            _RecordingUpsertClient(), table="t", rows=rows, on_conflict="user_id,email", label="l"
        )
    assert "user_id,email" in str(exc.value)  # the columns ARE named
    assert "person@example.com" not in str(exc.value)


@pytest.mark.asyncio
async def test_upsert_does_not_flag_rows_missing_a_conflict_column(_sync_only: None) -> None:
    """A row that omits a conflict column can't match the conflict target on a
    value we can see — Postgres would INSERT both rather than error — so
    flagging it would be a false positive that fails a healthy write."""
    client = _RecordingUpsertClient()
    rows = [{"title": "A"}, {"title": "B"}]  # neither carries the conflict key

    returned = await db_write.poll_db_upsert(
        client, table="jobs", rows=rows, on_conflict="source_id,external_id", label="t"
    )

    assert len(client.calls) == 1
    assert len(returned) == 2


@pytest.mark.asyncio
async def test_upsert_of_nothing_issues_no_statement(_sync_only: None) -> None:
    """``.upsert([])`` raises PGRST100 and would mark the poll failed (the guard
    the poller carries at its call sites) — the helper must not reintroduce it
    for a batch that grouped down to nothing."""
    client = _RecordingUpsertClient()

    assert (
        await db_write.poll_db_upsert(
            client, table="jobs", rows=[], on_conflict="source_id,external_id", label="t"
        )
        == []
    )
    assert client.calls == []
