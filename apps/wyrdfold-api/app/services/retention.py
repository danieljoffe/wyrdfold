"""Retention purge for append-only operational logs (#29 P3).

``llm_costs`` and ``notifications_sent`` grow unbounded and are never
otherwise pruned. This service deletes rows older than a configured
window so the data-retention posture is an explicit, bounded choice
rather than "keep operational PII forever by default".

Runs with the **service-role** client — background maintenance with no
JWT. Each delete is a single filtered statement (``WHERE ts < cutoff``),
so there is no id-list to marshal and ``returning="minimal"`` keeps the
response small even on a large first sweep; the count comes from the
``Content-Range`` header (``resp.count``).

Window semantics: ``days <= 0`` means **retain indefinitely** — that
table is skipped. The defaults (set in ``config.py``) are deliberately
generous because both logs feed live features:

* ``llm_costs.created_at`` backs the rolling budget windows (≤30d) and
  the cost / insights history — purging it early would distort spend
  history, so the floor is a year.
* ``notifications_sent.sent_at`` is the alert-dedup ledger; its window
  only needs to outlast a posting's active life.
* ``prescan_shadow.observed_at`` is the pre-scan disagreement shadow log
  (#60) — explicitly temporary analysis data, append-only with no other
  lifecycle (2026-07-02 audit: access-hardened but not lifecycle-hardened,
  so it accumulated forever while the flag was on). 30 days comfortably
  covers an analysis window; the off-ramp remains "analyse, then drop the
  table".
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from postgrest.types import CountMethod, ReturnMethod
from supabase import Client

logger = logging.getLogger(__name__)

# (table, age column) pairs purged by this service.
_LLM_COSTS = ("llm_costs", "created_at")
_NOTIFICATIONS = ("notifications_sent", "sent_at")
_PRESCAN_SHADOW = ("prescan_shadow", "observed_at")


def _purge_table(supabase: Client, table: str, ts_col: str, days: int) -> int:
    """Delete rows in ``table`` whose ``ts_col`` is older than ``days``.

    ``days <= 0`` retains indefinitely (skipped). Returns the number of
    rows deleted.
    """
    if days <= 0:
        logger.info("retention: %s retained indefinitely (days=%d)", table, days)
        return 0
    cutoff = datetime.now(UTC) - timedelta(days=days)
    resp = (
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


def purge_expired_records(
    supabase: Client,
    *,
    llm_costs_days: int,
    notifications_sent_days: int,
    prescan_shadow_days: int,
) -> dict[str, int]:
    """Purge expired rows from the logs; return a per-table deleted count.

    Idempotent — a second run within the same window deletes nothing.
    """
    return {
        _LLM_COSTS[0]: _purge_table(supabase, *_LLM_COSTS, llm_costs_days),
        _NOTIFICATIONS[0]: _purge_table(supabase, *_NOTIFICATIONS, notifications_sent_days),
        _PRESCAN_SHADOW[0]: _purge_table(supabase, *_PRESCAN_SHADOW, prescan_shadow_days),
    }
