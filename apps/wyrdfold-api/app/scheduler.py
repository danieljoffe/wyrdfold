"""In-process job scheduler for periodic polling.

Wraps APScheduler's ``AsyncIOScheduler`` so the FastAPI lifespan can
start/stop a single recurring job that calls ``poll_due_sources``.

The scheduler is **off by default** — opt-in via the
``POLL_SCHEDULER_ENABLED`` env var. Tests should leave it disabled so
the lifespan stays deterministic; ops enable it on the production
process. Any external cron driver (pg_cron, GitHub Actions) can call
``POST /poll/due`` instead and reach the same code path.

Single-poll safety: the scheduled poll is the PRIMARY ingestion trigger
in production (no longer dependent on the Vercel cron). To stay safe with
multiple replicas — or with the legacy Vercel cron still firing — each
scheduled poll runs inside a Postgres advisory lock (see
``app/services/poll_lock.py``): only one holder polls per tick, everyone
else skips cleanly. Job upserts are idempotent regardless, so the lock is
defense-in-depth, not the sole guarantee. APScheduler's ``max_instances=1``
+ ``coalesce=True`` still guard against overlapping ticks within ONE
process; the advisory lock extends that across processes.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from apscheduler.triggers.interval import IntervalTrigger  # type: ignore[import-untyped]

from app.cache import job_list_cache
from app.config import settings
from app.services.ingestion_health import check_ingestion_health
from app.services.poll_lock import poll_advisory_lock
from app.services.poller import poll_all_sources, poll_due_sources
from app.services.retention import purge_expired_records
from app.services.source_discovery import run_discovery_all_targets_locked
from app.services.url_health import run_url_health_check
from app.supabase_pool import get_supabase_pool

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


async def _run_scheduled_poll() -> None:
    """Tick body — fetch due sources, run the ingestion health check, and
    invalidate the list cache.

    Guarded by a Postgres advisory lock so only one poll runs at a time
    across every replica (and alongside the legacy Vercel cron). A tick
    that can't get the lock skips cleanly — another holder is already
    polling.

    The ingestion health check runs every tick that acquires the lock
    (cheap: two count queries + a 1-row read) so the "no new jobs in N
    days" / mass-disable alerts fire even when the poll itself produces
    nothing.

    Errors are logged but never raised; APScheduler would otherwise
    suppress and we'd lose the trace.
    """
    try:
        client = get_supabase_pool()
        if client is None:
            logger.warning("scheduled poll skipped — supabase client not initialized")
            return
        async with poll_advisory_lock(client, settings.poll_advisory_lock_key) as acquired:
            if not acquired:
                logger.info(
                    "scheduled poll skipped — another poll holds the advisory lock (key=%s)",
                    settings.poll_advisory_lock_key,
                )
                return
            result = await poll_due_sources(client)
            if result.sources_polled > 0:
                job_list_cache.invalidate()
            logger.info(
                "scheduled poll: polled=%d new=%d updated=%d archived=%d errors=%d",
                result.sources_polled,
                result.new_jobs,
                result.updated_jobs,
                result.archived_jobs,
                len(result.errors),
            )
            # Health check piggybacks the locked poll tick so it can't
            # race a concurrent poll and so it only runs on the replica
            # actually driving ingestion.
            await check_ingestion_health(client)
    except Exception:
        logger.exception("scheduled poll raised")


async def run_force_poll_locked() -> None:
    """Background body for the manual force-poll (``POST /poll``).

    Force-polls EVERY enabled source (ignores ``poll_interval_minutes``),
    the manual hammer the ``/poll`` route used to ``await`` inline. It now
    runs detached so the route can return ``202`` immediately instead of
    holding the request open for the multi-minute poll (which tripped the
    edge's 300s timeout).

    Routed through the SAME Postgres advisory lock as ``_run_scheduled_poll``
    (``settings.poll_advisory_lock_key``), so a manual trigger and the
    scheduled due-poll can never run concurrently: whichever gets the lock
    polls, the other logs "poll already running, skipping" and exits cleanly.

    Self-contained on purpose: it pulls the service-role singleton via
    ``get_supabase_pool()`` (the same client the scheduler uses) rather than
    a request-scoped client, so it keeps running correctly after the request
    that scheduled it has returned.

    Wrapped in try/except so a backgrounded task's exception is logged
    rather than silently swallowed by the event loop.
    """
    try:
        client = get_supabase_pool()
        if client is None:
            logger.warning("force poll skipped — supabase client not initialized")
            return
        async with poll_advisory_lock(client, settings.poll_advisory_lock_key) as acquired:
            if not acquired:
                logger.info(
                    "force poll: poll already running, skipping (another poll "
                    "holds the advisory lock, key=%s)",
                    settings.poll_advisory_lock_key,
                )
                return
            result = await poll_all_sources(client)
            job_list_cache.invalidate()
            logger.info(
                "force poll: polled=%d new=%d updated=%d archived=%d errors=%d",
                result.sources_polled,
                result.new_jobs,
                result.updated_jobs,
                result.archived_jobs,
                len(result.errors),
            )
            # Health check piggybacks the locked poll so it can't race a
            # concurrent poll, mirroring the scheduled tick.
            await check_ingestion_health(client)
    except Exception:
        logger.exception("force poll raised")


async def _run_scheduled_url_health() -> None:
    """Tick body — HEAD-check the oldest batch of job URLs and archive dead ones.

    See ``app/services/url_health.py``. Errors are logged but never raised
    (same pattern as ``_run_scheduled_poll``). Invalidates the list cache
    when jobs were archived this tick so users see the updated state on
    next page load.
    """
    try:
        client = get_supabase_pool()
        if client is None:
            logger.warning("scheduled url_health skipped — supabase client not initialized")
            return
        summary = await run_url_health_check(client)
        if summary["archived"] > 0:
            job_list_cache.invalidate()
    except Exception:
        logger.exception("scheduled url_health raised")


async def _run_scheduled_retention_purge() -> None:
    """Tick body — delete operational-log rows past their retention window.

    Same defensive shape as ``_run_scheduled_poll``: pull the singleton
    client, skip if uninitialized, never raise (APScheduler would swallow
    it). The purge is synchronous (service-role supabase client), so it
    runs in a worker thread to keep the event loop free.
    """
    try:
        client = get_supabase_pool()
        if client is None:
            logger.warning("scheduled retention purge skipped — supabase client not initialized")
            return
        report = await asyncio.to_thread(
            purge_expired_records,
            client,
            llm_costs_days=settings.llm_costs_retention_days,
            notifications_sent_days=settings.notifications_sent_retention_days,
        )
        logger.info("scheduled retention purge: %s", report)
    except Exception:
        logger.exception("scheduled retention purge raised")


def build_scheduler(
    *, tick_minutes: int, job_func: Callable[[], Awaitable[None]] = _run_scheduled_poll
) -> AsyncIOScheduler:
    """Construct a configured but unstarted scheduler.

    ``coalesce=True`` collapses missed ticks into one (e.g. if the
    process was paused). ``max_instances=1`` prevents two ticks from
    running concurrently — the poll itself is async-concurrent, so a
    second tick mid-fetch would just add contention.
    """
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        job_func,
        IntervalTrigger(minutes=tick_minutes),
        id="poll_due_sources",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    return scheduler


def start_scheduler_if_enabled() -> AsyncIOScheduler | None:
    """Build, start, and return the scheduler, or ``None`` when disabled.

    Called from the FastAPI lifespan; the returned handle is what the
    lifespan must shut down on exit.

    Four independent recurring jobs may run on the same scheduler:
      - ``poll_due_sources`` — gated on ``POLL_SCHEDULER_ENABLED``
      - ``url_health_check`` — gated on ``URL_HEALTH_CHECK_ENABLED``
      - ``retention_purge`` — gated on ``RETENTION_PURGE_ENABLED``
      - ``discovery_run`` — gated on ``DISCOVERY_SCHEDULER_ENABLED``

    If all flags are off, no scheduler is started. If only some are on,
    only those jobs are registered. Sharing one scheduler avoids multiple
    thread pools competing in the same FastAPI process.
    """
    if not (
        settings.poll_scheduler_enabled
        or settings.url_health_check_enabled
        or settings.retention_purge_enabled
        or settings.discovery_scheduler_enabled
    ):
        logger.info(
            "schedulers disabled (set POLL_SCHEDULER_ENABLED=true, "
            "URL_HEALTH_CHECK_ENABLED=true, RETENTION_PURGE_ENABLED=true, or "
            "DISCOVERY_SCHEDULER_ENABLED=true to enable)"
        )
        return None

    scheduler = AsyncIOScheduler()

    if settings.poll_scheduler_enabled:
        scheduler.add_job(
            _run_scheduled_poll,
            IntervalTrigger(minutes=settings.poll_tick_minutes),
            id="poll_due_sources",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        logger.info(
            "poll scheduler registered (tick every %d min)",
            settings.poll_tick_minutes,
        )

    if settings.url_health_check_enabled:
        scheduler.add_job(
            _run_scheduled_url_health,
            IntervalTrigger(hours=settings.url_health_tick_hours),
            id="url_health_check",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        logger.info(
            "url_health scheduler registered (tick every %d h)",
            settings.url_health_tick_hours,
        )

    if settings.retention_purge_enabled:
        scheduler.add_job(
            _run_scheduled_retention_purge,
            IntervalTrigger(hours=settings.retention_purge_tick_hours),
            id="retention_purge",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        logger.info(
            "retention purge scheduler registered (tick every %d h)",
            settings.retention_purge_tick_hours,
        )

    if settings.discovery_scheduler_enabled:
        scheduler.add_job(
            run_discovery_all_targets_locked,
            IntervalTrigger(hours=settings.discovery_tick_hours),
            id="discovery_run",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        logger.info(
            "discovery scheduler registered (tick every %d h)",
            settings.discovery_tick_hours,
        )

    scheduler.start()
    return scheduler
