"""Near-miss mining over the Phase-1 rejection store (compute + route).

The compute tests drive the same stateful PostgREST-faithful fake the
rejection-store tests use, so the filters are exercised against real row
storage — a poisoned row (wrong profile_version, settled confidence,
stale judgment) must provably exist AND provably not surface.

The route tests pin the authorization shape: the service-role read is
bounded by targets resolved through the CALLER's membership rows
(``user_targets``), never by the catalog-wide-readable ``targets`` table
alone (its RLS is ``qual: true`` — trusting it would leak every user's
near-miss signals to every caller).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from app.services.relevance.near_miss import (
    NEAR_MISS_CONFIDENCE_CEILING,
    NEAR_MISS_LIMIT_PER_TARGET,
    NEAR_MISS_WINDOW_DAYS,
    compute_near_misses,
)
from tests.support.fake_phase1_store import phase1_store_supabase


def _iso(days_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()


def _seed(
    supabase: MagicMock,
    *,
    target_id: str = "tgt-1",
    profile_version: int = 1,
    title: str,
    confidence: int | None,
    days_ago: float = 1.0,
) -> None:
    supabase._phase1_rejections.rows[(target_id, profile_version, title)] = {
        "target_id": target_id,
        "profile_version": profile_version,
        "title_norm": title,
        "confidence": confidence,
        "model": "deepseek-v3-2",
        "judged_at": _iso(days_ago),
    }


@pytest.mark.asyncio
async def test_surfaces_only_low_confidence_current_profile_recent_rows():
    supabase = phase1_store_supabase()
    # The near-miss: shaky rejection, current profile, recent.
    _seed(supabase, title="staff platform engineer", confidence=60)
    # Poison set — each provably present, each must NOT surface:
    _seed(supabase, title="account executive", confidence=95)  # settled "no"
    _seed(supabase, title="old profile role", confidence=50, profile_version=2)  # other pv
    _seed(
        supabase,
        title="stale near miss",
        confidence=50,
        days_ago=NEAR_MISS_WINDOW_DAYS + 5,
    )  # outside window
    _seed(supabase, title="legacy no confidence", confidence=None)  # pre-confidence row
    assert len(supabase._phase1_rejections.rows) == 5  # all five really exist

    result = await compute_near_misses(supabase, [("tgt-1", "Platform Eng", 1)])

    assert len(result.targets) == 1
    got = result.targets[0]
    assert got.target_id == "tgt-1"
    assert got.label == "Platform Eng"
    assert [t.title for t in got.titles] == ["staff platform engineer"]
    assert got.titles[0].confidence == 60
    assert result.confidence_ceiling == NEAR_MISS_CONFIDENCE_CEILING
    assert result.window_days == NEAR_MISS_WINDOW_DAYS


@pytest.mark.asyncio
async def test_orders_shakiest_first_and_caps_per_target():
    supabase = phase1_store_supabase()
    for i in range(NEAR_MISS_LIMIT_PER_TARGET + 5):
        _seed(supabase, title=f"adjacent role {i:02d}", confidence=50 + (i % 25))

    result = await compute_near_misses(supabase, [("tgt-1", "T", 1)])

    titles = result.targets[0].titles
    assert len(titles) == NEAR_MISS_LIMIT_PER_TARGET  # capped, not dumped
    confidences = [t.confidence for t in titles]
    assert confidences == sorted(confidences)  # shakiest verdicts first


@pytest.mark.asyncio
async def test_targets_isolated_and_empty_target_still_listed():
    supabase = phase1_store_supabase()
    _seed(supabase, target_id="tgt-1", title="adjacent role", confidence=55)

    result = await compute_near_misses(
        supabase, [("tgt-1", "Has one", 1), ("tgt-2", "Has none", 1)]
    )

    by_id = {t.target_id: t for t in result.targets}
    assert [x.title for x in by_id["tgt-1"].titles] == ["adjacent role"]
    # tgt-2 appears with an empty list — the FE decides how to render
    # "nothing yet", the API doesn't silently drop the target.
    assert by_id["tgt-2"].titles == []


@pytest.mark.asyncio
async def test_read_failure_degrades_to_empty_for_that_target(caplog):
    supabase = MagicMock()
    supabase.table.side_effect = RuntimeError("connection refused")

    with caplog.at_level("WARNING"):
        result = await compute_near_misses(supabase, [("tgt-1", "T", 1)])

    assert result.targets[0].titles == []  # advisory, never load-bearing
    assert any("near-miss read failed" in r.message for r in caplog.records)


# ---- Route shape: membership-bounded, JWT-only ------------------------------


def test_route_is_jwt_gated_and_membership_bounded():
    """Static contract checks on the route wiring.

    The full request path is covered by the router's shared JWT gate
    (``dependencies=[Depends(verify_supabase_jwt)]`` on the APIRouter) —
    here we pin the two properties a refactor could silently drop:
    the endpoint resolves targets via ``_user_target_ids`` (membership,
    ``user_targets`` RLS) before any service-role read, and it never
    passes the caller's raw ``targets`` select as the boundary.
    """
    import inspect

    from app.routers import insights as insights_router

    src = inspect.getsource(insights_router.near_miss_insights)
    assert "_user_target_ids" in src  # membership resolution present
    assert "_member_target_rows" in src  # bounded row fetch, not a raw select
    # The target-row read must be bounded by the membership ids.
    helper_src = inspect.getsource(insights_router._member_target_rows)
    assert '.in_("id", sorted(target_ids))' in helper_src


@pytest.mark.asyncio
async def test_route_returns_empty_shape_for_user_with_no_targets():
    """A caller with no memberships gets the empty envelope, and the
    service client is never consulted (no unbounded fallback)."""
    from app.routers.insights import near_miss_insights

    caller = MagicMock()
    # user_targets membership read → no rows. The chain ends with TWO ``.eq``
    # calls since #842 (user_id + is_active) — mock the second link.
    caller.table.return_value.select.return_value.eq.return_value.eq.return_value.execute = (
        MagicMock(return_value=_awaitable(MagicMock(data=[])))
    )
    service = MagicMock()

    result = await near_miss_insights(user_id="u-near-miss-none", supabase=caller, service=service)
    assert result.targets == []
    service.table.assert_not_called()


def _awaitable(value: object):
    async def _coro():
        return value

    return _coro()
