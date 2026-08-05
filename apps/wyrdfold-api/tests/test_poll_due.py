"""Tests for the due-source filter and the cron-driven poll endpoint."""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.poller import (
    DEFAULT_POLL_INTERVAL_MINUTES,
    filter_due_sources,
    poll_due_sources,
)


def _src(
    *,
    last_polled_at: str | None = None,
    poll_interval_minutes: int | None = 240,
    enabled: bool = True,
    company: str = "Test Co",
) -> dict[str, Any]:
    return {
        "id": f"src-{company}",
        "company_name": company,
        "board_token": company.lower(),
        "provider": "greenhouse",
        "enabled": enabled,
        "last_polled_at": last_polled_at,
        "poll_interval_minutes": poll_interval_minutes,
    }


def test_never_polled_is_due() -> None:
    src = _src(last_polled_at=None)
    assert filter_due_sources([src]) == [src]


def test_recently_polled_is_not_due() -> None:
    just_now = datetime.now(UTC).isoformat()
    src = _src(last_polled_at=just_now, poll_interval_minutes=240)
    assert filter_due_sources([src]) == []


def test_polled_past_interval_is_due() -> None:
    long_ago = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
    src = _src(last_polled_at=long_ago, poll_interval_minutes=240)
    assert filter_due_sources([src]) == [src]


def test_per_source_interval_honored() -> None:
    """A source with a tight 30-min interval should be due after 35 min,
    while a 4-hour source should not."""
    thirty_five_min_ago = (datetime.now(UTC) - timedelta(minutes=35)).isoformat()
    fast = _src(
        last_polled_at=thirty_five_min_ago,
        poll_interval_minutes=30,
        company="Fast",
    )
    slow = _src(
        last_polled_at=thirty_five_min_ago,
        poll_interval_minutes=240,
        company="Slow",
    )
    due = filter_due_sources([fast, slow])
    assert due == [fast]


def test_null_interval_falls_back_to_default() -> None:
    """Forward-compat: rows that predate the interval column shouldn't
    silently never get polled."""
    long_ago = (
        datetime.now(UTC) - timedelta(minutes=DEFAULT_POLL_INTERVAL_MINUTES + 5)
    ).isoformat()
    src = _src(last_polled_at=long_ago, poll_interval_minutes=None)
    assert filter_due_sources([src]) == [src]


def test_unparseable_timestamp_treated_as_never_polled() -> None:
    """A garbage timestamp shouldn't cause a row to be skipped forever."""
    src = _src(last_polled_at="not-a-date", poll_interval_minutes=60)
    assert filter_due_sources([src]) == [src]


def test_z_suffix_iso_timestamp_parses() -> None:
    """Supabase returns ISO timestamps with a 'Z' suffix; ensure we
    handle both 'Z' and '+00:00' forms identically."""
    long_ago = "2020-01-01T00:00:00Z"
    src = _src(last_polled_at=long_ago, poll_interval_minutes=60)
    assert filter_due_sources([src]) == [src]


# ---- end-to-end: poll_due_sources with mocked supabase ---------------------


def _supabase_returning(rows: list[dict[str, Any]]) -> MagicMock:
    """Build a Supabase-table-builder mock that returns ``rows`` from the
    paginated .select(...).eq(...).order(...).range(...).execute() chain
    (self-chaining, so filter/order/range in any arrangement resolve to the
    same terminal response). Keep rows under the 500-row page size or the
    pagination loop in ``_read_enabled_sources`` would request page 2 of the
    same canned response forever."""
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


@pytest.mark.asyncio
async def test_poll_due_sources_skips_when_nothing_due() -> None:
    just_now = datetime.now(UTC).isoformat()
    supabase = _supabase_returning([_src(last_polled_at=just_now, poll_interval_minutes=240)])
    with (
        patch("app.services.poller._latest_optimized", new_callable=AsyncMock) as get_opt,
        patch("app.services.poller._poll_one_source") as poll_one,
    ):
        get_opt.return_value = None
        result = await poll_due_sources(supabase)

    assert result.sources_polled == 0
    poll_one.assert_not_called()


