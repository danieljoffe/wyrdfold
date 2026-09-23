"""Cost-log CRUD. Every LLM completion or embedding call writes one row here.

Consumers call `record(...)` right after `client.complete(...)` (LLM) or
`record_embedding(...)` after `embed_client.embed(...)` with the result
+ a `purpose` label. Spend queries (`total_spend`, `spend_by_purpose`)
power the dashboard and any future budget guards.

The model column holds either a Claude ID or a Voyage ID — disambiguated
by the caller, opaque at the read layer.
"""

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any, cast

from supabase import AsyncClient, Client

from app.constants import resolve_owner
from app.models.embeddings import EmbeddingResult
from app.models.llm import LLMCallRecord, LLMResult
from app.services.supabase_retry import is_statement_timeout

TABLE = "llm_costs"

_log = logging.getLogger(__name__)

# PostgREST caps a single response at 1,000 rows on this deployment (verified
# against production: a request for 5,000 returned ``content-range: 0-999``).
# Every read in this module that sums or groups rows MUST page to exhaustion,
# because a capped page produces a total that is too SMALL — and the budget
# guards that consume these totals treat "too small" as "there is room left"
# (#1105).
_READ_PAGE = 1000
# A read this large means something is wrong with the window, not that we
# should spend minutes paging. Refuse to answer rather than answer slowly.
_MAX_READ_PAGES = 250


class SpendTotalUnavailableError(RuntimeError):
    """A spend total could not be established, so no number is returned.

    In plain terms: it is better to say "I do not know what has been spent"
    than to hand a budget guard a number that is too small. A guard given a
    too-small total concludes there is room left and permits work it should
    have blocked, which is strictly worse than pausing (#1105).

    Callers that GATE on spend must treat this as "blocked". Callers that
    merely DISPLAY spend must surface the gap rather than render a plausible
    wrong number.
    """


def _rpc_fallback_or_raise(exc: BaseException, rpc: str) -> None:
    """Decide whether an RPC failure may fall back to the client-side sum.

    The client-side fallback was written for exactly one situation: a deploy
    where the migration creating the RPC has not landed yet. That is a real
    transient and falling back is right for it.

    A statement timeout is NOT that situation. The function exists and the
    database is healthy; the query merely ran out of time. Substituting a
    client-side sum there swaps an exact answer for a paged walk of the same
    rows under the same clock — and historically for a silently capped one.
    So a timeout raises instead (#1105).
    """
    if is_statement_timeout(exc):
        _log.error(
            "%s hit the statement timeout — refusing to substitute a client-side sum, "
            "because an under-reported total makes a budget guard permit work it "
            "should block (#1105)",
            rpc,
        )
        raise SpendTotalUnavailableError(f"{rpc} exceeded the statement timeout") from exc
    # Genuinely unexpected, but survivable: log where it can be SEEN. This used
    # to be DEBUG, which production does not emit, so the degradation was
    # invisible for as long as it lasted.
    _log.warning(
        "%s failed (%s); falling back to the paginated client-side sum",
        rpc,
        type(exc).__name__,
        exc_info=True,
    )


def _read_pages(build: Callable[[], Any], *, label: str) -> list[dict[str, Any]]:
    """Read a select to exhaustion, one ``_READ_PAGE`` window at a time.

    ``build`` must return a FRESH query each call — postgrest builders carry
    their own state, so reusing one across pages does not re-range cleanly.

    Raises :class:`SpendTotalUnavailableError` rather than returning a partial set:
    a caller that wanted every row and got some of them has no way to tell.
    """
    rows: list[dict[str, Any]] = []
    for page in range(_MAX_READ_PAGES):
        offset = page * _READ_PAGE
        resp = build().range(offset, offset + _READ_PAGE - 1).execute()
        got = cast(list[dict[str, Any]], resp.data or [])
        rows.extend(got)
        if len(got) < _READ_PAGE:
            return rows
    raise SpendTotalUnavailableError(
        f"{label}: more than {_MAX_READ_PAGES * _READ_PAGE} rows in the window"
    )


