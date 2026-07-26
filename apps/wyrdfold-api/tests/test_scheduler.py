"""Tests for the in-process APScheduler wiring."""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from app.config import settings
from app.scheduler import (
    _anchor_discovery_schedule,
    _anchor_job_from_ledger,
    _last_scheduler_run,
    _record_scheduler_run,
    build_scheduler,
    start_scheduler_if_enabled,
)
from app.services.source_discovery import run_discovery_all_targets_locked


def _patch_lock_acquired() -> contextlib.AbstractContextManager[object]:
    """Patch the scheduler's advisory lock to always grant (acquired=True),
    so a poll-body test doesn't need a real ``.rpc()`` on its client.
    Dedicated lock semantics live in test_poll_lock.py."""

    @contextlib.asynccontextmanager
    async def _granted(*_a: object, **_k: object) -> AsyncIterator[bool]:
        yield True

    return patch("app.scheduler.poll_advisory_lock", _granted)


def _patch_health() -> contextlib.AbstractContextManager[object]:
    """No-op the health check in poll-body tests."""

    async def _noop(*_a: object, **_k: object) -> None:
        return None

    return patch("app.scheduler.check_ingestion_health", _noop)


def test_build_scheduler_registers_single_job() -> None:
    """``build_scheduler`` configures but does not start — verify the job
    is registered without needing a running event loop."""

    async def _noop() -> None:
        return None

    scheduler = build_scheduler(tick_minutes=15, job_func=_noop)
    jobs = scheduler.get_jobs()
    assert len(jobs) == 1
    assert jobs[0].id == "poll_due_sources"


def test_start_scheduler_returns_none_when_all_disabled() -> None:
    """Default settings have every scheduler off — verify nothing starts."""
    with patch("app.scheduler.settings") as mock_settings:
        mock_settings.poll_scheduler_enabled = False
        mock_settings.url_health_check_enabled = False
        mock_settings.retention_purge_enabled = False
        mock_settings.discovery_scheduler_enabled = False
        mock_settings.recency_refresh_enabled = False
        result = start_scheduler_if_enabled()
    assert result is None


@pytest.mark.asyncio
async def test_start_scheduler_registers_only_poll_when_only_poll_enabled() -> None:
    """AsyncIOScheduler binds to the running loop, so this test has to
    be async — production calls it from inside the FastAPI lifespan."""
    with patch("app.scheduler.settings") as mock_settings:
        mock_settings.poll_scheduler_enabled = True
        mock_settings.poll_tick_minutes = 30
        mock_settings.url_health_check_enabled = False
        mock_settings.retention_purge_enabled = False
        mock_settings.discovery_scheduler_enabled = False
        mock_settings.recency_refresh_enabled = False
        scheduler = start_scheduler_if_enabled()

    assert scheduler is not None
    try:
        assert scheduler.running is True
        jobs = scheduler.get_jobs()
        assert len(jobs) == 1
        assert jobs[0].id == "poll_due_sources"
    finally:
        scheduler.shutdown(wait=False)


@pytest.mark.asyncio
async def test_start_scheduler_registers_only_url_health_when_only_url_health_enabled() -> None:
    """URL-health-only operation — verify only the url_health_check job lands."""
    with patch("app.scheduler.settings") as mock_settings:
        mock_settings.poll_scheduler_enabled = False
        mock_settings.url_health_check_enabled = True
        mock_settings.url_health_tick_hours = 6
        mock_settings.retention_purge_enabled = False
        mock_settings.discovery_scheduler_enabled = False
        mock_settings.recency_refresh_enabled = False
        scheduler = start_scheduler_if_enabled()

    assert scheduler is not None
    try:
        assert scheduler.running is True
        ids = {j.id for j in scheduler.get_jobs()}
        # The ledger catch-up rides with the interval job (#327 follow-up).
        assert ids == {"url_health_check", "url_health_check_catchup"}
    finally:
        scheduler.shutdown(wait=False)


