"""Per-source poll budget (2026-08-05).

The cycle watchdog + cap bound the CYCLE; the budget bounds ONE SOURCE. A
giant board (workday tenant, hundreds of 429-throttled detail fetches) must
not occupy a POLL_CONCURRENCY slot until the watchdog kills every in-flight
source — observed live: every overnight tick aborted at 1200s with ~75 of
250 sources finished. On expiry the source is cancelled (archive pass never
reached → a partial fetch can't mass-archive), stamped ``last_polled_at``
(rotates to the back of the most-overdue-first queue), and the cycle keeps
polling.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.services.poller as poller_module
from app.models.schemas import PollResult
from app.services.poller import _poll_one_source_budgeted, poll_due_sources


def _src(*, company: str, last_polled_at: str | None = None) -> dict[str, Any]:
    return {
        "id": f"src-{company}",
        "company_name": company,
        "board_token": company.lower(),
        "provider": "greenhouse",
        "enabled": True,
        "last_polled_at": last_polled_at,
        "poll_interval_minutes": 240,
    }


def _supabase_returning(rows: list[dict[str, Any]]) -> MagicMock:
    """Same shape as test_poll_due's harness: ``poll_db_read`` falls back to
    the sync-in-thread path in tests (no async pool), so ``execute`` must be
    a SYNC mock returning the response object. Self-chaining so the paginated
    .eq().order().range() read resolves to the same response (keep rows under
    the 500-row page size)."""
    table = MagicMock()
    chain = MagicMock()
    response = MagicMock()
    response.data = rows
    chain.eq.return_value = chain
    chain.order.return_value = chain
    chain.range.return_value = chain
    chain.execute.return_value = response
    table.select.return_value = chain
    supabase = MagicMock()
    supabase.table.return_value = table
    return supabase


_FAST_SUMMARY = {"polled": True, "new": 3, "updated": 1, "archived": 0, "error": None}


async def _fast_then_hang(source: dict, *a: object, **k: object) -> dict:
    if source["company_name"] == "Fast":
        return dict(_FAST_SUMMARY)
    await asyncio.Event().wait()  # the giant 429-storm board
    raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_budget_cancels_wedged_source_and_cycle_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wedged board is cancelled at the budget; the cycle COMPLETES with
    the fast source counted, no error string (no ingestion-health alarm
    noise), and the wedged board's row stamped so it rotates to the back of
    the due queue."""
    monkeypatch.setattr(poller_module.settings, "poll_source_budget_seconds", 0.2)
    long_ago = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
    supabase = _supabase_returning(
        [_src(company="Fast", last_polled_at=long_ago), _src(company="Wedged")]
    )

    with (
        patch("app.services.poller._latest_optimized", new_callable=AsyncMock, return_value=None),
        patch("app.services.poller._active_targets", new_callable=AsyncMock, return_value=[]),
        patch("app.services.poller._poll_one_source", _fast_then_hang),
        patch("app.services.poller.poll_db_write", new_callable=AsyncMock) as stamp,
    ):
        # The outer bound proves the CYCLE is no longer hostage to one board:
        # without the per-source budget this would hang until the watchdog.
        result = await asyncio.wait_for(poll_due_sources(supabase), timeout=3)

    assert result.sources_polled == 1  # only Fast completed a real poll
    assert result.new_jobs == 3
    assert result.errors == []  # a budgeted-out board is NOT an error
    # Other cycle steps (lifecycle sweeps) also route through poll_db_write —
    # filter to the budget stamp by its label.
    stamps = [
        c for c in stamp.await_args_list if c.kwargs.get("label") == "poll budget stamp Wedged"
    ]
    assert len(stamps) == 1


@pytest.mark.asyncio
async def test_budget_zero_disables_per_source_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0 disables the budget (pre-budget behavior): the wedged board hangs
    the cycle — proven by the OUTER timeout firing — while the fast source's
    counts still land in the caller-owned progress accumulator."""
    monkeypatch.setattr(poller_module.settings, "poll_source_budget_seconds", 0)
    long_ago = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
    supabase = _supabase_returning(
        [_src(company="Fast", last_polled_at=long_ago), _src(company="Wedged")]
    )

    progress = PollResult(sources_polled=0, new_jobs=0, updated_jobs=0, archived_jobs=0, errors=[])
    with (
        patch("app.services.poller._latest_optimized", new_callable=AsyncMock, return_value=None),
        patch("app.services.poller._active_targets", new_callable=AsyncMock, return_value=[]),
        patch("app.services.poller._poll_one_source", _fast_then_hang),
        patch("app.services.poller.poll_db_write", new_callable=AsyncMock) as stamp,
    ):
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(poll_due_sources(supabase, progress=progress), timeout=0.4)

    assert progress.sources_polled == 1  # Fast landed before the (outer) cancel
    budget_stamps = [
        c
        for c in stamp.await_args_list
        if str(c.kwargs.get("label", "")).startswith("poll budget stamp")
    ]
    assert budget_stamps == []  # no budget machinery engaged


@pytest.mark.asyncio
async def test_budget_stamp_failure_is_nonfatal(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed stamp write must not raise out of the worker — the board just
    stays at the queue front and the next cycle re-attempts it."""
    monkeypatch.setattr(poller_module.settings, "poll_source_budget_seconds", 0.1)

    async def _hang(*_a: object, **_k: object) -> dict:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    with (
        patch("app.services.poller._poll_one_source", _hang),
        patch(
            "app.services.poller.poll_db_write",
            new_callable=AsyncMock,
            side_effect=RuntimeError("db down"),
        ),
    ):
        summary = await _poll_one_source_budgeted(
            _src(company="Wedged"),
            MagicMock(),
            None,
            active_targets=[],
            stage3_users=None,
        )

    assert summary["budget_exhausted"] is True
    assert summary["polled"] is False
    assert summary["error"] is None


