"""Retention purge for append-only operational logs (#29 P3).

``llm_costs`` and ``notifications_sent`` grow unbounded and are never
otherwise pruned. This service deletes rows older than a configured
window so the data-retention posture is an explicit, bounded choice
rather than "keep operational PII forever by default".

Runs with the **async service-role** client (#57 slice 1) — background
maintenance with no JWT, awaited directly on the event loop instead of
hopping through ``asyncio.to_thread``. Each delete is a single filtered
statement (``WHERE ts < cutoff``), so there is no id-list to marshal and
``returning="minimal"`` keeps the response small even on a large first
sweep; the count comes from the ``Content-Range`` header (``resp.count``).

Window semantics: ``days <= 0`` means **retain indefinitely** — that
table is skipped. The defaults (set in ``config.py``) are deliberately
generous because both logs feed live features:

* ``llm_costs.created_at`` backs the rolling budget windows (≤30d) and
  the cost / insights history — purging it early would distort spend
  history, so the floor is a year.
* ``notifications_sent.sent_at`` is the alert-dedup ledger; its window
  only needs to outlast a posting's active life.
* ``search_events.occurred_at`` is the search-funnel metrics ledger (#467
  §10 PR6). Its ``query`` column is raw-ish user input, so bounded
  retention is part of the privacy posture; 90 days covers funnel
  iteration.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from postgrest.types import CountMethod, ReturnMethod
from supabase import AsyncClient

logger = logging.getLogger(__name__)

# (table, age column) pairs purged by this service.
_LLM_COSTS = ("llm_costs", "created_at")
_NOTIFICATIONS = ("notifications_sent", "sent_at")
_SEARCH_EVENTS = ("search_events", "occurred_at")


async def _purge_table(supabase: AsyncClient, table: str, ts_col: str, days: int) -> int:
    """Delete rows in ``table`` whose ``ts_col`` is older than ``days``.

    ``days <= 0`` retains indefinitely (skipped). Returns the number of
    rows deleted.
    """
    if days <= 0:
        logger.info("retention: %s retained indefinitely (days=%d)", table, days)
        return 0
    cutoff = datetime.now(UTC) - timedelta(days=days)
    resp = await (
        supabase.table(table)
        .delete(count=CountMethod.exact, returning=ReturnMethod.minimal)
        .lt(ts_col, cutoff.isoformat())
        .execute()
    )
    deleted = resp.count or 0
    logger.info(
        "retention: purged %d rows from %s older than %dd (cutoff=%s)",
        deleted,
        table,
        days,
        cutoff.isoformat(),
    )
    return deleted


async def purge_expired_records(
    supabase: AsyncClient,
    *,
    llm_costs_days: int,
    notifications_sent_days: int,
    search_events_days: int,
) -> dict[str, int]:
    """Purge expired rows from the logs; return a per-table deleted count.

    Idempotent — a second run within the same window deletes nothing.
    """
    return {
        _LLM_COSTS[0]: await _purge_table(supabase, *_LLM_COSTS, llm_costs_days),
        _NOTIFICATIONS[0]: await _purge_table(supabase, *_NOTIFICATIONS, notifications_sent_days),
        _SEARCH_EVENTS[0]: await _purge_table(supabase, *_SEARCH_EVENTS, search_events_days),
    }