@pytest.mark.asyncio
async def test_start_scheduler_registers_both_jobs_when_both_enabled() -> None:
    """Both flags on — both jobs live on the single shared scheduler."""
    with patch("app.scheduler.settings") as mock_settings:
        mock_settings.poll_scheduler_enabled = True
        mock_settings.poll_tick_minutes = 30
        mock_settings.url_health_check_enabled = True
        mock_settings.url_health_tick_hours = 6
        mock_settings.retention_purge_enabled = False
        mock_settings.discovery_scheduler_enabled = False
        mock_settings.recency_refresh_enabled = False
        scheduler = start_scheduler_if_enabled()

    assert scheduler is not None
    try:
        assert scheduler.running is True
        ids = {j.id for j in scheduler.get_jobs()}
        assert ids == {
            "poll_due_sources",
            "url_health_check",
            "url_health_check_catchup",
        }
    finally:
        scheduler.shutdown(wait=False)


@pytest.mark.asyncio
async def test_start_scheduler_registers_only_retention_when_only_retention_enabled() -> None:
    """Retention-only operation — verify only the retention_purge job lands."""
    with patch("app.scheduler.settings") as mock_settings:
        mock_settings.poll_scheduler_enabled = False
        mock_settings.url_health_check_enabled = False
        mock_settings.retention_purge_enabled = True
        mock_settings.retention_purge_tick_hours = 24
        mock_settings.discovery_scheduler_enabled = False
        mock_settings.recency_refresh_enabled = False
        scheduler = start_scheduler_if_enabled()

    assert scheduler is not None
    try:
        assert scheduler.running is True
        ids = {j.id for j in scheduler.get_jobs()}
        assert ids == {"retention_purge", "retention_purge_catchup"}
    finally:
        scheduler.shutdown(wait=False)


@pytest.mark.asyncio
async def test_start_scheduler_registers_three_when_poll_health_retention_enabled() -> None:
    """Poll + url_health + retention on, discovery off — exactly those three
    jobs live on the single shared scheduler."""
    with patch("app.scheduler.settings") as mock_settings:
        mock_settings.poll_scheduler_enabled = True
        mock_settings.poll_tick_minutes = 30
        mock_settings.url_health_check_enabled = True
        mock_settings.url_health_tick_hours = 6
        mock_settings.retention_purge_enabled = True
        mock_settings.retention_purge_tick_hours = 24
        mock_settings.discovery_scheduler_enabled = False
        mock_settings.recency_refresh_enabled = False
        scheduler = start_scheduler_if_enabled()

    assert scheduler is not None
    try:
        assert scheduler.running is True
        ids = {j.id for j in scheduler.get_jobs()}
        assert ids == {
            "poll_due_sources",
            "url_health_check",
            "url_health_check_catchup",
            "retention_purge",
            "retention_purge_catchup",
        }
    finally:
        scheduler.shutdown(wait=False)


@pytest.mark.asyncio
async def test_start_scheduler_registers_only_discovery_when_only_discovery_enabled() -> None:
    """Discovery-only operation — verify only the discovery_run job lands, and
    that it's the bulk all-targets body (``run_discovery_all_targets_locked``)."""
    with patch("app.scheduler.settings") as mock_settings:
        mock_settings.poll_scheduler_enabled = False
        mock_settings.url_health_check_enabled = False
        mock_settings.retention_purge_enabled = False
        mock_settings.discovery_scheduler_enabled = True
        mock_settings.discovery_tick_hours = 24
        mock_settings.recency_refresh_enabled = False
        scheduler = start_scheduler_if_enabled()

    assert scheduler is not None
    try:
        assert scheduler.running is True
        jobs = {j.id: j for j in scheduler.get_jobs()}
        # discovery_run (the 24h interval) + discovery_catchup (the one-shot
        # boot anchor that stops near-daily deploys from starving the tick).
        assert set(jobs) == {"discovery_run", "discovery_catchup"}
        # The registered callable is the bulk, all-targets, advisory-locked body.
        assert jobs["discovery_run"].func is run_discovery_all_targets_locked
    finally:
        scheduler.shutdown(wait=False)


@pytest.mark.asyncio
async def test_discovery_scheduler_off_by_default_does_not_register() -> None:
    """With every flag off (the default posture) discovery does NOT start —
    the Brave-key gate is the inner guard, but the flag is the outer one."""
    with patch("app.scheduler.settings") as mock_settings:
        mock_settings.poll_scheduler_enabled = False
        mock_settings.url_health_check_enabled = False
        mock_settings.retention_purge_enabled = False
        mock_settings.discovery_scheduler_enabled = False
        mock_settings.recency_refresh_enabled = False
        scheduler = start_scheduler_if_enabled()
    # No flags on → no scheduler at all, so no discovery_run job.
    assert scheduler is None


