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
from collections.abc import Callable, Sequence
from typing import Any, cast

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


async def poll_db_upsert(
    supabase: Any,
    *,
    table: str,
    rows: Sequence[dict[str, Any]],
    on_conflict: str,
    label: str,
) -> list[dict[str, Any]]:
    """Bulk-upsert ``rows`` so that a key a row OMITS is never written (#928).

    A PostgREST bulk upsert is ONE ``INSERT … ON CONFLICT DO UPDATE`` built from
    the UNION of the keys across the whole payload: a key present on *any* row
    lands in the column list, and the rows that omitted it are sent ``NULL``.
    So "omit the key and the stored column is untouched" — the contract
    :func:`app.services.board_metadata.board_columns` is built on, and the way
    every optional-key spread reads — only holds while NO row in the batch
    supplies the key. Real poll batches are heterogeneous by construction (one
    posting states ``isRemote`` / ``employmentType``, its neighbour doesn't), so
    a board-silent posting was having its stored ``is_remote`` blanked by a
    board-speaking sibling on every cycle that re-upserted it. That is #795's
    contradiction problem re-entering through the write path, after #851 stopped
    the tagger doing the same thing.

    The fix is at the mechanism, not the column: partition by key-set so every
    statement PostgREST builds is homogeneous, and the omission contract holds
    exactly. Groups are keyed on ``frozenset(row)`` — the count is bounded by
    the number of optional keys (2**k, k tiny: a board either publishes a field
    or it doesn't), so in practice this is one or two round trips, not N.

    GUARDED, because splitting would otherwise LOSE a failure. Two rows sharing
    a conflict key raise Postgres' "cannot affect row a second time" when they
    are in one statement — but if their key-sets differ, grouping puts them in
    DIFFERENT statements, both succeed, and whichever group runs later silently
    wins. That would make this helper strictly LESS fail-fast than the plain
    bulk upsert it replaces, in exactly the heterogeneous case it exists for. So
    the uniqueness invariant is enforced here, before splitting and regardless
    of how the key-sets fall, and a duplicate raises ``ValueError``.

    Nothing upstream guarantees it. The poller's ``_dedupe_by_content`` is a
    CONTENT dedupe, not a conflict-key dedupe: it keys on
    ``_content_dedupe_key(company_name, title)``, so a source returning two
    postings that share an ``external_id`` under DIFFERENT titles yields two
    distinct dedupe keys, both rows survive, and the duplicate conflict key
    reaches the write. The per-target path does not dedupe at all. Neither path
    is protected — hence the guard lives in the helper, so every caller
    inherits it rather than each having to know.

    Returns the ``RETURNING`` rows across the groups — the same ``resp.data``
    the callers iterate — RE-SORTED into the caller's input order, keyed on the
    ``on_conflict`` columns. Splitting the batch otherwise reshuffles the
    result, and Phase 2's daily-cap trim (``candidates[:quota]`` after a stable
    sort) resolves residual ties by position: an ordering change there would
    quietly alter WHICH equally-ranked postings get graded. Restoring the input
    order keeps this change a pure write-mechanism fix with no observable
    downstream effect. Rows we cannot key (a caller whose conflict columns
    aren't echoed back) keep their group order at the end.

    Each group rides :func:`poll_db_write`, so the semaphore and the
    transient-blip retry are unchanged; groups are issued sequentially rather
    than gathered so one source's split write can't multiply the cycle-wide
    write burst.

    NB the *semantics* stay "silence is not falsity": nothing here invents a
    value for a key the board didn't supply. It only stops one row's answer
    being applied to another row's column.
    """
    if not rows:
        return []
    key_cols = tuple(c.strip() for c in on_conflict.split(",") if c.strip())

    def _conflict_key(row: dict[str, Any]) -> tuple[Any, ...]:
        return tuple(row.get(c) for c in key_cols)

    # Enforce conflict-key uniqueness BEFORE splitting, so the failure mode is
    # the same whichever way the key-sets happen to partition. Rows missing a
    # conflict column are skipped: they can't match the conflict target on a
    # value visible here, so Postgres would INSERT both rather than error, and
    # flagging them would be a false positive.
    #
    # The message names the columns but NOT their values: this helper is
    # generic, a conflict key elsewhere could be an email or a user id, and an
    # exception string ends up in the platform log (the #885 lesson). The
    # caller's ``label`` already identifies the batch.
    seen: set[tuple[Any, ...]] = set()
    for row in rows:
        if not all(c in row for c in key_cols):
            continue
        key = _conflict_key(row)
        if key in seen:
            raise ValueError(
                f"poll_db_upsert: duplicate ({on_conflict}) key within one batch "
                f"for table {table!r} [{label}]. A single statement would raise "
                "a cardinality error; split across key-set groups it would "
                "silently last-write-wins. Deduplicate before calling."
            )
        seen.add(key)

    groups: dict[frozenset[str], list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(frozenset(row), []).append(row)

    upserted: list[dict[str, Any]] = []
    for group in groups.values():
        resp = await poll_db_write(
            supabase,
            lambda c, g=group: c.table(table).upsert(g, on_conflict=on_conflict),
            label=label,
        )
        upserted.extend(cast(list[dict[str, Any]], resp.data or []))

    if len(groups) > 1:
        position = {_conflict_key(r): i for i, r in enumerate(rows)}
        unmatched = len(rows)
        # Stable, so anything unkeyable keeps its relative order at the end.
        upserted.sort(key=lambda r: position.get(_conflict_key(r), unmatched))
    return upserted


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
