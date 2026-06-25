"""Append-only writer for the pre-scan disagreement matrix (#60/#68, Phase 3).

One coroutine, :func:`record_shadow_observation`, inserts a single
``prescan_shadow`` row capturing BOTH decisions for one (job, target) the poller
just scored: the LIVE keyword admit decision (which actually drove admission) and
the would-be cosine gate decision (observed only). The poller calls this behind
``settings.prescan_shadow_enabled``; the cosine side comes from
``prescan_gate.cosine_gate_decision`` (NULL when the Phase-1/2 vectors aren't
populated yet).

Fail-soft: the insert is wrapped so a DB hiccup can never break polling — a
dropped observation just means one missing matrix row, never a failed poll. This
table changes no behavior; it exists purely to size the keyword↔cosine
disagreement before any gate flip.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from supabase import Client

logger = logging.getLogger(__name__)

TABLE = "prescan_shadow"


async def record_shadow_observation(
    supabase: Client,
    *,
    job_id: str,
    target_id: str,
    keyword_admit: bool | None,
    keyword_score: int | None,
    cosine: float | None,
    cosine_admit: bool | None,
    threshold: float | None,
) -> None:
    """Append one shadow observation row (best-effort, never raises).

    ``keyword_admit`` / ``keyword_score`` are the live decision in force;
    ``cosine`` / ``cosine_admit`` / ``threshold`` are the observed-only cosine
    gate decision (any may be None — the gate had no opinion). ``id`` and
    ``observed_at`` are filled by the table defaults.
    """
    row: dict[str, Any] = {
        "job_posting_id": job_id,
        "target_id": target_id,
        "keyword_admit": keyword_admit,
        "keyword_score": keyword_score,
        "cosine": cosine,
        "cosine_admit": cosine_admit,
        "threshold": threshold,
    }
    try:
        await asyncio.to_thread(
            lambda: supabase.table(TABLE).insert(row).execute()
        )
    except Exception:
        logger.exception(
            "Pre-scan shadow observation write failed for job %s / target %s",
            job_id,
            target_id,
        )