@pytest.mark.asyncio
async def test_start_scheduler_registers_all_five_when_all_enabled() -> None:
    """All five flags on — five jobs live on the single shared scheduler."""
    with patch("app.scheduler.settings") as mock_settings:
        mock_settings.poll_scheduler_enabled = True
        mock_settings.poll_tick_minutes = 30
        mock_settings.url_health_check_enabled = True
        mock_settings.url_health_tick_hours = 6
        mock_settings.retention_purge_enabled = True
        mock_settings.retention_purge_tick_hours = 24
        mock_settings.discovery_scheduler_enabled = True
        mock_settings.discovery_tick_hours = 24
        mock_settings.recency_refresh_enabled = True
        mock_settings.recency_refresh_tick_hours = 12
        scheduler = start_scheduler_if_enabled()

    assert scheduler is not None
    try:
        assert scheduler.running is True
        ids = {j.id for j in scheduler.get_jobs()}
        assert ids == {
            "poll_due_sources",
            "url_health_check",
            "url_health_check_catchup",
            "retention_purge",
            "retention_purge_catchup",
            "discovery_run",
            "discovery_catchup",
            "recency_refresh",
            "recency_refresh_catchup",
        }
    finally:
        scheduler.shutdown(wait=False)


@pytest.mark.asyncio
async def test_start_scheduler_registers_only_recency_when_only_recency_enabled() -> None:
    """Recency-refresh-only operation — verify only the recency_refresh job lands."""
    with patch("app.scheduler.settings") as mock_settings:
        mock_settings.poll_scheduler_enabled = False
        mock_settings.url_health_check_enabled = False
        mock_settings.retention_purge_enabled = False
        mock_settings.discovery_scheduler_enabled = False
        mock_settings.recency_refresh_enabled = True
        mock_settings.recency_refresh_tick_hours = 12
        scheduler = start_scheduler_if_enabled()

    assert scheduler is not None
    try:
        assert scheduler.running is True
        ids = {j.id for j in scheduler.get_jobs()}
        assert ids == {"recency_refresh", "recency_refresh_catchup"}
    finally:
        scheduler.shutdown(wait=False)


@pytest.mark.asyncio
async def test_run_scheduled_retention_purge_invokes_service_with_windows() -> None:
    """The tick body must pull the async service-role client and pass the
    configured windows to ``purge_expired_records`` (awaited natively)."""
    from app.scheduler import _run_scheduled_retention_purge

    fake_client = object()
    with (
        patch("app.scheduler.get_async_supabase", return_value=fake_client),
        patch("app.scheduler.settings") as mock_settings,
        patch("app.scheduler.purge_expired_records", autospec=True) as mock_purge,
    ):
        mock_settings.llm_costs_retention_days = 365
        mock_settings.notifications_sent_retention_days = 180
        mock_settings.prescan_shadow_retention_days = 30
        mock_settings.search_events_retention_days = 90
        mock_purge.return_value = {
            "llm_costs": 0,
            "notifications_sent": 0,
            "prescan_shadow": 0,
            "search_events": 0,
        }
        await _run_scheduled_retention_purge()

    mock_purge.assert_called_once_with(
        fake_client,
        llm_costs_days=365,
        notifications_sent_days=180,
        prescan_shadow_days=30,
        search_events_days=90,
    )


@pytest.mark.asyncio
async def test_run_scheduled_retention_purge_skips_when_supabase_uninitialized() -> None:
    from app.scheduler import _run_scheduled_retention_purge

    with (
        patch("app.scheduler.get_async_supabase", return_value=None),
        patch("app.scheduler.purge_expired_records", autospec=True) as mock_purge,
    ):
        await _run_scheduled_retention_purge()

    mock_purge.assert_not_called()


@pytest.mark.asyncio
async def test_run_scheduled_retention_purge_swallows_exceptions() -> None:
    from app.scheduler import _run_scheduled_retention_purge

    with (
        patch("app.scheduler.get_async_supabase", return_value=object()),
        patch(
            "app.scheduler.purge_expired_records",
            side_effect=RuntimeError("kaboom"),
        ),
    ):
        # Must not raise.
        await _run_scheduled_retention_purge()


