"""Cost-log CRUD. Every LLM completion or embedding call writes one row here.

Consumers call `record(...)` right after `client.complete(...)` (LLM) or
`record_embedding(...)` after `embed_client.embed(...)` with the result
+ a `purpose` label. Spend queries (`total_spend`, `spend_by_purpose`)
power the dashboard and any future budget guards.

The model column holds either a Claude ID or a Voyage ID — disambiguated
by the caller, opaque at the read layer.
"""

import logging
from datetime import datetime
from typing import Any, cast

from supabase import AsyncClient, Client

from app.constants import resolve_owner
from app.models.embeddings import EmbeddingResult
from app.models.llm import LLMCallRecord, LLMResult

TABLE = "llm_costs"

_log = logging.getLogger(__name__)


def _insert_row(supabase: Client, row: dict[str, Any]) -> LLMCallRecord:
    resp = supabase.table(TABLE).insert(row).execute()
    rows = cast(list[dict[str, Any]], resp.data or [])
    if not rows:
        raise RuntimeError("Failed to insert llm_costs row")
    return LLMCallRecord.model_validate(rows[0])


def _embedding_row_for(
    *,
    user_id: str | None,
    purpose: str,
    result: EmbeddingResult,
    metadata: dict[str, str | int | float | bool] | None,
) -> dict[str, Any]:
    return {
        "user_id": resolve_owner(user_id),
        "model": result.model,
        "purpose": purpose,
        "input_tokens": result.usage.input_tokens,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cost_usd": result.cost_usd,
        "latency_ms": result.latency_ms,
        "metadata": metadata or {},
    }


