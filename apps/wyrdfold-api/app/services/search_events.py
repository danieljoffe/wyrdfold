"""Search-funnel instrumentation (#467 §10 PR6).

Fire-and-forget writes into the ``search_events`` ledger — search volume,
zero-result coverage gaps, and the card-open / signup-click conversion
ticks. Reuses the buffered bulk-INSERT machinery (`CostLogBuffer`,
table-parameterized) so instrumentation adds **zero** DB round-trips to
the request path: ``enqueue`` is an O(1) in-memory append and the
background task drains the buffer every few seconds on the service-role
client (the table is deny-all; only service-role can write).

PRIVACY — the row builders are the whole contract: they have no
``user_id`` / IP parameters, and the table has no such columns (the
migration is the backstop). ``query`` is normalized (trim/lower/cap) the
same way the response cache keys it. Losing rows is acceptable —
analytics, not billing — so callers never await, retry, or surface
errors for this.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from app.services.llm.cost_log_buffer import CostLogBuffer

_log = logging.getLogger(__name__)

Surface = Literal["public", "authed"]

# Mirror the endpoint's input bounds (defense in depth alongside the DB
# CHECKs — an oversized value is truncated, not dropped or errored).
_MAX_QUERY = 120
_MAX_LOCATION = 100

# Module-level singleton, started/stopped in the FastAPI lifespan next to
# the cost-log buffer. Analytics-grade: a modest ceiling — under a
# sustained flush outage the oldest rows are dropped (logged), which for
# funnel metrics is the right trade.
buffer = CostLogBuffer(table="search_events", label="search-events", max_rows=5_000)


def _enqueue(row: dict[str, Any]) -> None:
    """Never-raise choke point: instrumentation must not be able to fail a
    search request or the beacon, whatever goes wrong in the buffer."""
    try:
        buffer.enqueue(row)
    except Exception:  # pragma: no cover - the buffer is designed not to raise
        _log.exception("search-events enqueue failed; event dropped")


def record_search(
    *,
    surface: Surface,
    q: str,
    result_count: int,
    has_more: bool,
    offset: int,
    location: str | None,
    posted_within_days: int | None,
) -> None:
    """Enqueue one ``search`` event (cache hits included — a cached
    response is still a user search; volume must count it)."""
    row: dict[str, Any] = {
        "event_type": "search",
        "surface": surface,
        "query": q.strip().lower()[:_MAX_QUERY],
        "result_count": result_count,
        "has_more": has_more,
        "page_offset": offset,
        "location": ((location or "").strip().lower()[:_MAX_LOCATION]) or None,
        "posted_within_days": posted_within_days,
    }
    _enqueue(row)


def record_funnel_event(
    *,
    event_type: Literal["card_open", "signup_click"],
    surface: Surface,
    job_posting_id: str | None,
) -> None:
    """Enqueue one browser-originated funnel tick (via the beacon
    endpoint). ``surface`` is the client's claim — analytics-grade, never
    used for authorization."""
    _enqueue(
        {
            "event_type": event_type,
            "surface": surface,
            "job_posting_id": job_posting_id,
        }
    )