@pytest.mark.asyncio
async def test_run_scheduled_recency_refresh_invokes_sweep_and_invalidates_cache() -> None:
    """The tick body must pull the async service-role client, await the full
    sweep natively, and invalidate the list cache when rows were rewritten."""
    from app.scheduler import _run_scheduled_recency_refresh

    fake_client = object()
    with (
        patch("app.scheduler.get_async_supabase", return_value=fake_client),
        patch("app.scheduler.refresh_all_recency_scores", autospec=True) as mock_sweep,
        patch("app.scheduler.job_list_cache") as mock_cache,
    ):
        mock_sweep.return_value = 12
        await _run_scheduled_recency_refresh()

    mock_sweep.assert_called_once_with(fake_client)
    mock_cache.invalidate.assert_called_once()


@pytest.mark.asyncio
async def test_run_scheduled_recency_refresh_skips_cache_invalidate_when_nothing_written() -> None:
    from app.scheduler import _run_scheduled_recency_refresh

    with (
        patch("app.scheduler.get_async_supabase", return_value=object()),
        patch("app.scheduler.refresh_all_recency_scores", return_value=0),
        patch("app.scheduler.job_list_cache") as mock_cache,
    ):
        await _run_scheduled_recency_refresh()

    mock_cache.invalidate.assert_not_called()


@pytest.mark.asyncio
async def test_run_scheduled_recency_refresh_skips_when_supabase_uninitialized() -> None:
    from app.scheduler import _run_scheduled_recency_refresh

    with (
        patch("app.scheduler.get_async_supabase", return_value=None),
        patch("app.scheduler.refresh_all_recency_scores", autospec=True) as mock_sweep,
    ):
        await _run_scheduled_recency_refresh()

    mock_sweep.assert_not_called()


@pytest.mark.asyncio
async def test_run_scheduled_recency_refresh_swallows_exceptions() -> None:
    from app.scheduler import _run_scheduled_recency_refresh

    with (
        patch("app.scheduler.get_async_supabase", return_value=object()),
        patch(
            "app.scheduler.refresh_all_recency_scores",
            side_effect=RuntimeError("kaboom"),
        ),
    ):
        # Must not raise — APScheduler would swallow the trace otherwise.
        await _run_scheduled_recency_refresh()


@pytest.mark.asyncio
async def test_run_scheduled_poll_invokes_due_poller_with_pool_client() -> None:
    """The tick body must pull the singleton supabase client and pass it
    to ``poll_due_sources``. Easy to break by accident — guard it."""
    from app.models.schemas import PollResult
    from app.scheduler import _run_scheduled_poll

    fake_client = object()
    fake_result = PollResult(
        sources_polled=1, new_jobs=2, updated_jobs=0, archived_jobs=0, errors=[]
    )

    with (
        patch("app.scheduler.get_supabase_pool", return_value=fake_client),
        patch("app.scheduler.poll_due_sources", autospec=True) as mock_poll,
        _patch_lock_acquired(),
        _patch_health(),
    ):
        mock_poll.return_value = fake_result
        await _run_scheduled_poll()

    mock_poll.assert_awaited_once_with(fake_client)


@pytest.mark.asyncio
async def test_run_scheduled_poll_skips_when_supabase_uninitialized() -> None:
    """If Supabase env isn't configured, ``get_supabase_pool`` returns
    None — the tick should log+skip instead of crashing."""
    from app.scheduler import _run_scheduled_poll

    with (
        patch("app.scheduler.get_supabase_pool", return_value=None),
        patch("app.scheduler.poll_due_sources", autospec=True) as mock_poll,
    ):
        await _run_scheduled_poll()

    mock_poll.assert_not_called()


@pytest.mark.asyncio
async def test_run_scheduled_poll_swallows_exceptions() -> None:
    """APScheduler swallows exceptions from job bodies silently — that's
    a debugging trap. The tick must log and return cleanly itself."""
    from app.scheduler import _run_scheduled_poll

    with (
        patch("app.scheduler.get_supabase_pool", return_value=object()),
        patch("app.scheduler.poll_due_sources", side_effect=RuntimeError("kaboom")),
        _patch_lock_acquired(),
        _patch_health(),
    ):
        # Must not raise.
        await _run_scheduled_poll()


