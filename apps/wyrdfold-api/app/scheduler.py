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
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from apscheduler.triggers.date import DateTrigger  # type: ignore[import-untyped]
from apscheduler.triggers.interval import IntervalTrigger  # type: ignore[import-untyped]
from supabase import Client

from app.cache import job_list_cache
from app.config import settings
from app.models.schemas import PollResult
from app.services.ingestion_health import _newest_discovery_at, check_ingestion_health
from app.services.poll_lock import poll_advisory_lock
from app.services.poller import (
    poll_all_sources,
    poll_due_sources,
    resume_phase1_backfills,
)
from app.services.recency import refresh_all_recency_scores
from app.services.retention import purge_expired_records
from app.services.source_discovery import run_discovery_all_targets_locked
from app.services.targets.activation import sweep_stalled_activations
from app.services.url_health import run_url_health_check
from app.supabase_pool import get_async_supabase

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

    The ingestion health check runs on EVERY tick — including ticks that
    could not take the lock. It used to live inside the acquired branch,
    which meant a LEAKED advisory lock (#350: a mid-cycle death leaves the
    lock with PostgREST's pooled backend, not the API process) silenced
    not just polling but every alarm too — no-new-jobs / staleness /
    credit-runway only ran when polling was already healthy. A lock-skipped
    tick now still runs the checks, and the leaked-lock alarm inside them
    pages on exactly that state.

    Errors are logged but never raised; APScheduler would otherwise
    suppress and we'd lose the trace.
    """
    try:
        client = get_async_supabase()
        if client is None:
            logger.warning("scheduled poll skipped — async supabase client not initialized")
            return
        # ``poll_advisory_lock`` is seam-aware (acquires on the pooled async
        # client internally) but is annotated ``Client``; the cast satisfies mypy.
        lock_client = cast(Client, client)
        async with poll_advisory_lock(lock_client, settings.poll_advisory_lock_key) as acquired:
            if not acquired:
                # WARNING, not INFO: prod's log level hides INFO, and this
                # line is the only direct evidence of a leaked advisory lock
                # silently no-oping ALL polling (the ingestion-health alarm
                # is the pager; this makes the state diagnosable from logs).
                logger.warning(
                    "scheduled poll skipped — another poll holds the advisory lock (key=%s)",
                    settings.poll_advisory_lock_key,
                )
            else:
                # Watchdog: cap the cycle's wall-time so a HUNG cycle (a stuck
                # httpx/LLM await during an upstream outage) can't wedge the
                # scheduler — ``max_instances=1`` blocks the next tick while
                # this one is still "running", so without this a hang stops
                # ALL polling until a restart (the 402-storm incident,
                # 2026-07-06). On timeout the cycle is cancelled, the advisory
                # lock unwinds, and the next tick recovers. 0 disables.
                timeout = settings.poll_cycle_timeout_seconds or None
                progress = PollResult(
                    sources_polled=0, new_jobs=0, updated_jobs=0, archived_jobs=0, errors=[]
                )
                try:
                    result = await asyncio.wait_for(
                        poll_due_sources(client, progress=progress), timeout=timeout
                    )
                except TimeoutError:
                    logger.error(
                        "scheduled poll: cycle exceeded the %ds watchdog and was "
                        "aborted so the next tick can recover (a hung cycle "
                        "otherwise wedges the scheduler until a restart); "
                        "partial progress before abort: polled=%d new=%d "
                        "updated=%d archived=%d errors=%d",
                        settings.poll_cycle_timeout_seconds,
                        progress.sources_polled,
                        progress.new_jobs,
                        progress.updated_jobs,
                        progress.archived_jobs,
                        len(progress.errors),
                    )
                else:
                    logger.info(
                        "scheduled poll: polled=%d new=%d updated=%d archived=%d errors=%d",
                        result.sources_polled,
                        result.new_jobs,
                        result.updated_jobs,
                        result.archived_jobs,
                        len(result.errors),
                    )
                finally:
                    # An aborted cycle has still ingested rows for every
                    # source that finished — the list cache must not serve
                    # them stale until TTL. Found live 2026-08-05: every
                    # overnight cycle hit the watchdog, so the invalidate in
                    # the success branch never ran.
                    if progress.sources_polled > 0:
                        job_list_cache.invalidate()
        # Outside the lock on purpose (see docstring); running after our own
        # release also means the leaked-lock check never sees a healthy hold
        # from this very tick.
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

    Self-contained on purpose: it pulls the async service-role singleton via
    ``get_async_supabase()`` (the same pooled client the scheduler uses) rather
    than a request-scoped client, so it keeps running correctly after the
    request that scheduled it has returned.

    Wrapped in try/except so a backgrounded task's exception is logged
    rather than silently swallowed by the event loop.
    """
    try:
        client = get_async_supabase()
        if client is None:
            logger.warning("force poll skipped — async supabase client not initialized")
            return
        # ``poll_advisory_lock`` is seam-aware (acquires on the pooled async
        # client internally) but is annotated ``Client``; the cast satisfies mypy.
        lock_client = cast(Client, client)
        async with poll_advisory_lock(lock_client, settings.poll_advisory_lock_key) as acquired:
            if not acquired:
                # WARNING, not INFO: prod's log level hides INFO, and this is
                # the only direct evidence of a leaked advisory lock silently
                # no-oping the manual trigger (root-caused 2026-08-04: a
                # mid-cycle kill leaves the lock on a PostgREST backend and
                # the skip was invisible in prod logs).
                logger.warning(
                    "force poll: poll already running, skipping (another poll "
                    "holds the advisory lock, key=%s)",
                    settings.poll_advisory_lock_key,
                )
                return
            # Watchdog: same hung-cycle protection as the scheduled tick — a
            # force poll holds the SAME advisory lock, so a hang here also
            # blocks scheduled polls until a restart. 0 disables.
            timeout = settings.poll_cycle_timeout_seconds or None
            progress = PollResult(
                sources_polled=0, new_jobs=0, updated_jobs=0, archived_jobs=0, errors=[]
            )
            try:
                result = await asyncio.wait_for(
                    poll_all_sources(client, progress=progress), timeout=timeout
                )
            except TimeoutError:
                logger.error(
                    "force poll: cycle exceeded the %ds watchdog and was "
                    "aborted (a hung cycle otherwise holds the advisory lock "
                    "and blocks scheduled polls until a restart); partial "
                    "progress before abort: polled=%d new=%d updated=%d "
                    "archived=%d errors=%d",
                    settings.poll_cycle_timeout_seconds,
                    progress.sources_polled,
                    progress.new_jobs,
                    progress.updated_jobs,
                    progress.archived_jobs,
                    len(progress.errors),
                )
            else:
                logger.info(
                    "force poll: polled=%d new=%d updated=%d archived=%d errors=%d",
                    result.sources_polled,
                    result.new_jobs,
                    result.updated_jobs,
                    result.archived_jobs,
                    len(result.errors),
                )
            finally:
                # An aborted force poll has still ingested rows for every
                # source that finished — keep the list cache honest (same
                # rationale as the scheduled tick).
                if progress.sources_polled > 0:
                    job_list_cache.invalidate()
            # Health check piggybacks the locked poll so it can't race a
            # concurrent poll, mirroring the scheduled tick. Runs after an
            # aborted cycle too — a struggling cycle is exactly when the
            # alarms must fire (#350).
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
        client = get_async_supabase()
        if client is None:
            logger.warning("scheduled url_health skipped — async supabase client not initialized")
            return
        await _record_scheduler_run("url_health_check")
        summary = await run_url_health_check(client)
        if summary["archived"] > 0:
            job_list_cache.invalidate()
    except Exception:
        logger.exception("scheduled url_health raised")


async def _run_phase1_backfill_resume() -> None:
    """Tick body — resume Phase-1 backfills that stopped early.

    Same defensive shape as the other sweeps: pull the singleton client, skip
    if uninitialized, never raise (APScheduler would swallow it).

    Why this tick exists: the activation backfill may stop on its own
    allowance, the shared daily cap, the global budget or a provider outage,
    and the rows it did not reach are judged by NOTHING else — ordinary polling
    triages only externally-new listings. Without this, an early stop was
    permanent until a user toggled the target off and on.
    """
    try:
        client = get_async_supabase()
        if client is None:
            logger.warning("phase1 backfill resume skipped — async supabase client not initialized")
            return
        await _record_scheduler_run("phase1_backfill_resume")
        await resume_phase1_backfills(client)
    except Exception:
        logger.exception("phase1 backfill resume tick failed")


async def _run_scheduled_retention_purge() -> None:
    """Tick body — delete operational-log rows past their retention window.

    Same defensive shape as ``_run_scheduled_poll``: pull the singleton
    client, skip if uninitialized, never raise (APScheduler would swallow
    it). The purge runs on the async service-role client (#57 slice 1),
    awaited natively on the event loop — no worker-thread hop.
    """
    try:
        client = get_async_supabase()
        if client is None:
            logger.warning(
                "scheduled retention purge skipped — async supabase client not initialized"
            )
            return
        await _record_scheduler_run("retention_purge")
        report = await purge_expired_records(
            client,
            llm_costs_days=settings.llm_costs_retention_days,
            notifications_sent_days=settings.notifications_sent_retention_days,
            search_events_days=settings.search_events_retention_days,
            phase1_rejections_days=settings.phase1_rejections_retention_days,
        )
        logger.info("scheduled retention purge: %s", report)
    except Exception:
        logger.exception("scheduled retention purge raised")


async def _run_scheduled_activation_sweep() -> None:
    """Tick body — reclaim targets stranded in an in-flight activation state.

    Same defensive shape as the other ticks: pull the singleton client, skip if
    uninitialized, never raise (APScheduler would swallow the trace).
    """
    try:
        client = get_async_supabase()
        if client is None:
            logger.warning(
                "scheduled activation sweep skipped — async supabase client not initialized"
            )
            return
        await _record_scheduler_run("activation_sweep")
        reclaimed = await sweep_stalled_activations(
            client, stale_after_hours=settings.activation_stale_after_hours
        )
        if any(reclaimed.values()):
            logger.info("scheduled activation sweep: %s", reclaimed)
    except Exception:
        logger.exception("scheduled activation sweep raised")


async def _run_scheduled_recency_refresh() -> None:
    """Tick body — rewrite ``recency_score`` for every live score row from the
    current date, then invalidate the list cache.

    The poller only refreshes recency for jobs it re-touched that cycle, so
    postings that age off the boards freeze their decay; this sweep keeps the
    stored sort key current for ALL live rows (see
    ``app/services/recency.refresh_all_recency_scores``). Runs on the async
    service-role client (#57 slice 1), awaited natively on the event loop —
    no worker-thread hop. Same defensive shape as the other ticks: skip if
    the client isn't ready, never raise (APScheduler would swallow the
    trace).
    """
    try:
        client = get_async_supabase()
        if client is None:
            logger.warning(
                "scheduled recency refresh skipped — async supabase client not initialized"
            )
            return
        await _record_scheduler_run("recency_refresh")
        written = await refresh_all_recency_scores(client)
        if written > 0:
            # Cached list pages were sorted by the previous recency_score;
            # drop them so the refreshed ordering surfaces on next load.
            job_list_cache.invalidate()
        logger.info("scheduled recency refresh: rewrote %d score rows", written)
    except Exception:
        logger.exception("scheduled recency refresh raised")


# ---------------------------------------------------------------------------
# scheduler_runs ledger (#327 follow-up): url_health / retention_purge /
# recency_refresh have 12-24h ticks measured from process start, and this
# app deploys near-daily — without a persisted marker they starve exactly
# like discovery did. Discovery anchors to its natural marker
# (source_discoveries); these jobs delete/rewrite rows and leave no trace,
# so they stamp a one-row-per-job ledger at run START (an attempt marker:
# a crashing job waits one tick instead of refiring every boot).
# ---------------------------------------------------------------------------


async def _record_scheduler_run(job_id: str) -> None:
    """Stamp the ledger. Fail-soft: a marker write must never break the job
    (and pre-migration deploys simply log until the table exists)."""
    try:
        client = get_async_supabase()
        if client is None:
            return
        await (
            client.table("scheduler_runs")
            .upsert(
                {
                    "job_id": job_id,
                    "last_run_at": datetime.now(UTC).isoformat(),
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            )
            .execute()
        )
    except Exception:
        logger.warning("scheduler_runs stamp failed for %s (non-fatal)", job_id)


async def _last_scheduler_run(job_id: str) -> datetime | None:
    client = get_async_supabase()
    if client is None:
        return None
    resp = await (
        client.table("scheduler_runs").select("last_run_at").eq("job_id", job_id).limit(1).execute()
    )
    rows = cast("list[dict[str, object]]", resp.data or [])
    if not rows:
        return None
    raw = rows[0].get("last_run_at")
    if not raw:
        return None
    parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def _anchor_job_from_ledger(
    scheduler: AsyncIOScheduler,
    *,
    job_id: str,
    tick_hours: int,
    runner: Callable[[], Awaitable[None]],
    now: datetime | None = None,
) -> None:
    """One-shot catch-up for a ledger-stamped job — same contract as
    ``_anchor_discovery_schedule``: overdue/never → run now (the runner
    stamps the ledger itself); fresh → pull the interval job's next fire up
    to ``last_run + tick``. Fail-soft: any error keeps the boot-relative
    schedule."""
    moment = now or datetime.now(UTC)
    try:
        last = await _last_scheduler_run(job_id)
        tick = timedelta(hours=tick_hours)
        if last is None or moment - last >= tick:
            logger.info(
                "%s catch-up: last persisted run %s is overdue (tick %dh) — running now",
                job_id,
                last.isoformat() if last else "never",
                tick_hours,
            )
            await runner()
        else:
            next_fire = last + tick
            scheduler.modify_job(job_id, next_run_time=next_fire)
            logger.info(
                "%s catch-up: last persisted run %s is fresh — next fire anchored to %s",
                job_id,
                last.isoformat(),
                next_fire.isoformat(),
            )
    except Exception:
        logger.exception("%s catch-up failed; keeping the boot-relative schedule", job_id)


async def _anchor_discovery_schedule(
    scheduler: AsyncIOScheduler, *, now: datetime | None = None
) -> None:
    """One-shot catch-up: re-anchor the discovery interval to the last
    PERSISTED run instead of process start.

    ``IntervalTrigger`` measures from boot, and this app deploys near-daily,
    so the 24h discovery tick effectively never elapsed — discovery ran ONCE
    in its first six enabled days while the #244 staleness alarm correctly
    fired about it (2026-07-13 finding). Overdue (or never ran) → run now;
    the run is advisory-locked and Brave-budget-capped, so restarts cannot
    stampede or blow the free tier. Fresh → pull the interval job's next
    fire up to ``last_run + tick`` (APScheduler chains subsequent fires from
    the overridden one, so the cadence stays anchored to real runs across
    deploys). Fail-soft: any error keeps the boot-relative schedule.
    """
    moment = now or datetime.now(UTC)
    try:
        client = get_async_supabase()
        if client is None:
            logger.warning("discovery catch-up skipped — async supabase client not initialized")
            return
        last = await _newest_discovery_at(client)
        tick = timedelta(hours=settings.discovery_tick_hours)
        if last is None or moment - last >= tick:
            logger.info(
                "discovery catch-up: last persisted run %s is overdue (tick %dh) — running now",
                last.isoformat() if last else "never",
                settings.discovery_tick_hours,
            )
            await run_discovery_all_targets_locked()
        else:
            next_fire = last + tick
            scheduler.modify_job("discovery_run", next_run_time=next_fire)
            logger.info(
                "discovery catch-up: last persisted run %s is fresh — next fire anchored to %s",
                last.isoformat(),
                next_fire.isoformat(),
            )
    except Exception:
        logger.exception("discovery catch-up failed; keeping the boot-relative schedule")


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


# APScheduler's default misfire grace is ~1s, so an interval tick that can't fire
# on time because the loop is busy (a poll gather burst, a JWKS stall) is SILENTLY
# SKIPPED — the job then waits a full extra interval, and the ledger catch-up only
# heals it at next boot (hardening review 2026-07-21, Perf-F2). A grace window lets
# a late tick still run; coalesce=True (set per-job) collapses multiple missed
# ticks into one so the grace can't cause a backlog stampede.
_POLL_MISFIRE_GRACE_S = 300  # 30-min tick — a few minutes late is harmless
_SWEEP_MISFIRE_GRACE_S = 3600  # 12-24h sweeps — an hour of slack, matches the catch-ups


def start_scheduler_if_enabled() -> AsyncIOScheduler | None:
    """Build, start, and return the scheduler, or ``None`` when disabled.

    Called from the FastAPI lifespan; the returned handle is what the
    lifespan must shut down on exit.

    Six independent recurring jobs may run on the same scheduler:
      - ``poll_due_sources`` — gated on ``POLL_SCHEDULER_ENABLED``
      - ``url_health_check`` — gated on ``URL_HEALTH_CHECK_ENABLED``
      - ``retention_purge`` — gated on ``RETENTION_PURGE_ENABLED``
      - ``discovery_run`` — gated on ``DISCOVERY_SCHEDULER_ENABLED``
      - ``recency_refresh`` — gated on ``RECENCY_REFRESH_ENABLED``
      - ``activation_sweep`` — gated on ``ACTIVATION_SWEEP_ENABLED``

    If all flags are off, no scheduler is started. If only some are on,
    only those jobs are registered. Sharing one scheduler avoids multiple
    thread pools competing in the same FastAPI process.
    """
    if not (
        settings.poll_scheduler_enabled
        or settings.url_health_check_enabled
        or settings.retention_purge_enabled
        or settings.discovery_scheduler_enabled
        or settings.recency_refresh_enabled
        or settings.activation_sweep_enabled
    ):
        logger.info(
            "schedulers disabled (set POLL_SCHEDULER_ENABLED=true, "
            "URL_HEALTH_CHECK_ENABLED=true, RETENTION_PURGE_ENABLED=true, "
            "DISCOVERY_SCHEDULER_ENABLED=true, RECENCY_REFRESH_ENABLED=true, "
            "or ACTIVATION_SWEEP_ENABLED=true "
            "to enable)"
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
            misfire_grace_time=_POLL_MISFIRE_GRACE_S,
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
            misfire_grace_time=_SWEEP_MISFIRE_GRACE_S,
        )
        scheduler.add_job(
            _anchor_job_from_ledger,
            DateTrigger(run_date=datetime.now(UTC) + timedelta(minutes=3)),
            kwargs={
                "job_id": "url_health_check",
                "tick_hours": settings.url_health_tick_hours,
                "runner": _run_scheduled_url_health,
            },
            args=[scheduler],
            id="url_health_check_catchup",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
            misfire_grace_time=3600,
        )
        logger.info(
            "url_health scheduler registered (tick every %d h)",
            settings.url_health_tick_hours,
        )

    if settings.phase1_backfill_enabled and settings.phase1_backfill_resume_tick_hours > 0:
        scheduler.add_job(
            _run_phase1_backfill_resume,
            IntervalTrigger(hours=settings.phase1_backfill_resume_tick_hours),
            id="phase1_backfill_resume",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
            misfire_grace_time=_SWEEP_MISFIRE_GRACE_S,
        )
        # Catch-up anchor. MANDATORY for an hour-scale, ledger-stamped job:
        # ``IntervalTrigger`` measures from BOOT, and this app deploys near
        # daily, so a 24h tick would reset its countdown on every deploy and
        # this sweep would essentially never fire — silently reinstating the
        # permanent-partial-backfill bug it exists to fix. The suite's
        # "every ledger-stamped interval job has a catch-up anchor" property
        # test caught exactly that here.
        scheduler.add_job(
            _anchor_job_from_ledger,
            DateTrigger(run_date=datetime.now(UTC) + timedelta(minutes=3)),
            kwargs={
                "job_id": "phase1_backfill_resume",
                "tick_hours": settings.phase1_backfill_resume_tick_hours,
                "runner": _run_phase1_backfill_resume,
            },
            args=[scheduler],
            id="phase1_backfill_resume_catchup",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
            misfire_grace_time=3600,
        )
        logger.info(
            "Phase-1 backfill resume sweep scheduled every %dh",
            settings.phase1_backfill_resume_tick_hours,
        )

    if settings.retention_purge_enabled:
        scheduler.add_job(
            _run_scheduled_retention_purge,
            IntervalTrigger(hours=settings.retention_purge_tick_hours),
            id="retention_purge",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
            misfire_grace_time=_SWEEP_MISFIRE_GRACE_S,
        )
        scheduler.add_job(
            _anchor_job_from_ledger,
            DateTrigger(run_date=datetime.now(UTC) + timedelta(minutes=3)),
            kwargs={
                "job_id": "retention_purge",
                "tick_hours": settings.retention_purge_tick_hours,
                "runner": _run_scheduled_retention_purge,
            },
            args=[scheduler],
            id="retention_purge_catchup",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
            misfire_grace_time=3600,
        )
        logger.info(
            "retention purge scheduler registered (tick every %d h)",
            settings.retention_purge_tick_hours,
        )

    if settings.activation_sweep_enabled:
        scheduler.add_job(
            _run_scheduled_activation_sweep,
            IntervalTrigger(hours=settings.activation_sweep_tick_hours),
            id="activation_sweep",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
            misfire_grace_time=_SWEEP_MISFIRE_GRACE_S,
        )
        # Same catch-up the other ledger-stamped jobs carry, and for the same
        # reason: `IntervalTrigger` counts from PROCESS START, and this app
        # deploys near-daily — every deploy restarts the countdown, so a tick
        # longer than the deploy cadence can go indefinitely without firing.
        # That already happened once (discovery ran ONCE in its first 6 enabled
        # days, #244). Shipping this sweep without the anchor reproduced the
        # bug: two redeploys on release day each reset its 6h timer.
        #
        # The anchor also makes the sweep verifiable — on the first boot after
        # this lands, `activation_sweep` has never been stamped, so it reads as
        # overdue and runs immediately, writing the ledger row that proves it.
        scheduler.add_job(
            _anchor_job_from_ledger,
            DateTrigger(run_date=datetime.now(UTC) + timedelta(minutes=3)),
            kwargs={
                "job_id": "activation_sweep",
                "tick_hours": settings.activation_sweep_tick_hours,
                "runner": _run_scheduled_activation_sweep,
            },
            args=[scheduler],
            id="activation_sweep_catchup",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
            misfire_grace_time=3600,
        )
        logger.info(
            "activation sweep scheduler registered (tick every %d h, stale after %d h, "
            "catch-up in 3m)",
            settings.activation_sweep_tick_hours,
            settings.activation_stale_after_hours,
        )

    if settings.discovery_scheduler_enabled:
        scheduler.add_job(
            run_discovery_all_targets_locked,
            IntervalTrigger(hours=settings.discovery_tick_hours),
            id="discovery_run",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
            misfire_grace_time=_SWEEP_MISFIRE_GRACE_S,
        )
        # The interval above measures from PROCESS START, and this app deploys
        # near-daily — so a 24h tick effectively never elapsed (discovery ran
        # once in its first 6 enabled days while the #244 staleness alarm
        # fired about it). One-shot catch-up shortly after boot re-anchors the
        # schedule to the last PERSISTED run; the 3-minute delay lets the app
        # settle and means a crash-looping process never reaches Brave at all.
        scheduler.add_job(
            _anchor_discovery_schedule,
            DateTrigger(run_date=datetime.now(UTC) + timedelta(minutes=3)),
            args=[scheduler],
            id="discovery_catchup",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
            # A catch-up must run whenever it CAN, not only exactly on time:
            # APScheduler's ~1s default misfire grace silently SKIPS the job
            # if the loop wakes late (observed 62s late on a busy boot — the
            # local smoke caught it). An hour of grace keeps the one-shot
            # alive through any realistic startup contention.
            misfire_grace_time=3600,
        )
        logger.info(
            "discovery scheduler registered (tick every %d h, catch-up in 3m)",
            settings.discovery_tick_hours,
        )

    if settings.recency_refresh_enabled:
        scheduler.add_job(
            _run_scheduled_recency_refresh,
            IntervalTrigger(hours=settings.recency_refresh_tick_hours),
            id="recency_refresh",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
            misfire_grace_time=_SWEEP_MISFIRE_GRACE_S,
        )
        scheduler.add_job(
            _anchor_job_from_ledger,
            DateTrigger(run_date=datetime.now(UTC) + timedelta(minutes=3)),
            kwargs={
                "job_id": "recency_refresh",
                "tick_hours": settings.recency_refresh_tick_hours,
                "runner": _run_scheduled_recency_refresh,
            },
            args=[scheduler],
            id="recency_refresh_catchup",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
            misfire_grace_time=3600,
        )
        logger.info(
            "recency refresh scheduler registered (tick every %d h)",
            settings.recency_refresh_tick_hours,
        )

    scheduler.start()
    return scheduler