@pytest.mark.asyncio
async def test_poll_due_sources_polls_only_due_rows() -> None:
    long_ago = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
    just_now = datetime.now(UTC).isoformat()

    due = _src(last_polled_at=long_ago, poll_interval_minutes=240, company="Due Co")
    fresh = _src(last_polled_at=just_now, poll_interval_minutes=240, company="Fresh Co")
    supabase = _supabase_returning([due, fresh])

    with (
        patch("app.services.poller._latest_optimized", new_callable=AsyncMock) as get_opt,
        # Cycle-level prefetch — irrelevant to the due-filter under test.
        patch("app.services.poller._active_targets", new_callable=AsyncMock, return_value=[]),
        patch("app.services.poller._poll_one_source", new_callable=AsyncMock) as poll_one,
    ):
        get_opt.return_value = None
        poll_one.return_value = {
            "polled": True,
            "new": 2,
            "updated": 1,
            "archived": 0,
            "error": None,
        }
        result = await poll_due_sources(supabase)

    assert poll_one.await_count == 1
    assert poll_one.await_args is not None
    polled_source = poll_one.await_args.args[0]
    assert polled_source["company_name"] == "Due Co"
    assert result.sources_polled == 1
    assert result.new_jobs == 2
    assert result.updated_jobs == 1


@pytest.mark.asyncio
async def test_poll_due_sources_aggregates_errors() -> None:
    long_ago = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
    supabase = _supabase_returning(
        [
            _src(last_polled_at=long_ago, company="A"),
            _src(last_polled_at=long_ago, company="B"),
        ]
    )

    with (
        patch("app.services.poller._latest_optimized", new_callable=AsyncMock) as get_opt,
        # Cycle-level prefetch — irrelevant to the error aggregation under test.
        patch("app.services.poller._active_targets", new_callable=AsyncMock, return_value=[]),
        patch("app.services.poller._poll_one_source", new_callable=AsyncMock) as poll_one,
    ):
        get_opt.return_value = None
        poll_one.side_effect = [
            {"polled": True, "new": 0, "updated": 0, "archived": 0, "error": None},
            {
                "polled": False,
                "new": 0,
                "updated": 0,
                "archived": 0,
                "error": "B: poll failed",
            },
        ]
        result = await poll_due_sources(supabase)

    assert result.sources_polled == 1
    assert result.errors == ["B: poll failed"]


@pytest.mark.asyncio
async def test_poll_due_sources_partial_progress_survives_cancellation() -> None:
    """The caller-owned ``progress`` accumulator must hold every FINISHED
    source's counts when the cycle is cancelled mid-gather (the scheduler's
    watchdog abort) — accumulation happens per-worker as each source lands,
    not after the gather (which a cancel would wipe). Found live 2026-08-05:
    overnight watchdog aborts reported nothing and never invalidated the
    list cache despite ~75 sources/cycle finishing."""
    import asyncio

    from app.models.schemas import PollResult

    long_ago = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
    supabase = _supabase_returning(
        [
            _src(last_polled_at=long_ago, company="Fast"),
            _src(last_polled_at=long_ago, company="Wedged"),
        ]
    )

    async def _fast_then_hang(source: dict, *a: object, **k: object) -> dict:
        if source["company_name"] == "Fast":
            return {"polled": True, "new": 3, "updated": 1, "archived": 0, "error": None}
        await asyncio.Event().wait()  # the 429-storm board that never returns
        raise AssertionError("unreachable")

    progress = PollResult(sources_polled=0, new_jobs=0, updated_jobs=0, archived_jobs=0, errors=[])
    with (
        patch("app.services.poller._latest_optimized", new_callable=AsyncMock, return_value=None),
        patch("app.services.poller._active_targets", new_callable=AsyncMock, return_value=[]),
        patch("app.services.poller._poll_one_source", _fast_then_hang),
    ):
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(poll_due_sources(supabase, progress=progress), timeout=0.3)

    assert progress.sources_polled == 1
    assert progress.new_jobs == 3
    assert progress.updated_jobs == 1