@pytest.mark.asyncio
async def test_run_scheduled_poll_aborts_hung_cycle_via_watchdog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A HUNG poll cycle must be aborted by the watchdog so the next tick can
    run. Without it, ``max_instances=1`` wedges the scheduler until a restart
    (the 2026-07-06 402-storm incident: 68 min of no polls while the API stayed
    up). The tick returns cleanly (no raise) after the timeout; the health
    checks still run (#350 — a broken cycle is exactly when the alarms must
    fire), only the cache-invalidate/log post-poll steps are skipped."""
    import app.scheduler as sched
    from app.scheduler import _run_scheduled_poll

    monkeypatch.setattr(sched.settings, "poll_cycle_timeout_seconds", 0.05)

    async def _hang(*_a: object, **_k: object) -> None:
        await asyncio.Event().wait()  # never completes

    health_calls = {"n": 0}

    async def _count_health(*_a: object, **_k: object) -> None:
        health_calls["n"] += 1

    with (
        patch("app.scheduler.get_supabase_pool", return_value=object()),
        patch("app.scheduler.poll_due_sources", _hang),
        _patch_lock_acquired(),
        patch("app.scheduler.check_ingestion_health", _count_health),
    ):
        # Bound the test so a regression that drops the watchdog fails loudly
        # (outer timeout) instead of hanging the suite forever.
        await asyncio.wait_for(_run_scheduled_poll(), timeout=5)

    # The health pass runs even after a timed-out cycle (#350): the alarms
    # must not be silenced by the very breakage they exist to report.
    assert health_calls["n"] == 1


@pytest.mark.asyncio
async def test_run_scheduled_poll_watchdog_disabled_when_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """timeout=0 disables the watchdog (``wait_for(None)``) — a normal cycle
    still completes end-to-end (the pre-watchdog behavior)."""
    import app.scheduler as sched
    from app.models.schemas import PollResult
    from app.scheduler import _run_scheduled_poll

    monkeypatch.setattr(sched.settings, "poll_cycle_timeout_seconds", 0)
    result = PollResult(sources_polled=1, new_jobs=0, updated_jobs=0, archived_jobs=0, errors=[])

    with (
        patch("app.scheduler.get_supabase_pool", return_value=object()),
        patch("app.scheduler.poll_due_sources", autospec=True) as mock_poll,
        _patch_lock_acquired(),
        _patch_health(),
    ):
        mock_poll.return_value = result
        await _run_scheduled_poll()

    mock_poll.assert_awaited_once()


# ---- discovery catch-up (anchor to last persisted run, task #33) ------------
#
# IntervalTrigger measures from process start; near-daily deploys meant the
# 24h discovery tick never elapsed (one run in six enabled days, 2026-07-13).
# The catch-up must: run discovery when the last persisted run is overdue or
# absent, anchor the next interval fire to last+tick when fresh, and never
# raise into the scheduler.


class _RecordingScheduler:
    def __init__(self) -> None:
        self.modified: list[tuple[str, object]] = []

    def modify_job(self, job_id: str, **changes: object) -> None:
        self.modified.append((job_id, changes["next_run_time"]))


def _patch_discovery_deps(
    monkeypatch: pytest.MonkeyPatch,
    *,
    last_run: "datetime | None | Exception",
) -> list[int]:
    """Wire the catch-up's deps: a present pool client, a scripted
    ``_newest_discovery_at``, and a spy on the discovery run body."""
    runs: list[int] = []

    monkeypatch.setattr("app.scheduler.get_supabase_pool", lambda: object())

    async def fake_newest(_client: object) -> "datetime | None":
        if isinstance(last_run, Exception):
            raise last_run
        return last_run

    monkeypatch.setattr("app.scheduler._newest_discovery_at", fake_newest)

    async def fake_run() -> None:
        runs.append(1)

    monkeypatch.setattr("app.scheduler.run_discovery_all_targets_locked", fake_run)
    return runs


_CATCHUP_NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


class TestDiscoveryCatchup:
    @pytest.mark.asyncio
    async def test_never_ran_runs_now(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runs = _patch_discovery_deps(monkeypatch, last_run=None)
        sched = _RecordingScheduler()

        await _anchor_discovery_schedule(sched, now=_CATCHUP_NOW)  # type: ignore[arg-type]

        assert runs == [1]
        assert sched.modified == []

    @pytest.mark.asyncio
    async def test_overdue_runs_now(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "discovery_tick_hours", 24)
        runs = _patch_discovery_deps(monkeypatch, last_run=_CATCHUP_NOW - timedelta(hours=25))
        sched = _RecordingScheduler()

        await _anchor_discovery_schedule(sched, now=_CATCHUP_NOW)  # type: ignore[arg-type]

        assert runs == [1]
        assert sched.modified == []

    @pytest.mark.asyncio
    async def test_fresh_anchors_next_fire_instead_of_running(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "discovery_tick_hours", 24)
        last = _CATCHUP_NOW - timedelta(hours=2)
        runs = _patch_discovery_deps(monkeypatch, last_run=last)
        sched = _RecordingScheduler()

        await _anchor_discovery_schedule(sched, now=_CATCHUP_NOW)  # type: ignore[arg-type]

        assert runs == []
        assert sched.modified == [("discovery_run", last + timedelta(hours=24))]

    @pytest.mark.asyncio
    async def test_db_error_is_fail_soft(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runs = _patch_discovery_deps(monkeypatch, last_run=RuntimeError("db down"))
        sched = _RecordingScheduler()

        await _anchor_discovery_schedule(sched, now=_CATCHUP_NOW)  # must not raise

        assert runs == []
        assert sched.modified == []

    @pytest.mark.asyncio
    async def test_missing_pool_client_skips_quietly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("app.scheduler.get_supabase_pool", lambda: None)
        sched = _RecordingScheduler()

        await _anchor_discovery_schedule(sched, now=_CATCHUP_NOW)  # must not raise

        assert sched.modified == []

    @pytest.mark.asyncio
    async def test_modify_job_error_is_fail_soft(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "discovery_tick_hours", 24)
        _patch_discovery_deps(monkeypatch, last_run=_CATCHUP_NOW - timedelta(hours=2))

        class _BoomScheduler:
            def modify_job(self, job_id: str, **changes: object) -> None:
                raise RuntimeError("job store closed")

        await _anchor_discovery_schedule(_BoomScheduler(), now=_CATCHUP_NOW)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# scheduler_runs ledger: the generic catch-up + the stamp/read helpers
# ---------------------------------------------------------------------------


def _patch_ledger_read(
    monkeypatch: pytest.MonkeyPatch,
    *,
    last_run: "datetime | None | Exception",
) -> None:
    async def fake_last(_job_id: str) -> "datetime | None":
        if isinstance(last_run, Exception):
            raise last_run
        return last_run

    monkeypatch.setattr("app.scheduler._last_scheduler_run", fake_last)


def _spy_runner() -> tuple[list[int], "Callable[[], Awaitable[None]]"]:
    runs: list[int] = []

    async def runner() -> None:
        runs.append(1)

    return runs, runner


class TestLedgerCatchup:
    @pytest.mark.asyncio
    async def test_never_ran_runs_now(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_ledger_read(monkeypatch, last_run=None)
        runs, runner = _spy_runner()
        sched = _RecordingScheduler()

        await _anchor_job_from_ledger(
            sched,  # type: ignore[arg-type]
            job_id="url_health_check",
            tick_hours=12,
            runner=runner,
            now=_CATCHUP_NOW,
        )

        assert runs == [1]
        assert sched.modified == []

    @pytest.mark.asyncio
    async def test_overdue_runs_now(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_ledger_read(monkeypatch, last_run=_CATCHUP_NOW - timedelta(hours=13))
        runs, runner = _spy_runner()
        sched = _RecordingScheduler()

        await _anchor_job_from_ledger(
            sched,  # type: ignore[arg-type]
            job_id="url_health_check",
            tick_hours=12,
            runner=runner,
            now=_CATCHUP_NOW,
        )

        assert runs == [1]
        assert sched.modified == []

    @pytest.mark.asyncio
    async def test_fresh_anchors_next_fire_instead_of_running(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        last = _CATCHUP_NOW - timedelta(hours=2)
        _patch_ledger_read(monkeypatch, last_run=last)
        runs, runner = _spy_runner()
        sched = _RecordingScheduler()

        await _anchor_job_from_ledger(
            sched,  # type: ignore[arg-type]
            job_id="retention_purge",
            tick_hours=24,
            runner=runner,
            now=_CATCHUP_NOW,
        )

        assert runs == []
        assert sched.modified == [("retention_purge", last + timedelta(hours=24))]

    @pytest.mark.asyncio
    async def test_ledger_read_error_is_fail_soft(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_ledger_read(monkeypatch, last_run=RuntimeError("db down"))
        runs, runner = _spy_runner()
        sched = _RecordingScheduler()

        await _anchor_job_from_ledger(
            sched,  # type: ignore[arg-type]  # must not raise
            job_id="recency_refresh",
            tick_hours=12,
            runner=runner,
            now=_CATCHUP_NOW,
        )

        assert runs == []
        assert sched.modified == []

    @pytest.mark.asyncio
    async def test_runner_error_is_fail_soft(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_ledger_read(monkeypatch, last_run=None)

        async def boom() -> None:
            raise RuntimeError("job body exploded")

        await _anchor_job_from_ledger(
            _RecordingScheduler(),  # type: ignore[arg-type]  # must not raise
            job_id="url_health_check",
            tick_hours=12,
            runner=boom,
            now=_CATCHUP_NOW,
        )

    @pytest.mark.asyncio
    async def test_modify_job_error_is_fail_soft(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_ledger_read(monkeypatch, last_run=_CATCHUP_NOW - timedelta(hours=2))
        runs, runner = _spy_runner()

        class _BoomScheduler:
            def modify_job(self, job_id: str, **changes: object) -> None:
                raise RuntimeError("job store closed")

        await _anchor_job_from_ledger(
            _BoomScheduler(),  # type: ignore[arg-type]  # must not raise
            job_id="retention_purge",
            tick_hours=24,
            runner=runner,
            now=_CATCHUP_NOW,
        )

        assert runs == []


class _LedgerTableStub:
    """Just enough of the postgrest chain for the stamp/read helpers."""

    def __init__(
        self, *, rows: "list[dict[str, object]] | None" = None, boom: bool = False
    ) -> None:
        self.rows = rows or []
        self.boom = boom
        self.upserted: list[dict[str, object]] = []

    # both chains funnel through table(); every step returns self
    def table(self, name: str) -> "_LedgerTableStub":
        assert name == "scheduler_runs"
        return self

    def upsert(self, payload: dict[str, object]) -> "_LedgerTableStub":
        self.upserted.append(payload)
        return self

    def select(self, *_cols: str) -> "_LedgerTableStub":
        return self

    def eq(self, *_a: object) -> "_LedgerTableStub":
        return self

    def limit(self, _n: int) -> "_LedgerTableStub":
        return self

    async def execute(self) -> object:
        if self.boom:
            raise RuntimeError("pooler down")

        class _Resp:
            def __init__(self, data: object) -> None:
                self.data = data

        return _Resp(self.rows)


class TestLedgerHelpers:
    @pytest.mark.asyncio
    async def test_record_stamps_the_job_row(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _LedgerTableStub()
        monkeypatch.setattr("app.scheduler.get_async_supabase", lambda: stub)

        await _record_scheduler_run("url_health_check")

        assert len(stub.upserted) == 1
        assert stub.upserted[0]["job_id"] == "url_health_check"
        assert stub.upserted[0]["last_run_at"]

    @pytest.mark.asyncio
    async def test_record_skips_without_pool_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("app.scheduler.get_async_supabase", lambda: None)

        await _record_scheduler_run("url_health_check")  # must not raise

    @pytest.mark.asyncio
    async def test_record_write_error_is_fail_soft(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _LedgerTableStub(boom=True)
        monkeypatch.setattr("app.scheduler.get_async_supabase", lambda: stub)

        await _record_scheduler_run("url_health_check")  # must not raise

    @pytest.mark.asyncio
    async def test_last_run_parses_z_suffix_utc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _LedgerTableStub(rows=[{"last_run_at": "2026-07-14T10:30:00Z"}])
        monkeypatch.setattr("app.scheduler.get_async_supabase", lambda: stub)

        got = await _last_scheduler_run("retention_purge")

        assert got == datetime(2026, 7, 14, 10, 30, tzinfo=UTC)

    @pytest.mark.asyncio
    async def test_last_run_none_when_no_row(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _LedgerTableStub(rows=[])
        monkeypatch.setattr("app.scheduler.get_async_supabase", lambda: stub)

        assert await _last_scheduler_run("retention_purge") is None

    @pytest.mark.asyncio
    async def test_last_run_none_without_pool_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("app.scheduler.get_async_supabase", lambda: None)

        assert await _last_scheduler_run("retention_purge") is None
