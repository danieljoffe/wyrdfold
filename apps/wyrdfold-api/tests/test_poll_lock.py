"""Poll advisory-lock guard — only one poll runs at a time.

These tests model the Postgres advisory lock with an in-memory holder so
they don't need a live database: ``try_poll_advisory_lock`` returns True
to the first caller and False to a second caller while the first still
holds it, exactly like ``pg_try_advisory_lock``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.scheduler import _run_scheduled_poll
from app.services.poll_lock import poll_advisory_lock


class _Resp:
    def __init__(self, data: Any) -> None:
        self.data = data


def _fake_lock_supabase() -> tuple[MagicMock, dict[str, bool]]:
    """A MagicMock supabase whose advisory-lock RPCs share one in-memory
    'held' flag — a faithful stand-in for pg_try_advisory_lock semantics."""
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
async def test_lock_grants_first_caller_and_releases() -> None:
    sb, state = _fake_lock_supabase()
    async with poll_advisory_lock(sb, 123) as acquired:
        assert acquired is True
        assert state["held"] is True
    # Released on context exit.
    assert state["held"] is False


@pytest.mark.asyncio
async def test_lock_blocks_second_concurrent_holder() -> None:
    """While one holder is inside the context, a second try gets False."""
    sb, state = _fake_lock_supabase()
    async with poll_advisory_lock(sb, 123) as first:
        assert first is True
        async with poll_advisory_lock(sb, 123) as second:
            # Second caller is locked out — it must NOT poll.
            assert second is False
        # Second exit must not release the lock it never held.
        assert state["held"] is True
    assert state["held"] is False


@pytest.mark.asyncio
async def test_acquire_failure_returns_false_not_raise() -> None:
    """A lock-RPC outage skips the tick rather than crashing the scheduler."""
    sb = MagicMock()
    sb.rpc.return_value.execute.side_effect = RuntimeError("db down")
    async with poll_advisory_lock(sb, 1) as acquired:
        assert acquired is False


# ---- POLLER_ASYNC_DB backend selection (#57) --------------------------------


class _AsyncLockClient:
    """Async-client stand-in: records rpc calls, async execute, no locking
    semantics needed — these tests only assert WHICH backend ran the RPC."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def rpc(self, name: str, params: dict[str, Any]) -> Any:
        self.calls.append((name, params))

        class _Handle:
            async def execute(self) -> _Resp:
                return _Resp(True)

        return _Handle()


@pytest.mark.asyncio
async def test_flag_on_lock_rpcs_use_async_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import poll_lock

    async_client = _AsyncLockClient()
    monkeypatch.setattr(poll_lock, "get_async_supabase", lambda: async_client)
    sync_sb, _ = _fake_lock_supabase()

    async with poll_advisory_lock(sync_sb, 42) as acquired:
        assert acquired is True

    assert [name for name, _ in async_client.calls] == [
        "try_poll_advisory_lock",
        "release_poll_advisory_lock",
    ]
    assert all(params == {"p_key": 42} for _, params in async_client.calls)
    sync_sb.rpc.assert_not_called()  # sync client never touched


@pytest.mark.asyncio
async def test_flag_on_without_async_client_falls_back_to_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import poll_lock

    monkeypatch.setattr(poll_lock, "get_async_supabase", lambda: None)
    sb, state = _fake_lock_supabase()

    async with poll_advisory_lock(sb, 7) as acquired:
        assert acquired is True
        assert state["held"] is True
    assert state["held"] is False


@pytest.mark.asyncio
async def test_scheduled_poll_runs_when_lock_acquired() -> None:
    """Happy path: lock acquired → poll_due_sources is called."""
    sb, _ = _fake_lock_supabase()
    poll_result = MagicMock(
        sources_polled=1, new_jobs=2, updated_jobs=0, archived_jobs=0, errors=[]
    )
    with (
        patch("app.scheduler.get_supabase_pool", return_value=sb),
        patch(
            "app.scheduler.poll_due_sources",
            new=AsyncMock(return_value=poll_result),
        ) as mock_poll,
        patch("app.scheduler.check_ingestion_health", new=AsyncMock()) as mock_health,
    ):
        await _run_scheduled_poll()

    mock_poll.assert_awaited_once_with(sb)
    mock_health.assert_awaited_once_with(sb)


@pytest.mark.asyncio
async def test_scheduled_poll_skips_when_lock_held() -> None:
    """The regression guard: when the advisory lock is already held, the
    scheduled poll must NOT call poll_due_sources (no double-poll) — but the
    health checks MUST still run (#350): a leaked lock previously silenced
    every alarm because they only ran inside the acquired branch, so the
    exact failure the leaked-lock alarm exists for also disabled it."""
    sb, state = _fake_lock_supabase()
    state["held"] = True  # someone else already polling

    with (
        patch("app.scheduler.get_supabase_pool", return_value=sb),
        patch("app.scheduler.poll_due_sources") as mock_poll,
        patch("app.scheduler.check_ingestion_health", new=AsyncMock()) as mock_health,
    ):
        await _run_scheduled_poll()

    mock_poll.assert_not_called()
    mock_health.assert_awaited_once_with(sb)