async def _read_pages_async(build: Callable[[], Any], *, label: str) -> list[dict[str, Any]]:
    """Async mirror of :func:`_read_pages`. Same exhaustion rule, same refusal
    to return a partial set."""
    rows: list[dict[str, Any]] = []
    for page in range(_MAX_READ_PAGES):
        offset = page * _READ_PAGE
        resp = await build().range(offset, offset + _READ_PAGE - 1).execute()
        got = cast(list[dict[str, Any]], resp.data or [])
        rows.extend(got)
        if len(got) < _READ_PAGE:
            return rows
    raise SpendTotalUnavailableError(
        f"{label}: more than {_MAX_READ_PAGES * _READ_PAGE} rows in the window"
    )


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
        # ``transport`` (#1067): which wire path produced the result, merged
        # here for every row so a transport rollout reconciles from the ledger.
        "metadata": {
            **(metadata or {}),
            "cost_source": result.cost_source,
            "transport": result.transport or "unknown",
            **({"provider": result.provider} if result.provider else {}),
        },
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

    def build() -> Any:
        q = supabase.table(TABLE).select("cost_usd")
        q = q.eq("user_id", resolve_owner(user_id))
        if since is not None:
            q = q.gte("created_at", since.isoformat())
        return q

    rows = _read_pages(build, label="total_spend")
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
    except Exception as exc:
        _rpc_fallback_or_raise(exc, "total_spend_since")
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

    def build() -> Any:
        q = supabase.table(TABLE).select("cost_usd")
        q = q.eq("user_id", resolve_owner(user_id))
        if since is not None:
            q = q.gte("created_at", since.isoformat())
        return q

    rows = await _read_pages_async(build, label="total_spend")
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
    except Exception as exc:
        _rpc_fallback_or_raise(exc, "total_spend_since")
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

    def build() -> Any:
        q = supabase.table(TABLE).select("cost_usd,purpose")
        q = q.eq("user_id", resolve_owner(user_id))
        if since is not None:
            q = q.gte("created_at", since.isoformat())
        return q

    rows = _read_pages(build, label="total_billable_spend")
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
    except Exception as exc:
        _rpc_fallback_or_raise(exc, "total_billable_spend_since")
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

    def build() -> Any:
        q = supabase.table(TABLE).select("cost_usd,purpose")
        q = q.eq("user_id", resolve_owner(user_id))
        if since is not None:
            q = q.gte("created_at", since.isoformat())
        return q

    rows = await _read_pages_async(build, label="total_billable_spend")
    excluded = set(excluded_purposes)
    return round(
        sum(float(r["cost_usd"]) for r in rows if r.get("purpose") not in excluded),
        6,
    )


