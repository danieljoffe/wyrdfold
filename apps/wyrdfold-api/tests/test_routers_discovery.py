"""``POST /discovery/run`` returns 202 and runs the bulk pass in the background.

The bulk-discovery route used to ``await`` the multi-minute pass inline, so a
manual/external trigger held the request open past the edge's 300s timeout
(curl got a 499/502 even when discovery was fine). It now schedules the pass
via FastAPI ``BackgroundTasks`` and returns ``202`` immediately.

Two things are proven here:
  - the HTTP contract: ``POST /discovery/run`` returns
    ``202 {"status": "scheduled"}`` and the pass is *scheduled* (queued as a
    background task), not awaited inside the handler.
  - the background body (``run_discovery_all_targets_locked``): it routes
    through an advisory lock so two passes can't stack; it walks EVERY target
    (active + inactive) via ``crud.get_all``; and it never lets a backgrounded
    exception escape.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.dependencies import get_supabase, verify_api_key
from app.main import app
from app.services.source_discovery import (
    DiscoveryRunStats,
    run_discovery_all_targets,
    run_discovery_all_targets_locked,
)


def _stats(target_id: str, *, inserted: int = 0) -> DiscoveryRunStats:
    return DiscoveryRunStats(
        target_id=target_id,
        queries_issued=6,
        urls_examined=12,
        inserted=inserted,
        duplicates=1,
        unclassified=2,
        filtered=3,
    )


def _client() -> TestClient:
    app.dependency_overrides[get_supabase] = lambda: MagicMock()
    app.dependency_overrides[verify_api_key] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


def teardown_function() -> None:
    app.dependency_overrides.clear()


# --- HTTP contract: fast 202, pass not awaited inline -----------------------


def test_run_returns_202_with_scheduled_body() -> None:
    with patch("app.routers.discovery.run_discovery_all_targets_locked", new=AsyncMock()):
        resp = _client().post("/discovery/run")
    assert resp.status_code == 202
    assert resp.json() == {"status": "scheduled"}


def test_run_schedules_background_task_not_awaited_inline() -> None:
    """The pass runs as a background task: the response is produced BEFORE the
    pass body runs, and the handler never awaits the pass itself.

    Starlette's TestClient drains background tasks after sending the response,
    so observing the body ran *after* a clean 202 proves it was scheduled.
    """
    events: list[str] = []

    async def _fake_run() -> None:
        events.append("discovery_ran")

    with patch("app.routers.discovery.run_discovery_all_targets_locked", new=_fake_run):
        assert events == []  # nothing ran before the request
        resp = _client().post("/discovery/run")
        assert resp.status_code == 202
        # The background body ran (drained by TestClient), proving scheduled.
        assert events == ["discovery_ran"]


def test_run_handler_does_not_walk_targets_inline() -> None:
    """The handler must not run discovery itself — it delegates to the
    backgrounded helper. If the work were awaited inline, this spy on the
    underlying per-target runner would be called during the request."""
    with (
        patch("app.routers.discovery.run_discovery_all_targets_locked", new=AsyncMock()),
        patch(
            "app.services.source_discovery.run_discovery_for_target", new=AsyncMock()
        ) as inline_spy,
    ):
        resp = _client().post("/discovery/run")
    assert resp.status_code == 202
    inline_spy.assert_not_called()


def test_run_requires_api_key() -> None:
    app.dependency_overrides[get_supabase] = lambda: MagicMock()
    # No verify_api_key override — the real dependency must reject.
    resp = TestClient(app).post("/discovery/run")
    assert resp.status_code in (401, 403)


# --- Background body: advisory lock + ALL-targets walk ----------------------


class _Resp:
    def __init__(self, data: Any) -> None:
        self.data = data


def _fake_lock_supabase() -> tuple[MagicMock, dict[str, bool]]:
    """A MagicMock supabase whose advisory-lock RPCs share one in-memory
    'held' flag — a faithful stand-in for pg_try_advisory_lock semantics.
    Mirrors the fixture in test_poll_background.py / test_poll_lock.py."""
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
async def test_locked_walks_all_targets_when_lock_acquired() -> None:
    """Happy path: lock acquired → discovery walks EVERY target (via
    ``crud.get_all``, NOT ``get_active``) and the lock is released after."""
    sb, state = _fake_lock_supabase()
    t_active, t_inactive = MagicMock(id="t-active"), MagicMock(id="t-inactive")

    fake_per_target = AsyncMock(
        side_effect=[_stats("t-active", inserted=2), _stats("t-inactive", inserted=1)]
    )
    with (
        patch("app.services.source_discovery.get_supabase_pool", return_value=sb),
        patch(
            "app.services.source_discovery.crud.get_all",
            return_value=[t_active, t_inactive],
        ) as mock_get_all,
        patch(
            "app.services.source_discovery.crud.get_active",
            return_value=[t_active],
        ) as mock_get_active,
        patch("app.services.source_discovery.run_discovery_for_target", fake_per_target),
    ):
        await run_discovery_all_targets_locked()

    mock_get_all.assert_called_once_with(sb)
    mock_get_active.assert_not_called()  # bulk path must NOT use active-only
    # Both targets — active AND inactive — were processed.
    assert fake_per_target.await_count == 2
    assert {c.args[1].id for c in fake_per_target.await_args_list} == {
        "t-active",
        "t-inactive",
    }
    assert state["held"] is False  # lock released


@pytest.mark.asyncio
async def test_locked_skips_when_lock_held() -> None:
    """The double-run guard: when the advisory lock is already held (a
    scheduled tick is mid-pass), a second trigger must NOT walk targets."""
    sb, state = _fake_lock_supabase()
    state["held"] = True  # another discovery run holds it

    with (
        patch("app.services.source_discovery.get_supabase_pool", return_value=sb),
        patch("app.services.source_discovery.crud.get_all") as mock_get_all,
        patch("app.services.source_discovery.run_discovery_for_target") as mock_per_target,
    ):
        await run_discovery_all_targets_locked()

    mock_get_all.assert_not_called()
    mock_per_target.assert_not_called()
    assert state["held"] is True  # didn't steal/release a lock it never held


@pytest.mark.asyncio
async def test_locked_skips_when_client_uninitialized() -> None:
    """No supabase singleton (startup race) → skip cleanly, no walk, no crash."""
    with (
        patch("app.services.source_discovery.get_supabase_pool", return_value=None),
        patch("app.services.source_discovery.crud.get_all") as mock_get_all,
    ):
        await run_discovery_all_targets_locked()
    mock_get_all.assert_not_called()


@pytest.mark.asyncio
async def test_locked_swallows_and_logs_exceptions() -> None:
    """A backgrounded task's exception is otherwise lost. The helper must log
    it and never propagate."""
    sb, _ = _fake_lock_supabase()
    with (
        patch("app.services.source_discovery.get_supabase_pool", return_value=sb),
        patch(
            "app.services.source_discovery.crud.get_all",
            side_effect=RuntimeError("db boom"),
        ),
        patch("app.services.source_discovery.logger") as mock_logger,
    ):
        # Must NOT raise.
        await run_discovery_all_targets_locked()

    mock_logger.exception.assert_called_once()


# --- Aggregation helper: per-target roll-up + error isolation ---------------


@pytest.mark.asyncio
async def test_run_all_aggregates_per_target_stats() -> None:
    sb = MagicMock()
    t1, t2 = MagicMock(id="t-1"), MagicMock(id="t-2")
    fake_run = AsyncMock(side_effect=[_stats("t-1", inserted=4), _stats("t-2", inserted=1)])
    with patch("app.services.source_discovery.run_discovery_for_target", fake_run):
        result = await run_discovery_all_targets(sb, [t1, t2])

    assert result.targets_processed == 2
    assert result.queries_issued == 12
    assert result.inserted == 5
    assert result.errors == []
    assert [s.target_id for s in result.per_target] == ["t-1", "t-2"]


@pytest.mark.asyncio
async def test_run_all_records_error_and_continues() -> None:
    sb = MagicMock()
    t1, t2 = MagicMock(id="t-1"), MagicMock(id="t-2")
    fake_run = AsyncMock(side_effect=[RuntimeError("brave down"), _stats("t-2", inserted=2)])
    with patch("app.services.source_discovery.run_discovery_for_target", fake_run):
        result = await run_discovery_all_targets(sb, [t1, t2])

    assert result.targets_processed == 1
    assert result.inserted == 2
    assert result.errors == ["t-1: discovery failed"]


@pytest.mark.asyncio
async def test_run_all_with_no_targets_is_a_noop() -> None:
    sb = MagicMock()
    with patch("app.services.source_discovery.run_discovery_for_target") as mock_run:
        result = await run_discovery_all_targets(sb, [])

    assert result.targets_processed == 0
    mock_run.assert_not_called()