@pytest.mark.asyncio
async def test_budget_expiry_summary_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """The expiry summary is accumulator-compatible: not polled, zero counts,
    no error string, and carries the ``budget_exhausted`` marker."""
    monkeypatch.setattr(poller_module.settings, "poll_source_budget_seconds", 0.1)

    async def _hang(*_a: object, **_k: object) -> dict:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    with (
        patch("app.services.poller._poll_one_source", _hang),
        patch("app.services.poller.poll_db_write", new_callable=AsyncMock) as stamp,
    ):
        summary = await _poll_one_source_budgeted(
            _src(company="Wedged"),
            MagicMock(),
            None,
            active_targets=[],
            stage3_users=None,
        )

    assert summary == {
        "polled": False,
        "new": 0,
        "updated": 0,
        "archived": 0,
        "error": None,
        "budget_exhausted": True,
    }
    stamp.assert_awaited_once()
    # The stamp touches ONLY last_polled_at — no job_count / failure-counter
    # reset, which would falsely claim a clean poll.
    builder = stamp.await_args.args[1]
    recorder = MagicMock()
    builder(recorder)
    update_payload = recorder.table.return_value.update.call_args.args[0]
    assert set(update_payload.keys()) == {"last_polled_at"}


@pytest.mark.asyncio
async def test_fast_source_passes_through_unbudgeted_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source that finishes inside the budget returns its real summary
    untouched — the budget wrapper is transparent on the happy path."""
    monkeypatch.setattr(poller_module.settings, "poll_source_budget_seconds", 5)

    async def _fast(*_a: object, **_k: object) -> dict:
        return dict(_FAST_SUMMARY)

    with (
        patch("app.services.poller._poll_one_source", _fast),
        patch("app.services.poller.poll_db_write", new_callable=AsyncMock) as stamp,
    ):
        summary = await _poll_one_source_budgeted(
            _src(company="Fast"),
            MagicMock(),
            None,
            active_targets=[],
            stage3_users=None,
        )

    assert summary == _FAST_SUMMARY
    stamp.assert_not_awaited()


# ---- enabled-sources pagination (2026-08-05) --------------------------------
# PostgREST silently clamps un-ranged selects at db-max-rows (~1,000 hosted).
# Found live: 3,676 enabled sources, the cycle read returned exactly 1,000 —
# 1,144 never-polled catalog rows sat outside the window and the backlog
# froze. The read must page past the clamp, ordered by PK for stability.


@pytest.mark.asyncio
async def test_read_enabled_sources_stitches_pages_past_the_clamp() -> None:
    """1,050 enabled rows must come back complete (500+500+50), not clamped
    at the first page — the exact silent-truncation failure from prod."""
    from app.services.poller import _read_enabled_sources

    rows = [{"id": f"src-{i:04d}"} for i in range(1050)]
    calls: list[tuple[int, int]] = []

    def _rpc_chain(offset: int, end: int) -> MagicMock:
        resp = MagicMock()
        resp.data = rows[offset : end + 1]
        chain = MagicMock()
        chain.execute.return_value = resp
        return chain

    table = MagicMock()
    chain = MagicMock()
    chain.eq.return_value = chain
    chain.order.return_value = chain

    def _range(start: int, end: int) -> MagicMock:
        calls.append((start, end))
        return _rpc_chain(start, end)

    chain.range.side_effect = _range
    table.select.return_value = chain
    supabase = MagicMock()
    supabase.table.return_value = table

    out = await _read_enabled_sources(supabase)

    assert len(out) == 1050
    assert [r["id"] for r in out] == [f"src-{i:04d}" for i in range(1050)]
    assert calls == [(0, 499), (500, 999), (1000, 1499)]


@pytest.mark.asyncio
async def test_read_enabled_sources_orders_by_pk_for_stable_pages() -> None:
    """Pages must be ordered by primary key — heap-order pagination can skip
    or duplicate rows across pages when updates relocate tuples mid-read."""
    from app.services.poller import _read_enabled_sources

    table = MagicMock()
    chain = MagicMock()
    resp = MagicMock()
    resp.data = [{"id": "only"}]
    chain.eq.return_value = chain
    chain.order.return_value = chain
    chain.range.return_value = chain
    chain.execute.return_value = resp
    table.select.return_value = chain
    supabase = MagicMock()
    supabase.table.return_value = table

    out = await _read_enabled_sources(supabase)

    assert out == [{"id": "only"}]
    chain.order.assert_called_with("id")
