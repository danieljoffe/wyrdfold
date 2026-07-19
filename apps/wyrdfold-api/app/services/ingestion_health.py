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
  3. **Discovery stalled** — ``max(source_discoveries.discovered_at)`` older
     than ``discovery_max_age_hours``, but ONLY when discovery is meant to be
     running (``discovery_scheduler_enabled``). The same silent-freeze class
     one layer up: discovery quietly stopped producing sources and the
     catalog rotted for weeks (#60) before anyone noticed.
  4. **LLM credit runway** — the OpenRouter key's remaining balance vs the
     trailing spend rate; exhaustion 402s every grading stage silently
     (three drains before this check existed).
  5. **Leaked advisory lock** (#350) — a poll/discovery lock currently held
     while the work it serializes is provably stale. The locks are
     session-level and live on PostgREST's pooled backend, so an API death
     mid-cycle leaks them and every later cycle "skips cleanly" (an INFO
     log, invisible on prod). Postgres stores no lock-acquisition time, so
     held-ness is paired with behavioral staleness instead.

All are best-effort and never raise into the caller: a health-check
query failure must not crash a poll cycle.

The DB reads route through the #57 seam (:func:`app.services.db_write.
poll_db_read`), so the health pass rides the pooled async client when
``POLLER_ASYNC_DB`` is on and falls back to sync-in-thread otherwise.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

import httpx
from supabase import Client

from app.config import settings
from app.services.db_write import poll_db_read
from app.services.llm import cost_log

logger = logging.getLogger(__name__)

# OpenRouter credit endpoints. ``/v1/key`` describes the calling key
# (including ``limit_remaining`` when a per-key limit is set); ``/v1/credits``
# is the account-level balance for keys without their own limit.
_OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
_OPENROUTER_CREDITS_URL = "https://openrouter.ai/api/v1/credits"


def _capture_alert(message: str, *, level: Literal["error", "warning", "info"] = "error") -> None:
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
    newest_discovery_at: datetime | None = None
    stale_discovery: bool = False
    credit_remaining_usd: float | None = None
    credit_runway_days: float | None = None
    low_credit: bool = False
    held_advisory_locks: list[int] = field(default_factory=list)
    stale_poll_lock: bool = False
    stale_discovery_lock: bool = False
    alerts: list[str] = field(default_factory=list)


async def _newest_job_created_at(supabase: Client) -> datetime | None:
    """``max(jobs.created_at)`` via a 1-row keyset read on the existing
    ``created_at DESC`` index — cheaper than an aggregate scan."""
    resp = await poll_db_read(
        supabase,
        lambda c: c.table("jobs").select("created_at").order("created_at", desc=True).limit(1),
        label="health newest-job read",
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
    # ``head=True`` → PostgREST issues a HEAD, returning only the
    # Content-Range count and *zero* rows. Without it, ``count="exact"``
    # ships every matching ``id`` row in the body just to read one integer
    # from the header — and the ``sources`` catalog grows with every
    # discovered company (722 discoveries across 6 targets in project
    # history), so each health tick would transfer thousands of rows twice.
    # We read only ``.count`` below, never ``.data``, so this is a pure win.
    total_resp = await poll_db_read(
        supabase,
        lambda c: c.table("sources").select("id", count="exact", head=True),
        label="health sources count",
    )
    disabled_resp = await poll_db_read(
        supabase,
        lambda c: c.table("sources").select("id", count="exact", head=True).eq("enabled", False),
        label="health disabled-sources count",
    )
    return int(total_resp.count or 0), int(disabled_resp.count or 0)


async def _newest_discovery_at(supabase: Client) -> datetime | None:
    """``max(source_discoveries.discovered_at)`` via a 1-row keyset read on
    the ``discovered_at DESC`` index — the timestamp of the most recent
    discovery attempt of any outcome (inserted / duplicate / filtered)."""
    resp = await poll_db_read(
        supabase,
        lambda c: (
            c.table("source_discoveries")
            .select("discovered_at")
            .order("discovered_at", desc=True)
            .limit(1)
        ),
        label="health newest-discovery read",
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    if not rows:
        return None
    raw = rows[0].get("discovered_at")
    if not raw:
        return None
    parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


async def _newest_source_polled_at(supabase: Client) -> datetime | None:
    """``max(sources.last_polled_at)`` via a 1-row keyset read — the poll
    cycle's behavioral freshness stamp (every polled source updates it, so it
    advances continuously while any polling happens at all). ``nullsfirst=
    False`` matters: PostgREST's DESC default puts NULLs first, and
    never-polled sources would otherwise mask the real newest stamp."""
    resp = await poll_db_read(
        supabase,
        lambda c: (
            c.table("sources")
            .select("last_polled_at")
            .order("last_polled_at", desc=True, nullsfirst=False)
            .limit(1)
        ),
        label="health newest-poll-stamp read",
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    if not rows:
        return None
    raw = rows[0].get("last_polled_at")
    if not raw:
        return None
    parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


async def _held_advisory_locks(supabase: Client) -> dict[int, dict[str, Any]]:
    """Granted session advisory locks keyed by their bigint lock key (#350).

    Reads the ``advisory_lock_info`` SECURITY DEFINER RPC (service-role
    only); the RPC reassembles pg_locks' classid/objid split back into the
    bigint the app locked with. Values carry holder diagnostics
    (``pid`` / ``backend_start`` / ``application_name``) for the alarm text.
    """
    resp = await poll_db_read(
        supabase,
        lambda c: c.rpc("advisory_lock_info", {}),
        label="health advisory-locks read",
    )
    held: dict[int, dict[str, Any]] = {}
    for row in cast(list[dict[str, Any]], resp.data or []):
        if row.get("granted") and row.get("lock_key") is not None:
            held[int(row["lock_key"])] = row
    return held


async def _openrouter_remaining_usd(
    client: httpx.AsyncClient | None = None,
) -> float | None:
    """Remaining spendable USD on the operator's OpenRouter key.

    Prefers the key's own budget (``/v1/key`` → ``limit_remaining``); a key
    with no per-key limit falls back to the account balance (``/v1/credits``
    → total_credits - total_usage). Returns None when neither is available.
    ``client`` is an injection seam for tests (httpx.MockTransport); prod
    callers let it default.
    """
    headers = {"Authorization": f"Bearer {settings.openrouter_api_key}"}
    owned = client is None
    http = client or httpx.AsyncClient(timeout=10.0)
    try:
        resp = await http.get(_OPENROUTER_KEY_URL, headers=headers)
        resp.raise_for_status()
        key_data = (resp.json() or {}).get("data") or {}
        remaining = key_data.get("limit_remaining")
        if remaining is not None:
            return float(remaining)
        resp = await http.get(_OPENROUTER_CREDITS_URL, headers=headers)
        resp.raise_for_status()
        credit_data = (resp.json() or {}).get("data") or {}
        credits, usage = credit_data.get("total_credits"), credit_data.get("total_usage")
        if credits is None or usage is None:
            return None
        return float(credits) - float(usage)
    finally:
        if owned:
            await http.aclose()


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

    # --- 3. Discovery stalled ----------------------------------------------
    # Armed ONLY when discovery is meant to be running, so a deliberately-off
    # discovery loop (the default posture) never pages. When on, this catches
    # the silent freeze where discovery stops producing sources and the
    # catalog rots (#60). The effective threshold floors at 2x the tick so a
    # single missed run never false-alarms and a longer tick auto-relaxes it.
    if settings.discovery_scheduler_enabled and settings.discovery_max_age_hours > 0:
        threshold_h = max(settings.discovery_max_age_hours, settings.discovery_tick_hours * 2)
        try:
            newest_disc = await _newest_discovery_at(supabase)
            report.newest_discovery_at = newest_disc
            cutoff = moment - timedelta(hours=threshold_h)
            if newest_disc is None:
                report.stale_discovery = True
                msg = (
                    "ingestion health: discovery is enabled but has NEVER run — "
                    "source_discoveries is empty; the source catalog will not "
                    "grow (check BRAVE_SEARCH_API_KEY and the scheduler)."
                )
                report.alerts.append(msg)
                logger.warning(msg)
                _capture_alert(msg, level="warning")
            elif newest_disc < cutoff:
                report.stale_discovery = True
                age_h = (moment - newest_disc).total_seconds() / 3600.0
                msg = (
                    f"ingestion health: no discovery run in {age_h:.1f}h "
                    f"(threshold {threshold_h}h) — newest source_discoveries."
                    f"discovered_at {newest_disc.isoformat()}. Discovery may be "
                    "stalled (Brave quota exhausted, key unset, or scheduler down)."
                )
                report.alerts.append(msg)
                logger.warning(msg)
                _capture_alert(msg, level="warning")
        except Exception:
            logger.exception("ingestion health: discovery-staleness check failed")

    # --- 4. LLM credit runway ------------------------------------------------
    # Armed only when the operator's OpenRouter key pays for LLM calls. Three
    # credit drains (2026-06-25, 2026-07-04, 2026-07-13) were each discovered
    # only after grading silently died — the budget caps bound the burn rate,
    # but only a balance probe sees exhaustion COMING. Two rules: an absolute
    # USD floor (catches the already-starved case where the run rate reads
    # ~$0/day), and a runway rule (remaining ÷ trailing 7-day daily spend).
    if (
        settings.llm_provider == "openrouter"
        and settings.openrouter_api_key
        and (settings.llm_credit_min_runway_days > 0 or settings.llm_credit_min_remaining_usd > 0)
    ):
        try:
            remaining = await _openrouter_remaining_usd()
            report.credit_remaining_usd = remaining
            if remaining is not None:
                week_spend = await asyncio.to_thread(
                    cost_log.total_spend_all, supabase, moment - timedelta(days=7)
                )
                daily_rate = week_spend / 7.0
                runway_days = remaining / daily_rate if daily_rate > 0 else None
                report.credit_runway_days = runway_days
                below_floor = (
                    settings.llm_credit_min_remaining_usd > 0
                    and remaining < settings.llm_credit_min_remaining_usd
                )
                below_runway = (
                    settings.llm_credit_min_runway_days > 0
                    and runway_days is not None
                    and runway_days < settings.llm_credit_min_runway_days
                )
                if below_floor or below_runway:
                    report.low_credit = True
                    runway_txt = f"~{runway_days:.1f} day(s)" if runway_days is not None else "n/a"
                    msg = (
                        f"ingestion health: LLM credit runway low — "
                        f"${remaining:.2f} remaining on the OpenRouter key "
                        f"({runway_txt} at the trailing ${daily_rate:.2f}/day). "
                        f"Top up before the pipeline 402s: "
                        f"https://openrouter.ai/settings/credits"
                    )
                    report.alerts.append(msg)
                    logger.error(msg)
                    _capture_alert(msg, level="error")
        except Exception:
            logger.exception("ingestion health: LLM credit probe failed")

    # --- 5. Leaked advisory lock (#350) --------------------------------------
    # A held lock is only a problem when the work it serializes is stale:
    # a healthy in-flight cycle stamps sources/discoveries continuously, so
    # fresh stamps + held lock = someone is legitimately mid-run. The
    # scheduler calls this AFTER releasing its own lock, so a healthy tick
    # never trips over its own hold. Runs even when polling is disabled —
    # a lock leaked by a manual force-poll still blocks the next one.
    stale_after_min = settings.advisory_lock_stale_after_minutes
    if stale_after_min > 0:
        try:
            held = await _held_advisory_locks(supabase)
            report.held_advisory_locks = sorted(held)
            cutoff = moment - timedelta(minutes=stale_after_min)
            runbook = (
                "Clear with: select pg_terminate_backend(pid) from pg_locks "
                "where locktype='advisory'; (#350)"
            )

            poll_lock_row = held.get(settings.poll_advisory_lock_key)
            if poll_lock_row is not None:
                newest_poll = await _newest_source_polled_at(supabase)
                if newest_poll is None or newest_poll < cutoff:
                    report.stale_poll_lock = True
                    last_txt = newest_poll.isoformat() if newest_poll else "never"
                    msg = (
                        f"ingestion health: the POLL advisory lock "
                        f"(key {settings.poll_advisory_lock_key}) is held by backend "
                        f"pid={poll_lock_row.get('pid')} "
                        f"(backend_start {poll_lock_row.get('backend_start')}) while the "
                        f"newest sources.last_polled_at is {last_txt} "
                        f"(threshold {stale_after_min}min) — likely LEAKED by a "
                        f"mid-cycle death; every poll is silently skipping. {runbook}"
                    )
                    report.alerts.append(msg)
                    logger.error(msg)
                    _capture_alert(msg, level="error")

            disc_lock_row = held.get(settings.discovery_advisory_lock_key)
            if disc_lock_row is not None:
                newest_disc = report.newest_discovery_at or await _newest_discovery_at(supabase)
                if newest_disc is None or newest_disc < cutoff:
                    report.stale_discovery_lock = True
                    last_txt = newest_disc.isoformat() if newest_disc else "never"
                    msg = (
                        f"ingestion health: the DISCOVERY advisory lock "
                        f"(key {settings.discovery_advisory_lock_key}) is held by backend "
                        f"pid={disc_lock_row.get('pid')} "
                        f"(backend_start {disc_lock_row.get('backend_start')}) while the "
                        f"newest source_discoveries.discovered_at is {last_txt} "
                        f"(threshold {stale_after_min}min) — likely LEAKED by a "
                        f"mid-run death; discovery runs are silently skipping. {runbook}"
                    )
                    report.alerts.append(msg)
                    logger.warning(msg)
                    _capture_alert(msg, level="warning")
        except Exception:
            logger.exception("ingestion health: advisory-lock check failed")

    if not report.alerts:
        logger.info(
            "ingestion health OK: newest_job_at=%s sources=%d disabled=%d "
            "newest_discovery_at=%s credit_remaining=%s",
            report.newest_job_at.isoformat() if report.newest_job_at else "none",
            report.total_sources,
            report.disabled_sources,
            report.newest_discovery_at.isoformat() if report.newest_discovery_at else "n/a",
            (
                f"${report.credit_remaining_usd:.2f}"
                if report.credit_remaining_usd is not None
                else "n/a"
            ),
        )
    return report
