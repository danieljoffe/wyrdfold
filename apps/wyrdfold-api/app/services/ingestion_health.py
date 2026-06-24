"""Ingestion health checks + alerting.

The Sept-2026 outage went unnoticed for 10+ days because nothing watched
the one symptom that mattered: jobs stopped being ingested. These checks
run from the poll cycle and raise a Sentry alert (``capture_message``)
when ingestion looks dead, so an operator finds out in hours, not weeks:

  1. **No new jobs in N hours** (highest value) — ``max(jobs.created_at)``
     older than ``ingestion_max_job_age_hours``. This is the exact symptom
     that was invisible.
  2. **Mass source disable** — a majority (configurable fraction) of all
     sources currently disabled, i.e. the whole fleet backed off at once.

Both are best-effort and never raise into the caller: a health-check
query failure must not crash a poll cycle.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from supabase import Client

from app.config import settings

logger = logging.getLogger(__name__)


def _capture_alert(
    message: str, *, level: Literal["error", "warning", "info"] = "error"
) -> None:
    """Emit a Sentry alert. No-op when Sentry isn't configured; the log
    line is always written so the signal exists even without Sentry."""
    if not settings.sentry_dsn:
        return
    try:
        import sentry_sdk

        sentry_sdk.capture_message(message, level=level)
    except Exception:
        logger.exception("Failed to report ingestion-health alert to Sentry")


@dataclass
class IngestionHealthReport:
    """Outcome of a health-check pass. ``alerts`` lists the problems found
    (empty == healthy). Returned so the scheduler can log/act on it and so
    tests can assert without inspecting Sentry."""

    newest_job_at: datetime | None = None
    stale_job_data: bool = False
    total_sources: int = 0
    disabled_sources: int = 0
    mass_disable: bool = False
    alerts: list[str] = field(default_factory=list)


async def _newest_job_created_at(supabase: Client) -> datetime | None:
    """``max(jobs.created_at)`` via a 1-row keyset read on the existing
    ``created_at DESC`` index — cheaper than an aggregate scan."""
    resp = await asyncio.to_thread(
        lambda: (
            supabase.table("jobs")
            .select("created_at")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    if not rows:
        return None
    raw = rows[0].get("created_at")
    if not raw:
        return None
    # PostgREST returns ISO-8601; normalise the trailing Z for fromisoformat.
    parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


async def _source_counts(supabase: Client) -> tuple[int, int]:
    """Returns ``(total_sources, disabled_sources)``."""
    total_resp = await asyncio.to_thread(
        lambda: (
            supabase.table("sources")
            .select("id", count="exact")  # type: ignore[arg-type]
            .execute()
        )
    )
    disabled_resp = await asyncio.to_thread(
        lambda: (
            supabase.table("sources")
            .select("id", count="exact")  # type: ignore[arg-type]
            .eq("enabled", False)
            .execute()
        )
    )
    return int(total_resp.count or 0), int(disabled_resp.count or 0)


async def check_ingestion_health(
    supabase: Client, *, now: datetime | None = None
) -> IngestionHealthReport:
    """Run the health checks and alert on anything unhealthy.

    Never raises — on a query error it logs and returns whatever it
    gathered (so a partial failure still surfaces what it could). Honours
    the disable switches in ``settings``.
    """
    moment = now or datetime.now(UTC)
    report = IngestionHealthReport()

    if not settings.ingestion_health_check_enabled:
        return report

    # --- 1. No new jobs in N hours -----------------------------------------
    max_age_hours = settings.ingestion_max_job_age_hours
    if max_age_hours > 0:
        try:
            newest = await _newest_job_created_at(supabase)
            report.newest_job_at = newest
            cutoff = moment - timedelta(hours=max_age_hours)
            if newest is None:
                report.stale_job_data = True
                msg = (
                    "ingestion health: NO jobs in the database at all — "
                    "ingestion has produced nothing."
                )
                report.alerts.append(msg)
                logger.error(msg)
                _capture_alert(msg, level="error")
            elif newest < cutoff:
                report.stale_job_data = True
                age_h = (moment - newest).total_seconds() / 3600.0
                msg = (
                    f"ingestion health: no new jobs in {age_h:.1f}h "
                    f"(threshold {max_age_hours}h) — newest job created_at "
                    f"{newest.isoformat()}. Ingestion may be stalled."
                )
                report.alerts.append(msg)
                logger.error(msg)
                _capture_alert(msg, level="error")
        except Exception:
            logger.exception("ingestion health: newest-job check failed")

    # --- 2. Majority of sources disabled -----------------------------------
    ratio = settings.ingestion_mass_disable_ratio
    if ratio > 0:
        try:
            total, disabled = await _source_counts(supabase)
            report.total_sources = total
            report.disabled_sources = disabled
            if total > 0 and disabled / total >= ratio:
                report.mass_disable = True
                msg = (
                    f"ingestion health: {disabled}/{total} sources disabled "
                    f"({disabled / total:.0%} >= {ratio:.0%} threshold) — the "
                    f"source fleet may have backed off en masse."
                )
                report.alerts.append(msg)
                logger.error(msg)
                _capture_alert(msg, level="error")
        except Exception:
            logger.exception("ingestion health: source-count check failed")

    if not report.alerts:
        logger.info(
            "ingestion health OK: newest_job_at=%s sources=%d disabled=%d",
            report.newest_job_at.isoformat() if report.newest_job_at else "none",
            report.total_sources,
            report.disabled_sources,
        )
    return report
