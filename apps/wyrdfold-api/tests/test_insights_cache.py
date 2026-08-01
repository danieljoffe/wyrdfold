"""Router-level result cache for the /insights endpoints (PERF-2, 2026-07-16).

The three /insights handlers recompute heavy period-windowed aggregations on
every request (historically 9-41s), and the Trends UI toggles period across
three tabs — so one visit re-ran them several times. A per-(user, kind, period)
TTL cache collapses that. These pin the cache behaviour: a repeat request is
served without recomputing; user / period / kind are distinct keys; and the
empty-targets short-circuit is never cached (so adding a first target shows up
immediately).

Handlers are `async def` since #57 slice 3 (DB reads run on the async user
client), so the stubs are AsyncMocks and the calls are awaited.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.cache import insights_cache
from app.routers import insights as mod

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clear_cache():
    insights_cache.invalidate()
    yield
    insights_cache.invalidate()


async def test_pipeline_second_call_is_served_from_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "_user_target_ids", AsyncMock(return_value={"t1"}))
    compute = AsyncMock(return_value="PIPELINE_RESULT")
    monkeypatch.setattr(mod, "compute_pipeline", compute)
    sb = MagicMock()

    first = await mod.pipeline_insights(period="30d", user_id="u1", supabase=sb)
    second = await mod.pipeline_insights(period="30d", user_id="u1", supabase=sb)

    assert first == "PIPELINE_RESULT"
    assert second == "PIPELINE_RESULT"
    compute.assert_called_once()  # second request hit the cache


async def test_distinct_period_and_user_and_kind_do_not_share(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mod, "_user_target_ids", AsyncMock(return_value={"t1"}))
    pipeline = AsyncMock(return_value="P")
    targets = AsyncMock(return_value="T")
    monkeypatch.setattr(mod, "compute_pipeline", pipeline)
    monkeypatch.setattr(mod, "compute_targets", targets)
    sb = MagicMock()

    await mod.pipeline_insights(period="30d", user_id="u1", supabase=sb)
    await mod.pipeline_insights(period="7d", user_id="u1", supabase=sb)  # different period
    await mod.pipeline_insights(period="30d", user_id="u2", supabase=sb)  # different user
    # Different KIND on the same (user, period) must not collide with pipeline's key.
    await mod.target_insights(period="30d", user_id="u1", supabase=sb)

    assert pipeline.call_count == 3  # 3 distinct (user, period) pipeline keys
    assert targets.call_count == 1  # targets kind computed independently


async def test_empty_targets_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    # No targets → empty short-circuit before the cache. It must NOT be cached:
    # a user who then adds their first target should see real data immediately,
    # not a stale empty payload for a full TTL.
    calls = {"n": 0}

    async def _targets(*_a, **_k):
        calls["n"] += 1
        return set() if calls["n"] == 1 else {"t1"}

    monkeypatch.setattr(mod, "_user_target_ids", _targets)
    compute = AsyncMock(return_value="PIPELINE_RESULT")
    monkeypatch.setattr(mod, "compute_pipeline", compute)
    sb = MagicMock()

    first = await mod.pipeline_insights(period="30d", user_id="u1", supabase=sb)
    second = await mod.pipeline_insights(period="30d", user_id="u1", supabase=sb)

    # First call: no targets → empty, compute never ran. Second call: now has a
    # target → real compute runs (the empty result was not cached).
    assert first == mod._empty_pipeline()
    assert second == "PIPELINE_RESULT"
    compute.assert_called_once()


async def test_invalidate_forces_recompute(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "_user_target_ids", AsyncMock(return_value={"t1"}))
    compute = AsyncMock(return_value="PIPELINE_RESULT")
    monkeypatch.setattr(mod, "compute_pipeline", compute)
    sb = MagicMock()

    await mod.pipeline_insights(period="30d", user_id="u1", supabase=sb)
    insights_cache.invalidate()
    await mod.pipeline_insights(period="30d", user_id="u1", supabase=sb)

    assert compute.call_count == 2  # cache cleared → recomputed