@pytest.mark.asyncio
async def test_backfill_runs_even_when_nothing_due(monkeypatch: pytest.MonkeyPatch) -> None:
    """#285 regression: the untagged-backlog sweep must run every cycle —
    including one where no source is due. It's independent of source polling and
    sits BEFORE the ``not due`` early-exit, so a quiet cycle still drains it."""
    from app.config import settings as live_settings

    monkeypatch.setattr(live_settings, "qualification_enabled", True)
    monkeypatch.setattr(live_settings, "qualification_backfill_batch", 50)

    just_now = datetime.now(UTC).isoformat()
    supabase = _supabase_returning([_src(last_polled_at=just_now, poll_interval_minutes=240)])

    with (
        patch("app.services.poller._latest_optimized", new_callable=AsyncMock, return_value=None),
        patch("app.services.poller._poll_one_source") as poll_one,
        patch("app.services.poller._backfill_qualify_stale", new_callable=AsyncMock) as backfill,
    ):
        result = await poll_due_sources(supabase)

    assert result.sources_polled == 0
    poll_one.assert_not_called()  # nothing was due to poll
    backfill.assert_awaited_once()  # ...but the sweep still ran
    assert backfill.await_args is not None
    assert backfill.await_args.args[1] == 50  # with the configured batch


# ---- per-cycle source cap (#514 residual) ----------------------------------


def _three_due_sources() -> list[dict[str, Any]]:
    """Three due sources, deliberately listed in NON-overdue order so the
    cap's most-overdue-first sort (never-polled at the very front) is
    observable, not an accident of input order."""
    ten_h = (datetime.now(UTC) - timedelta(hours=10)).isoformat()
    six_h = (datetime.now(UTC) - timedelta(hours=6)).isoformat()
    return [
        _src(last_polled_at=six_h, poll_interval_minutes=240, company="Newer Co"),
        _src(last_polled_at=ten_h, poll_interval_minutes=240, company="Oldest Co"),
        _src(last_polled_at=None, poll_interval_minutes=240, company="Never Co"),
    ]


@pytest.mark.asyncio
async def test_poll_due_sources_caps_batch_most_overdue_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With more due sources than ``poll_max_sources_per_cycle``, only the cap
    is polled this tick — and it's the MOST overdue slice (never-polled first,
    then oldest ``last_polled_at``), so a backlog drains oldest-first instead
    of re-attempting the whole fleet and tripping the cycle watchdog."""
    from app.config import settings as live_settings

    monkeypatch.setattr(live_settings, "poll_max_sources_per_cycle", 2)
    supabase = _supabase_returning(_three_due_sources())

    with (
        patch("app.services.poller._latest_optimized", new_callable=AsyncMock, return_value=None),
        patch("app.services.poller._active_targets", new_callable=AsyncMock, return_value=[]),
        patch("app.services.poller._poll_one_source", new_callable=AsyncMock) as poll_one,
    ):
        poll_one.return_value = {
            "polled": True,
            "new": 0,
            "updated": 0,
            "archived": 0,
            "error": None,
        }
        result = await poll_due_sources(supabase)

    assert poll_one.await_count == 2
    polled = {call.args[0]["company_name"] for call in poll_one.await_args_list}
    assert polled == {"Never Co", "Oldest Co"}  # the 6h-overdue one waits a tick
    assert result.sources_polled == 2


@pytest.mark.asyncio
async def test_poll_due_sources_cap_zero_is_unbounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``poll_max_sources_per_cycle=0`` keeps the legacy poll-everything-due
    behavior."""
    from app.config import settings as live_settings

    monkeypatch.setattr(live_settings, "poll_max_sources_per_cycle", 0)
    supabase = _supabase_returning(_three_due_sources())

    with (
        patch("app.services.poller._latest_optimized", new_callable=AsyncMock, return_value=None),
        patch("app.services.poller._active_targets", new_callable=AsyncMock, return_value=[]),
        patch("app.services.poller._poll_one_source", new_callable=AsyncMock) as poll_one,
    ):
        poll_one.return_value = {
            "polled": True,
            "new": 0,
            "updated": 0,
            "archived": 0,
            "error": None,
        }
        result = await poll_due_sources(supabase)

    assert poll_one.await_count == 3
    assert result.sources_polled == 3
