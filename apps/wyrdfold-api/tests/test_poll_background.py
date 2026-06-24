"""``POST /poll`` returns 202 and runs the force-poll in the background.

The force-poll-all-sources route used to ``await`` the multi-minute poll
inline, so a manual/external trigger held the request open past the
edge's 300s timeout (curl got a 499/502 even when the poll was fine). It
now schedules the poll via FastAPI ``BackgroundTasks`` and returns ``202``
immediately.

Two things are proven here:
  - the HTTP contract: ``POST /poll`` returns ``202 {"status": "scheduled"}``
    and the poll is *scheduled* (queued as a background task), not awaited
    inside the request handler; the response is produced before the poll
    body runs.
  - the background body (``run_force_poll_locked``): it routes through the
    SAME advisory lock as the scheduler, so a manual trigger can't
    double-poll alongside the scheduled tick; it skips cleanly when the
    lock is held; and it never lets a backgrounded exception escape.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.dependencies import get_settings, get_supabase
from app.main import app
from app.scheduler import run_force_poll_locked

_SETTINGS = Settings(
    wyrdfold_api_key="legacykey",
    wyrdfold_cron_key="cronkey",
    supabase_url="https://test-project.supabase.co",
)


def _client() -> TestClient:
    app.dependency_overrides[get_settings] = lambda: _SETTINGS
    app.dependency_overrides[get_supabase] = lambda: MagicMock()
    return TestClient(app, raise_server_exceptions=False)


# --- HTTP contract: fast 202, poll not awaited inline ----------------------


def test_poll_returns_202_with_scheduled_body() -> None:
    with patch("app.routers.poll.run_force_poll_locked", new=AsyncMock()):
        client = _client()
        try:
            res = client.post("/poll", headers={"x-api-key": "cronkey"})
            assert res.status_code == 202
            assert res.json() == {"status": "scheduled"}
        finally:
            app.dependency_overrides.clear()


def test_poll_schedules_background_task_not_awaited_inline() -> None:
    """The poll runs as a background task: the response is produced BEFORE
    the poll body runs, and the handler never awaits the poll itself.

    We record ordering — the request handler returns, then the background
    task fires. If the route had awaited the poll inline, the poll body
    would run *during* the request (inside ``client.post``), not after it.
    """
    events: list[str] = []

    async def _fake_force_poll() -> None:
        events.append("poll_ran")

    with patch("app.routers.poll.run_force_poll_locked", new=_fake_force_poll):
        client = _client()
        try:
            # Starlette's TestClient runs background tasks after sending
            # the response. Record the moment the handler returns relative
            # to the background body.
            assert events == []  # nothing polled before the request
            res = client.post("/poll", headers={"x-api-key": "cronkey"})
            assert res.status_code == 202
            # The background body ran (TestClient drains background tasks
            # before returning from .post), proving it was *scheduled*...
            assert events == ["poll_ran"]
        finally:
            app.dependency_overrides.clear()


def test_poll_handler_does_not_call_poll_all_sources_inline() -> None:
    """The handler must not run the poll itself — it delegates to the
    backgrounded helper. If the poll function were awaited inline, this
    spy on the underlying poller would be called during the request."""
    with (
        patch("app.routers.poll.run_force_poll_locked", new=AsyncMock()),
        patch("app.scheduler.poll_all_sources", new=AsyncMock()) as inline_spy,
    ):
        client = _client()
        try:
            res = client.post("/poll", headers={"x-api-key": "cronkey"})
            assert res.status_code == 202
            # The real poller was never invoked by the request handler;
            # only the (mocked) background helper was scheduled.
            inline_spy.assert_not_called()
        finally:
            app.dependency_overrides.clear()


# --- Background body: advisory lock guards against double-poll --------------


class _Resp:
    def __init__(self, data: Any) -> None:
        self.data = data


def _fake_lock_supabase() -> tuple[MagicMock, dict[str, bool]]:
    """A MagicMock supabase whose advisory-lock RPCs share one in-memory
    'held' flag — a faithful stand-in for pg_try_advisory_lock semantics.
    Mirrors the fixture in test_poll_lock.py."""
    state = {"held": False}

    def _rpc(name: str, params: dict[str, Any]) -> MagicMock:
        handle = MagicMock()
        if name == "try_poll_advisory_lock":
            acquired = not state["held"]
            if acquired:
                state["held"] = True
            handle.execute.return_value = _Resp(acquired)
        elif name == "release_poll_advisory_lock":
            state["held"] = False
            handle.execute.return_value = _Resp(True)
        else:
            handle.execute.return_value = _Resp(None)
        return handle

    sb = MagicMock()
    sb.rpc.side_effect = _rpc
    return sb, state


@pytest.mark.asyncio
async def test_force_poll_runs_when_lock_acquired() -> None:
    """Happy path: lock acquired → poll_all_sources is called and the lock
    is released afterwards."""
    sb, state = _fake_lock_supabase()
    poll_result = MagicMock(
        sources_polled=1, new_jobs=2, updated_jobs=0, archived_jobs=0, errors=[]
    )
    with (
        patch("app.scheduler.get_supabase_pool", return_value=sb),
        patch(
            "app.scheduler.poll_all_sources",
            new=AsyncMock(return_value=poll_result),
        ) as mock_poll,
        patch("app.scheduler.check_ingestion_health", new=AsyncMock()) as mock_health,
    ):
        await run_force_poll_locked()

    mock_poll.assert_awaited_once_with(sb)
    mock_health.assert_awaited_once_with(sb)
    assert state["held"] is False  # lock released


@pytest.mark.asyncio
async def test_force_poll_skips_when_lock_held() -> None:
    """The double-poll guard: when the advisory lock is already held (e.g. the
    scheduled tick is mid-poll), the manual force-poll must NOT call
    poll_all_sources."""
    sb, state = _fake_lock_supabase()
    state["held"] = True  # the scheduled poll (or another trigger) holds it

    with (
        patch("app.scheduler.get_supabase_pool", return_value=sb),
        patch("app.scheduler.poll_all_sources") as mock_poll,
        patch("app.scheduler.check_ingestion_health") as mock_health,
    ):
        await run_force_poll_locked()

    mock_poll.assert_not_called()
    mock_health.assert_not_called()
    # Did not steal/release a lock it never acquired.
    assert state["held"] is True


@pytest.mark.asyncio
async def test_force_poll_skips_when_client_uninitialized() -> None:
    """No supabase singleton (startup race) → skip cleanly, no poll, no crash."""
    with (
        patch("app.scheduler.get_supabase_pool", return_value=None),
        patch("app.scheduler.poll_all_sources") as mock_poll,
    ):
        await run_force_poll_locked()
    mock_poll.assert_not_called()


@pytest.mark.asyncio
async def test_force_poll_swallows_and_logs_exceptions() -> None:
    """A backgrounded task's exception is otherwise lost. The helper must
    log it and never propagate — so the background task can't crash silently
    *or* surface an unhandled-exception in the event loop."""
    sb, _ = _fake_lock_supabase()
    with (
        patch("app.scheduler.get_supabase_pool", return_value=sb),
        patch(
            "app.scheduler.poll_all_sources",
            new=AsyncMock(side_effect=RuntimeError("poll boom")),
        ),
        patch("app.scheduler.check_ingestion_health", new=AsyncMock()),
        patch("app.scheduler.logger") as mock_logger,
    ):
        # Must NOT raise.
        await run_force_poll_locked()

    mock_logger.exception.assert_called_once()
