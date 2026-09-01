"""Per-target daily COUNT cap on Phase-1 triage calls (#930).

Phase 2 has had one since #6; Phase 1 had only DOLLAR rails, which bound the
bill but not the call volume. These tests pin the two things that make the
cap a shared rail rather than a constant: what it counts, and how each caller
treats a count it cannot read.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.config import settings
from app.services.relevance.daily_cap import (
    phase1_backfill_allowance,
    phase1_calls_today,
    phase1_cap_reached,
)
from app.services.relevance.title_triage import PHASE1_PURPOSE
from tests.support.fake_backfill_db import backfill_supabase, cost_row

TARGET_ID = "t-1"
OTHER_TARGET = "t-2"


def _sb(costs: list[dict] | None = None):  # type: ignore[no-untyped-def]
    return backfill_supabase(jobs=[], scores=[], costs=costs)


@pytest.fixture(autouse=True)
def _cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "phase1_daily_cap", 10)
    monkeypatch.setattr(settings, "phase1_backfill_cap_fraction", 0.25)


class TestCounting:
    @pytest.mark.asyncio
    async def test_counts_only_this_targets_phase1_calls_today(self) -> None:
        yesterday = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        supabase = _sb(
            [
                cost_row(target_id=TARGET_ID, purpose=PHASE1_PURPOSE),
                cost_row(target_id=TARGET_ID, purpose=PHASE1_PURPOSE),
                # Another target's Phase-1 spend — a separate ceiling.
                cost_row(target_id=OTHER_TARGET, purpose=PHASE1_PURPOSE),
                # This target's PHASE-2 spend — a different cap entirely.
                cost_row(target_id=TARGET_ID, purpose="fit.job"),
                # This target's Phase-1 spend from before today's rollover.
                cost_row(
                    target_id=TARGET_ID, purpose=PHASE1_PURPOSE, created_at=yesterday
                ),
            ]
        )

        assert await phase1_calls_today(supabase, TARGET_ID) == 2

    @pytest.mark.asyncio
    async def test_unreadable_count_is_none_not_zero(self) -> None:
        supabase = _sb()
        supabase.table.side_effect = KeyError("llm_costs not routed")

        # None, not 0: collapsing the unknown here would hand every caller a
        # fail-open posture it did not choose.
        assert await phase1_calls_today(supabase, TARGET_ID) is None


class TestCapReached:
    @pytest.mark.asyncio
    async def test_true_once_the_cap_is_spent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "phase1_daily_cap", 2)
        supabase = _sb([cost_row(target_id=TARGET_ID, purpose=PHASE1_PURPOSE)])

        assert await phase1_cap_reached(supabase, TARGET_ID) is False  # precondition
        supabase._llm_costs.rows.append(cost_row(target_id=TARGET_ID, purpose=PHASE1_PURPOSE))
        assert await phase1_cap_reached(supabase, TARGET_ID) is True

    @pytest.mark.asyncio
    async def test_cap_of_zero_disables_the_rail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "phase1_daily_cap", 0)
        supabase = _sb([cost_row(target_id=TARGET_ID, purpose=PHASE1_PURPOSE) for _ in range(50)])

        assert await phase1_cap_reached(supabase, TARGET_ID) is False
        # And it never even reads the counter.
        assert supabase._llm_costs.count_reads == 0

    @pytest.mark.asyncio
    async def test_unreadable_count_fails_open_for_ingestion(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A Phase-1 stall PAUSES admission (#285/#294), so a transient
        count-read blip must not stop new listings entering the catalog."""
        monkeypatch.setattr(settings, "phase1_daily_cap", 1)
        supabase = _sb([cost_row(target_id=TARGET_ID, purpose=PHASE1_PURPOSE) for _ in range(5)])
        # Precondition: with a readable count this WOULD be capped.
        assert await phase1_cap_reached(supabase, TARGET_ID) is True

        supabase.table.side_effect = KeyError("llm_costs not routed")
        assert await phase1_cap_reached(supabase, TARGET_ID) is False


class TestBackfillAllowance:
    @pytest.mark.asyncio
    async def test_bounded_by_the_fraction_on_an_untouched_day(self) -> None:
        supabase = _sb()
        # cap 10, fraction 0.25 → 2, not 10.
        assert await phase1_backfill_allowance(supabase, TARGET_ID) == 2

    @pytest.mark.asyncio
    async def test_bounded_by_what_the_day_has_left(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "phase1_backfill_cap_fraction", 1.0)
        supabase = _sb([cost_row(target_id=TARGET_ID, purpose=PHASE1_PURPOSE) for _ in range(9)])

        assert await phase1_backfill_allowance(supabase, TARGET_ID) == 1

    @pytest.mark.asyncio
    async def test_never_negative_when_the_day_is_overspent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "phase1_backfill_cap_fraction", 1.0)
        supabase = _sb([cost_row(target_id=TARGET_ID, purpose=PHASE1_PURPOSE) for _ in range(25)])

        assert await phase1_backfill_allowance(supabase, TARGET_ID) == 0

    @pytest.mark.asyncio
    async def test_cap_of_zero_means_unbounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "phase1_daily_cap", 0)
        assert await phase1_backfill_allowance(_sb(), TARGET_ID) is None

    @pytest.mark.asyncio
    async def test_unreadable_count_fails_closed_for_the_backfill(self) -> None:
        supabase = _sb()
        assert await phase1_backfill_allowance(supabase, TARGET_ID) == 2  # precondition

        supabase.table.side_effect = KeyError("llm_costs not routed")
        # Opposite posture to ingestion, on purpose: a backfill that does not
        # run costs a user nothing they had yesterday.
        assert await phase1_backfill_allowance(supabase, TARGET_ID) == 0
