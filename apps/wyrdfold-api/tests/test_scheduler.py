"""Tests for the in-process APScheduler wiring."""

import contextlib
from collections.abc import AsyncIterator
from unittest.mock import patch

import pytest

from app.scheduler import build_scheduler, start_scheduler_if_enabled
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
        scheduler = start_scheduler_if_enabled()

    assert scheduler is not None
    try:
        assert scheduler.running is True
        jobs = scheduler.get_jobs()
        assert len(jobs) == 1
        assert jobs[0].id == "url_health_check"
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
        scheduler = start_scheduler_if_enabled()

    assert scheduler is not None
    try:
        assert scheduler.running is True
        ids = {j.id for j in scheduler.get_jobs()}
        assert ids == {"poll_due_sources", "url_health_check"}
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
        scheduler = start_scheduler_if_enabled()

    assert scheduler is not None
    try:
        assert scheduler.running is True
        jobs = scheduler.get_jobs()
        assert len(jobs) == 1
        assert jobs[0].id == "retention_purge"
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
        scheduler = start_scheduler_if_enabled()

    assert scheduler is not None
    try:
        assert scheduler.running is True
        ids = {j.id for j in scheduler.get_jobs()}
        assert ids == {"poll_due_sources", "url_health_check", "retention_purge"}
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
        scheduler = start_scheduler_if_enabled()

    assert scheduler is not None
    try:
        assert scheduler.running is True
        jobs = scheduler.get_jobs()
        assert len(jobs) == 1
        assert jobs[0].id == "discovery_run"
        # The registered callable is the bulk, all-targets, advisory-locked body.
        assert jobs[0].func is run_discovery_all_targets_locked
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
        scheduler = start_scheduler_if_enabled()
    # No flags on → no scheduler at all, so no discovery_run job.
    assert scheduler is None


@pytest.mark.asyncio
async def test_start_scheduler_registers_all_four_when_all_enabled() -> None:
    """All four flags on — four jobs live on the single shared scheduler."""
    with patch("app.scheduler.settings") as mock_settings:
        mock_settings.poll_scheduler_enabled = True
        mock_settings.poll_tick_minutes = 30
        mock_settings.url_health_check_enabled = True
        mock_settings.url_health_tick_hours = 6
        mock_settings.retention_purge_enabled = True
        mock_settings.retention_purge_tick_hours = 24
        mock_settings.discovery_scheduler_enabled = True
        mock_settings.discovery_tick_hours = 24
        scheduler = start_scheduler_if_enabled()

    assert scheduler is not None
    try:
        assert scheduler.running is True
        ids = {j.id for j in scheduler.get_jobs()}
        assert ids == {
            "poll_due_sources",
            "url_health_check",
            "retention_purge",
            "discovery_run",
        }
    finally:
        scheduler.shutdown(wait=False)


@pytest.mark.asyncio
async def test_run_scheduled_retention_purge_invokes_service_with_windows() -> None:
    """The tick body must pull the singleton client and pass the configured
    windows to ``purge_expired_records`` (run in a worker thread)."""
    from app.scheduler import _run_scheduled_retention_purge

    fake_client = object()
    with (
        patch("app.scheduler.get_supabase_pool", return_value=fake_client),
        patch("app.scheduler.settings") as mock_settings,
        patch("app.scheduler.purge_expired_records", autospec=True) as mock_purge,
    ):
        mock_settings.llm_costs_retention_days = 365
        mock_settings.notifications_sent_retention_days = 180
        mock_purge.return_value = {"llm_costs": 0, "notifications_sent": 0}
        await _run_scheduled_retention_purge()

    mock_purge.assert_called_once_with(fake_client, llm_costs_days=365, notifications_sent_days=180)


@pytest.mark.asyncio
async def test_run_scheduled_retention_purge_skips_when_supabase_uninitialized() -> None:
    from app.scheduler import _run_scheduled_retention_purge

    with (
        patch("app.scheduler.get_supabase_pool", return_value=None),
        patch("app.scheduler.purge_expired_records", autospec=True) as mock_purge,
    ):
        await _run_scheduled_retention_purge()

    mock_purge.assert_not_called()


@pytest.mark.asyncio
async def test_run_scheduled_retention_purge_swallows_exceptions() -> None:
    from app.scheduler import _run_scheduled_retention_purge

    with (
        patch("app.scheduler.get_supabase_pool", return_value=object()),
        patch(
            "app.scheduler.purge_expired_records",
            side_effect=RuntimeError("kaboom"),
        ),
    ):
        # Must not raise.
        await _run_scheduled_retention_purge()


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