async def total_background_spend_async(
    supabase: AsyncClient,
    user_id: str | None,
    since: datetime | None = None,
) -> float:
    """Sum of ``cost_usd`` this user incurred on BACKGROUND work in the window.

    The exact complement of :func:`total_billable_spend_async`, and defined by
    subtracting it from the unfiltered total rather than by re-listing the
    purposes: ``entitlements.NON_BILLABLE_PURPOSES`` stays the single source of
    truth for what "background" means, so a purpose added there is picked up
    here for free and the two can never disagree about the same row.

    Why it exists: the per-payer DAILY ceiling meters unattended grading, and
    metering it with the unfiltered total let a user's own INTERACTIVE spend
    (résumé tailoring, analysis) exhaust it. That spend already has its own
    gate — ``user_llm_daily_budget_usd``, an order of magnitude larger — so the
    two ceilings would have fought: one afternoon of tailoring would silently
    stop that user's background grading for a day.

    Costs two RPC-backed reads rather than one. Both are exact and neither is
    subject to the PostgREST row cap a client-side sum over a purpose filter
    would hit, which is why this is a subtraction and not a new query.
    """
    from app.services.entitlements import NON_BILLABLE_PURPOSES

    total = await total_spend_async(supabase, user_id=user_id, since=since)
    interactive = await total_billable_spend_async(
        supabase, user_id, since, excluded_purposes=NON_BILLABLE_PURPOSES
    )
    return round(max(0.0, total - interactive), 6)


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
    except Exception as exc:
        _rpc_fallback_or_raise(exc, "total_billable_spend_since")
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

    def build() -> Any:
        q = supabase.table(TABLE).select("cost_usd")
        if since is not None:
            q = q.gte("created_at", since.isoformat())
        return q

    rows = _read_pages(build, label="total_spend_all")
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
    except Exception as exc:
        _rpc_fallback_or_raise(exc, "total_spend_all_since")
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

    def build() -> Any:
        q = supabase.table(TABLE).select("cost_usd")
        if since is not None:
            q = q.gte("created_at", since.isoformat())
        return q

    rows = await _read_pages_async(build, label="total_spend_all")
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
    except Exception as exc:
        _rpc_fallback_or_raise(exc, "total_spend_all_since")
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
    def build() -> Any:
        q = supabase.table(TABLE).select("purpose, cost_usd")
        q = q.eq("user_id", resolve_owner(user_id))
        if since is not None:
            q = q.gte("created_at", since.isoformat())
        return q

    rows = _read_pages(build, label="spend_by_purpose")
    totals: dict[str, float] = {}
    for r in rows:
        totals[r["purpose"]] = totals.get(r["purpose"], 0.0) + float(r["cost_usd"])
    return {k: round(v, 6) for k, v in totals.items()}


def spend_by_purpose_all(
    supabase: Client,
    since: datetime | None = None,
) -> dict[str, float]:
    """Per-purpose spend across ALL users over the window.

    Powers the operator cost-summary endpoint (#26 F4). No RPC variant: the
    operator surface is queried infrequently, so a client-side group is fine
    — but it must PAGE. The earlier note here said the table is "bounded by
    retention, so a client-side group is fine"; that was wrong. PostgREST
    caps one response at 1,000 rows and the windows this is called with
    exceed that, so the breakdown was computed from a fraction of the
    matching rows on EVERY call — not only when something failed (#1105).
    """

    def build() -> Any:
        q = supabase.table(TABLE).select("purpose, cost_usd")
        if since is not None:
            q = q.gte("created_at", since.isoformat())
        return q

    rows = _read_pages(build, label="spend_by_purpose_all")
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
    as the sync version, paged to exhaustion so the breakdown is not computed
    from a capped page (#1105)."""

    def build() -> Any:
        q = supabase.table(TABLE).select("purpose, cost_usd")
        if since is not None:
            q = q.gte("created_at", since.isoformat())
        return q

    rows = await _read_pages_async(build, label="spend_by_purpose_all")
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
    infrequently, so a client-side sum is fine — paged to exhaustion, same
    posture as ``spend_by_purpose_all`` and for the same reason (#1105).
    """

    def build() -> Any:
        q = supabase.table(TABLE).select(
            "input_tokens, cache_read_input_tokens, cache_creation_input_tokens"
        )
        if since is not None:
            q = q.gte("created_at", since.isoformat())
        return q

    rows = _read_pages(build, label="cache_metrics_all")
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
    client-side sum as the sync version, paged to exhaustion (#1105)."""

    def build() -> Any:
        q = supabase.table(TABLE).select(
            "input_tokens, cache_read_input_tokens, cache_creation_input_tokens"
        )
        if since is not None:
            q = q.gte("created_at", since.isoformat())
        return q

    rows = await _read_pages_async(build, label="cache_metrics_all")
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
    except Exception as exc:
        _rpc_fallback_or_raise(exc, "spend_by_purpose_since")
        return _spend_by_purpose_python(supabase, user_id, since)

    raw = resp.data
    if not raw:
        return {}
    return {k: round(float(v), 6) for k, v in cast(dict[str, Any], raw).items()}
