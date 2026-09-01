"""Per-target daily COUNT cap on Phase-1 triage calls (#930).

Phase 2 has had ``phase2_daily_cap`` since #6 (see
``app/services/fit/daily_cap.py``). Phase 1 had no count ceiling at all —
only the global daily spend budget (``_global_budget_exhausted``) and the
payer's rolling monthly allowance. Both are *dollar* rails: they bound the
bill, not the call volume, and neither can distinguish "the pipeline is
working" from "something is looping".

This module is the count rail. It counts the same way the Phase-2 cap does
— ``llm_costs`` rows for the purpose since UTC midnight, filtered on
``metadata.target_id`` — so there is no second counter table and no second
source of truth.

**One counter, two spenders.** The activation backfill
(:mod:`app.services.relevance.phase1_backfill`) and ordinary poll-cycle
ingestion both write ``purpose='relevance.title_triage'`` cost rows with the
same ``metadata.target_id``, so they draw on the SAME daily count. The
backfill additionally clamps itself to ``phase1_backfill_cap_fraction`` of
the cap, so a large activation can never consume the whole day and starve
the day's new intake.

Failure posture differs by caller, deliberately:

- :func:`phase1_cap_reached` fails OPEN (a count-read error returns
  ``False``, keep triaging). It sits in the poller's triage loops next to
  :func:`app.services.poller._triage_budget_blocks`, which fails open for
  the same reason: a Phase-1 stall PAUSES ADMISSION (#285/#294), so a
  transient count-read blip must not stop new listings entering the catalog.
- :func:`phase1_backfill_allowance` fails CLOSED (returns 0 — do not
  spend). A backfill that does not run costs a user nothing they had
  yesterday; a backfill that overruns an unreadable cap costs money.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, time

from supabase import AsyncClient

from app.config import settings
from app.services.relevance.title_triage import PHASE1_PURPOSE

logger = logging.getLogger(__name__)


def _utc_day_start() -> str:
    """ISO-8601 UTC midnight of today — the rollover the Phase-2 cap uses."""
    return datetime.combine(datetime.now(UTC).date(), time.min, tzinfo=UTC).isoformat()


async def phase1_calls_today(supabase: AsyncClient, target_id: str) -> int | None:
    """Phase-1 triage calls billed to ``target_id`` since UTC midnight.

    ``None`` means "could not be read" — NOT zero. Callers pick their own
    posture for the unknown (see the module docstring); collapsing it to 0
    here would silently hand every caller a fail-open they did not choose.
    """
    try:
        resp = await (
            supabase.table("llm_costs")
            # head=True → count only, no rows shipped.
            .select("id", count="exact", head=True)  # type: ignore[arg-type]
            .eq("purpose", PHASE1_PURPOSE)
            .eq("metadata->>target_id", target_id)
            .gte("created_at", _utc_day_start())
            .execute()
        )
        count = resp.count
        # Coerce INSIDE the try. A non-numeric count (a stubbed client, a
        # PostgREST response without the Content-Range header) must land in
        # the "unreadable" arm, not escape as a TypeError from the caller's
        # comparison — the whole point of this function is that its failure
        # mode is a value the caller chooses how to treat.
        # ``None`` here is a SUCCESSFUL request that shipped no count (no
        # Content-Range header, a stubbed client). That is an unknown, not a
        # zero, and returning 0 would report "nothing spent today" — handing
        # the caller a full allowance at exactly the moment spend is invisible.
        # This is the fail-open the docstring above promises not to impose.
        return None if count is None else int(count)
    except Exception as exc:
        # One line, no stack: this sits inside the poller's per-batch triage
        # loop, and a repeating traceback there is how #652 flooded Railway's
        # log replica cap and cost visibility exactly when it was needed.
        logger.warning(
            "phase1_calls_today: count unreadable for target %s (%s: %s)",
            target_id,
            type(exc).__name__,
            exc,
        )
        return None


async def phase1_cap_reached(
    supabase: AsyncClient, target_id: str, *, cap: int | None = None
) -> bool:
    """True when this target has already spent its Phase-1 calls for the day.

    Fail-OPEN: an unreadable count returns ``False``. See the module
    docstring for why the ingestion path must not stall on a count blip.
    ``cap`` defaults to ``settings.phase1_daily_cap`` resolved at CALL time
    (not import time) so an env flip or a test override takes effect without
    re-importing; ``0`` disables the rail.
    """
    effective = settings.phase1_daily_cap if cap is None else cap
    if effective <= 0:
        return False
    used = await phase1_calls_today(supabase, target_id)
    if used is None:
        return False
    return used >= effective


async def phase1_backfill_allowance(
    supabase: AsyncClient,
    target_id: str,
    *,
    cap: int | None = None,
    fraction: float | None = None,
) -> int | None:
    """How many Phase-1 calls one activation backfill may make right now.

    ``None`` means UNBOUNDED — the cap is disabled (``phase1_daily_cap`` 0),
    which is exactly Phase 1's behaviour before this rail existed. Otherwise
    the answer is ``min(cap - used, floor(cap * fraction))``:

    - ``cap - used`` keeps the backfill inside the SHARED daily count, so a
      day whose intake already spent the cap leaves the backfill nothing.
    - ``floor(cap * fraction)`` is the structural half of the guarantee: no
      matter how early in the day a backfill runs, it cannot take more than
      its share, so ``(1 - fraction)`` of the day's calls stay available for
      fresh listings.

    Fail-CLOSED: an unreadable count returns 0.
    """
    effective_cap = settings.phase1_daily_cap if cap is None else cap
    if effective_cap <= 0:
        return None
    effective_fraction = settings.phase1_backfill_cap_fraction if fraction is None else fraction
    share = int(effective_cap * effective_fraction)
    used = await phase1_calls_today(supabase, target_id)
    if used is None:
        logger.warning(
            "phase1_backfill_allowance: count unreadable for target %s — "
            "refusing to spend (fail-closed)",
            target_id,
        )
        return 0
    return max(0, min(effective_cap - used, share))