async def record_async(
    supabase: AsyncClient,
    user_id: str | None,
    purpose: str,
    result: LLMResult,
    metadata: dict[str, str | int | float | bool] | None = None,
) -> LLMCallRecord:
    """Async mirror of :func:`record` (#57 slice 3).

    The interactive cost write for an ``async def`` handler on the pooled async
    service client — the row lands on the event loop instead of a threadpool
    worker. Same immediate-INSERT semantics as :func:`record` (budget guard sees
    fresh totals). The sync :func:`record` stays for the poller/batch paths."""
    resp = (
        await supabase.table(TABLE)
        .insert(_row_for(user_id=user_id, purpose=purpose, result=result, metadata=metadata))
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    if not rows:
        raise RuntimeError("Failed to insert llm_costs row")
    return LLMCallRecord.model_validate(rows[0])


async def record_embedding_async(
    supabase: AsyncClient,
    user_id: str | None,
    purpose: str,
    result: EmbeddingResult,
    metadata: dict[str, str | int | float | bool] | None = None,
) -> LLMCallRecord:
    """Async mirror of :func:`record_embedding` (#57 slice 3) for ``async def``
    callers on the pooled async client."""
    resp = (
        await supabase.table(TABLE)
        .insert(
            _embedding_row_for(user_id=user_id, purpose=purpose, result=result, metadata=metadata)
        )
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    if not rows:
        raise RuntimeError("Failed to insert llm_costs row")
    return LLMCallRecord.model_validate(rows[0])


def record(
    supabase: Client,
    user_id: str | None,
    purpose: str,
    result: LLMResult,
    metadata: dict[str, str | int | float | bool] | None = None,
) -> LLMCallRecord:
    # Same row shape as `record_async` and `enqueue` — built by `_row_for` so
    # the three writers cannot drift (they had already diverged by a copy).
    return _insert_row(
        supabase,
        _row_for(user_id=user_id, purpose=purpose, result=result, metadata=metadata),
    )


def record_embedding(
    supabase: Client,
    user_id: str | None,
    purpose: str,
    result: EmbeddingResult,
    metadata: dict[str, str | int | float | bool] | None = None,
) -> LLMCallRecord:
    return _insert_row(
        supabase,
        {
            "user_id": resolve_owner(user_id),
            "model": result.model,
            "purpose": purpose,
            "input_tokens": result.usage.input_tokens,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cost_usd": result.cost_usd,
            "latency_ms": result.latency_ms,
            "metadata": metadata or {},
        },
    )


def _row_for(
    *,
    user_id: str | None,
    purpose: str,
    result: LLMResult,
    metadata: dict[str, str | int | float | bool] | None,
) -> dict[str, Any]:
    return {
        "user_id": resolve_owner(user_id),
        "model": result.model,
        "purpose": purpose,
        "input_tokens": result.usage.input_tokens,
        "output_tokens": result.usage.output_tokens,
        "cache_read_input_tokens": result.usage.cache_read_input_tokens,
        "cache_creation_input_tokens": result.usage.cache_creation_input_tokens,
        "cost_usd": result.cost_usd,
        "latency_ms": result.latency_ms,
        # Stamp where cost_usd came from (#933). `metadata` is jsonb, so this
        # needs no migration, and it makes "are we still estimating?"
        # answerable from the table itself rather than by reading the code.
        # Embedding rows carry no cost_source: Voyage reports no cost, so
        # `_embedding_row_for` is always a table estimate.
        "metadata": {**(metadata or {}), "cost_source": result.cost_source},
    }


def enqueue(
    user_id: str | None,
    purpose: str,
    result: LLMResult,
    metadata: dict[str, str | int | float | bool] | None = None,
) -> None:
    """Async cost-log path for cron callers.

    Appends the row to the in-memory `cost_log_buffer.buffer` instead of
    issuing a synchronous INSERT. The buffer's background task batches
    rows into a single bulk INSERT every few seconds. Use this anywhere
    the call is system-driven (poller, batch endpoints) where the spend
    record doesn't need to be queryable immediately.

    Interactive paths (analysis, tailor, conversation) should keep using
    `record(...)` so the budget guard sees fresh totals on the next call.
    """
    # Imported here to avoid a circular import: the buffer module
    # imports `Client` from supabase, which is fine, but importing
    # `cost_log_buffer` at the top of `cost_log` would tie module
    # initialization order across services unnecessarily.
    from app.services.llm.cost_log_buffer import buffer

    buffer.enqueue(_row_for(user_id=user_id, purpose=purpose, result=result, metadata=metadata))


def list_recent(
    supabase: Client,
    user_id: str | None,
    limit: int = 100,
) -> list[LLMCallRecord]:
    query = supabase.table(TABLE).select("*").order("created_at", desc=True).limit(limit)
    query = query.eq("user_id", resolve_owner(user_id))
    resp = query.execute()
    rows = cast(list[dict[str, Any]], resp.data or [])
    return [LLMCallRecord.model_validate(r) for r in rows]


def _total_spend_python(
    supabase: Client,
    user_id: str | None,
    since: datetime | None,
) -> float:
    """Fallback used when the Postgres RPC is unavailable (e.g. mid-deploy
    before the migration lands). Selects every row in the window and sums
    in Python — O(rows) on the wire and in memory."""
    query = supabase.table(TABLE).select("cost_usd")
    query = query.eq("user_id", resolve_owner(user_id))
    if since is not None:
        query = query.gte("created_at", since.isoformat())
    resp = query.execute()
    rows = cast(list[dict[str, Any]], resp.data or [])
    return round(sum(float(r["cost_usd"]) for r in rows), 6)


def total_spend(
    supabase: Client,
    user_id: str | None,
    since: datetime | None = None,
) -> float:
    """Sum of `cost_usd` over the window for this user.

    Tries the `total_spend_since` RPC first — Postgres returns a single
    `numeric` regardless of usage volume. Falls back to a client-side
    select+sum if the RPC isn't deployed yet, so the budget guard never
    fails closed during a partial deploy.
    """
    try:
        resp = supabase.rpc(
            "total_spend_since",
            {
                "p_user_id": resolve_owner(user_id),
                "p_since": since.isoformat() if since is not None else None,
            },
        ).execute()
    except Exception:
        _log.debug("total_spend_since RPC unavailable, falling back to client-side sum")
        return _total_spend_python(supabase, user_id, since)

    # PostgREST returns scalar function results as the bare value (or in
    # `data` depending on client version). Numeric → str | int | float.
    raw = resp.data
    if raw is None:
        return 0.0
    return round(float(cast(Any, raw)), 6)


async def _total_spend_python_async(
    supabase: AsyncClient,
    user_id: str | None,
    since: datetime | None,
) -> float:
    """Async mirror of :func:`_total_spend_python` (#57 PR-F). Same select+sum
    fallback, awaited on the pooled async user client."""
    query = supabase.table(TABLE).select("cost_usd")
    query = query.eq("user_id", resolve_owner(user_id))
    if since is not None:
        query = query.gte("created_at", since.isoformat())
    resp = await query.execute()
    rows = cast(list[dict[str, Any]], resp.data or [])
    return round(sum(float(r["cost_usd"]) for r in rows), 6)


async def total_spend_async(
    supabase: AsyncClient,
    user_id: str | None,
    since: datetime | None = None,
) -> float:
    """Async mirror of :func:`total_spend` (#57 PR-F).

    The user-scoped spend read for an ``async def`` handler on the pooled async
    RLS user client — same ``total_spend_since`` RPC-first / client-side-sum
    fallback and same rounding as the sync version, awaited instead of run in a
    threadpool. The sync :func:`total_spend` stays for the budget-gate / payer
    paths."""
    try:
        resp = await supabase.rpc(
            "total_spend_since",
            {
                "p_user_id": resolve_owner(user_id),
                "p_since": since.isoformat() if since is not None else None,
            },
        ).execute()
    except Exception:
        _log.debug("total_spend_since RPC unavailable, falling back to client-side sum")
        return await _total_spend_python_async(supabase, user_id, since)

    raw = resp.data
    if raw is None:
        return 0.0
    return round(float(cast(Any, raw)), 6)


def _total_billable_spend_python(
    supabase: Client,
    user_id: str | None,
    since: datetime | None,
    excluded_purposes: tuple[str, ...],
) -> float:
    """Fallback for :func:`total_billable_spend` when the RPC is
    unavailable (mid-deploy). Selects (cost_usd, purpose) rows in the
    window and filters/sums in Python."""
    query = supabase.table(TABLE).select("cost_usd,purpose")
    query = query.eq("user_id", resolve_owner(user_id))
    if since is not None:
        query = query.gte("created_at", since.isoformat())
    resp = query.execute()
    rows = cast(list[dict[str, Any]], resp.data or [])
    excluded = set(excluded_purposes)
    return round(
        sum(float(r["cost_usd"]) for r in rows if r.get("purpose") not in excluded),
        6,
    )


def total_billable_spend(
    supabase: Client,
    user_id: str | None,
    since: datetime | None = None,
    *,
    excluded_purposes: tuple[str, ...],
) -> float:
    """Sum of `cost_usd` over the window, excluding background purposes.

    The managed-tier quota accounting (Phase 3): the ledger attributes
    catalog/background work to the triggering user, and the quota must
    count only what the user actively clicked for. Tries the
    `total_billable_spend_since` RPC, falling back client-side like
    :func:`total_spend` so the guard never fails closed mid-deploy.
    """
    try:
        resp = supabase.rpc(
            "total_billable_spend_since",
            {
                "p_user_id": resolve_owner(user_id),
                "p_since": since.isoformat() if since is not None else None,
                "p_excluded_purposes": list(excluded_purposes),
            },
        ).execute()
    except Exception:
        _log.debug("total_billable_spend_since RPC unavailable, falling back to client-side sum")
        return _total_billable_spend_python(supabase, user_id, since, excluded_purposes)

    raw = resp.data
    if raw is None:
        return 0.0
    return round(float(cast(Any, raw)), 6)


async def _total_billable_spend_python_async(
    supabase: AsyncClient,
    user_id: str | None,
    since: datetime | None,
    excluded_purposes: tuple[str, ...],
) -> float:
    """Async mirror of :func:`_total_billable_spend_python` (#57 PR-F). Same
    select + purpose-filter + sum fallback, awaited on the async user client."""
    query = supabase.table(TABLE).select("cost_usd,purpose")
    query = query.eq("user_id", resolve_owner(user_id))
    if since is not None:
        query = query.gte("created_at", since.isoformat())
    resp = await query.execute()
    rows = cast(list[dict[str, Any]], resp.data or [])
    excluded = set(excluded_purposes)
    return round(
        sum(float(r["cost_usd"]) for r in rows if r.get("purpose") not in excluded),
        6,
    )


async def total_billable_spend_async(
    supabase: AsyncClient,
    user_id: str | None,
    since: datetime | None = None,
    *,
    excluded_purposes: tuple[str, ...],
) -> float:
    """Async mirror of :func:`total_billable_spend` (#57 PR-F).

    The user-scoped billable-spend read for an ``async def`` handler on the
    pooled async RLS user client — same ``total_billable_spend_since`` RPC-first
    / client-side fallback and rounding as the sync version, awaited instead of
    threadpooled. The sync :func:`total_billable_spend` stays for the budget-gate
    paths."""
    try:
        resp = await supabase.rpc(
            "total_billable_spend_since",
            {
                "p_user_id": resolve_owner(user_id),
                "p_since": since.isoformat() if since is not None else None,
                "p_excluded_purposes": list(excluded_purposes),
            },
        ).execute()
    except Exception:
        _log.debug("total_billable_spend_since RPC unavailable, falling back to client-side sum")
        return await _total_billable_spend_python_async(supabase, user_id, since, excluded_purposes)

    raw = resp.data
    if raw is None:
        return 0.0
    return round(float(cast(Any, raw)), 6)


def _total_spend_all_python(
    supabase: Client,
    since: datetime | None,
) -> float:
    """Fallback for ``total_spend_all`` when the RPC is unavailable (e.g.
    mid-deploy before the migration lands). Selects every row in the window
    and sums in Python — O(rows) on the wire."""
    query = supabase.table(TABLE).select("cost_usd")
    if since is not None:
        query = query.gte("created_at", since.isoformat())
    resp = query.execute()
    rows = cast(list[dict[str, Any]], resp.data or [])
    return round(sum(float(r["cost_usd"]) for r in rows), 6)


def total_spend_all(
    supabase: Client,
    since: datetime | None = None,
) -> float:
    """Sum of ``cost_usd`` across ALL users over the window.

    Powers the global LLM circuit breaker, called once per poll cycle.
    Tries the ``total_spend_all_since`` RPC first — Postgres returns a single
    ``numeric`` regardless of the day's call volume, instead of transferring
    every row. Falls back to a client-side select+sum if the RPC isn't
    deployed yet, so the breaker never fails during a partial deploy
    (mirrors ``total_spend``).
    """
    try:
        resp = supabase.rpc(
            "total_spend_all_since",
            {"p_since": since.isoformat() if since is not None else None},
        ).execute()
    except Exception:
        _log.debug("total_spend_all_since RPC unavailable, falling back to client-side sum")
        return _total_spend_all_python(supabase, since)

    raw = resp.data
    if raw is None:
        return 0.0
    return round(float(cast(Any, raw)), 6)


async def _total_spend_all_python_async(
    supabase: AsyncClient,
    since: datetime | None,
) -> float:
    """Async mirror of :func:`_total_spend_all_python` (#57 PR-G2c). Same
    all-users select+sum fallback, awaited on the pooled async service client."""
    query = supabase.table(TABLE).select("cost_usd")
    if since is not None:
        query = query.gte("created_at", since.isoformat())
    resp = await query.execute()
    rows = cast(list[dict[str, Any]], resp.data or [])
    return round(sum(float(r["cost_usd"]) for r in rows), 6)


async def total_spend_all_async(
    supabase: AsyncClient,
    since: datetime | None = None,
) -> float:
    """Async mirror of :func:`total_spend_all` (#57 PR-G2c).

    The all-users spend read for an ``async def`` handler (the operator
    cost-summary) on the pooled async service client — same
    ``total_spend_all_since`` RPC-first / client-side-sum fallback and rounding
    as the sync version, awaited instead of threadpooled. The sync
    :func:`total_spend_all` stays for the poller/ingestion-health callers."""
    try:
        resp = await supabase.rpc(
            "total_spend_all_since",
            {"p_since": since.isoformat() if since is not None else None},
        ).execute()
    except Exception:
        _log.debug("total_spend_all_since RPC unavailable, falling back to client-side sum")
        return await _total_spend_all_python_async(supabase, since)

    raw = resp.data
    if raw is None:
        return 0.0
    return round(float(cast(Any, raw)), 6)


def _spend_by_purpose_python(
    supabase: Client,
    user_id: str | None,
    since: datetime | None,
) -> dict[str, float]:
    query = supabase.table(TABLE).select("purpose, cost_usd")
    query = query.eq("user_id", resolve_owner(user_id))
    if since is not None:
        query = query.gte("created_at", since.isoformat())
    resp = query.execute()
    rows = cast(list[dict[str, Any]], resp.data or [])
    totals: dict[str, float] = {}
    for r in rows:
        totals[r["purpose"]] = totals.get(r["purpose"], 0.0) + float(r["cost_usd"])
    return {k: round(v, 6) for k, v in totals.items()}


def spend_by_purpose_all(
    supabase: Client,
    since: datetime | None = None,
) -> dict[str, float]:
    """Per-purpose spend across ALL users over the window.

    Powers the operator cost-summary endpoint (#26 F4). No RPC variant
    — the operator surface is queried infrequently, and the table is
    bounded by retention, so a client-side group is fine.
    """
    query = supabase.table(TABLE).select("purpose, cost_usd")
    if since is not None:
        query = query.gte("created_at", since.isoformat())
    resp = query.execute()
    rows = cast(list[dict[str, Any]], resp.data or [])
    totals: dict[str, float] = {}
    for r in rows:
        totals[r["purpose"]] = totals.get(r["purpose"], 0.0) + float(r["cost_usd"])
    return {k: round(v, 6) for k, v in totals.items()}


async def spend_by_purpose_all_async(
    supabase: AsyncClient,
    since: datetime | None = None,
) -> dict[str, float]:
    """Async mirror of :func:`spend_by_purpose_all` (#57 PR-G2c).

    Per-purpose spend across ALL users, awaited on the async service client for
    the operator cost-summary handler. No RPC variant — same client-side group
    as the sync version (the operator surface is queried infrequently)."""
    query = supabase.table(TABLE).select("purpose, cost_usd")
    if since is not None:
        query = query.gte("created_at", since.isoformat())
    resp = await query.execute()
    rows = cast(list[dict[str, Any]], resp.data or [])
    totals: dict[str, float] = {}
    for r in rows:
        totals[r["purpose"]] = totals.get(r["purpose"], 0.0) + float(r["cost_usd"])
    return {k: round(v, 6) for k, v in totals.items()}


def cache_metrics_all(
    supabase: Client,
    since: datetime | None = None,
) -> dict[str, int]:
    """Sum prompt-cache token usage across ALL users over the window.

    Returns ``{"cache_read", "cache_creation", "uncached_input"}`` — the
    three Anthropic input-token buckets (``input_tokens`` is the
    non-cached portion). Powers the cache hit-rate line on the operator
    cost-summary (#73). No RPC variant: the operator surface is queried
    infrequently and the table is bounded by retention, so a client-side
    sum is fine — same posture as ``spend_by_purpose_all``.
    """
    query = supabase.table(TABLE).select(
        "input_tokens, cache_read_input_tokens, cache_creation_input_tokens"
    )
    if since is not None:
        query = query.gte("created_at", since.isoformat())
    resp = query.execute()
    rows = cast(list[dict[str, Any]], resp.data or [])
    return {
        "cache_read": sum(int(r["cache_read_input_tokens"]) for r in rows),
        "cache_creation": sum(int(r["cache_creation_input_tokens"]) for r in rows),
        "uncached_input": sum(int(r["input_tokens"]) for r in rows),
    }


async def cache_metrics_all_async(
    supabase: AsyncClient,
    since: datetime | None = None,
) -> dict[str, int]:
    """Async mirror of :func:`cache_metrics_all` (#57 PR-G2c).

    Prompt-cache token buckets across ALL users, awaited on the async service
    client for the operator cost-summary handler. No RPC variant — same
    client-side sum as the sync version."""
    query = supabase.table(TABLE).select(
        "input_tokens, cache_read_input_tokens, cache_creation_input_tokens"
    )
    if since is not None:
        query = query.gte("created_at", since.isoformat())
    resp = await query.execute()
    rows = cast(list[dict[str, Any]], resp.data or [])
    return {
        "cache_read": sum(int(r["cache_read_input_tokens"]) for r in rows),
        "cache_creation": sum(int(r["cache_creation_input_tokens"]) for r in rows),
        "uncached_input": sum(int(r["input_tokens"]) for r in rows),
    }


def spend_by_purpose(
    supabase: Client,
    user_id: str | None,
    since: datetime | None = None,
) -> dict[str, float]:
    """Per-purpose spend breakdown over the window.

    Same RPC-first / client-fallback pattern as `total_spend`.
    """
    try:
        resp = supabase.rpc(
            "spend_by_purpose_since",
            {
                "p_user_id": resolve_owner(user_id),
                "p_since": since.isoformat() if since is not None else None,
            },
        ).execute()
    except Exception:
        _log.debug("spend_by_purpose_since RPC unavailable, falling back to client-side group")
        return _spend_by_purpose_python(supabase, user_id, since)

    raw = resp.data
    if not raw:
        return {}
    return {k: round(float(v), 6) for k, v in cast(dict[str, Any], raw).items()}
