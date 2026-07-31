"""Unit tests for the lazy fit-score refresh (E2).

Cover the two halves offline: the staleness filter (which links are stale vs the
current profile version) and the background refresh (recompute + re-stamp, the
concurrency re-check, is_active is never touched, one failure doesn't abort the
batch).
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from app.models.targets import JobTarget, ScoringProfile
from app.services.llm import provider_breaker
from app.services.llm.errors import LLMQuotaExhaustedError
from app.services.targets import fit_refresh


@pytest.fixture(autouse=True)
def _reset_breaker() -> object:
    """The provider-fatal latch is process-global; reset around each test."""
    provider_breaker.reset_for_tests()
    yield
    provider_breaker.reset_for_tests()


def _target(id: str = "t1") -> JobTarget:
    return JobTarget(
        id=id,
        label="Senior Engineer",
        scoring_profile=ScoringProfile(),
        app_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _rows_supabase(rows: list[dict]) -> MagicMock:
    """A client whose user_targets select returns ``rows``."""
    supabase = MagicMock()
    supabase.table.return_value.select.return_value.eq.return_value.execute.return_value = (
        MagicMock(data=rows)
    )
    return supabase


# ---- stale_target_ids -------------------------------------------------------


def test_stale_filter_selects_mismatched_and_null_markers() -> None:
    rows = [
        {"target_id": "fresh", "fit_score": 80, "fit_score_prose_doc_id": "p2"},  # current
        {"target_id": "stale-old", "fit_score": 70, "fit_score_prose_doc_id": "p1"},  # old ver
        {"target_id": "stale-null", "fit_score": 60, "fit_score_prose_doc_id": None},  # untracked
        {"target_id": "unscored", "fit_score": None, "fit_score_prose_doc_id": None},  # skip
    ]
    stale = fit_refresh.stale_target_ids(
        _rows_supabase(rows), user_id="u1", current_prose_doc_id="p2", limit=10
    )
    # Mismatched + null-marker scored rows are stale; the current + unscored are not.
    assert set(stale) == {"stale-old", "stale-null"}


def test_stale_filter_respects_limit() -> None:
    rows = [
        {"target_id": f"t{i}", "fit_score": 50, "fit_score_prose_doc_id": "old"} for i in range(10)
    ]
    stale = fit_refresh.stale_target_ids(
        _rows_supabase(rows), user_id="u1", current_prose_doc_id="p2", limit=3
    )
    assert len(stale) == 3  # capped


def test_stale_filter_disabled_when_limit_zero() -> None:
    supabase = MagicMock()
    stale = fit_refresh.stale_target_ids(
        supabase, user_id="u1", current_prose_doc_id="p2", limit=0
    )
    assert stale == []
    supabase.table.assert_not_called()  # no query at all


# ---- refresh_stale_fit_scores ----------------------------------------------


@pytest.fixture
def refresh_patches(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Patch the refresh collaborators; return recorders for assertions."""
    rec: dict = {"updates": [], "markers": {}, "scored": []}

    async def fake_resolve(supabase, llm, *, cost_supabase, user_id):  # type: ignore[no-untyped-def]
        return object(), "p2"  # (payload, current prose doc id)

    monkeypatch.setattr(fit_refresh, "resolve_current_payload", fake_resolve)
    monkeypatch.setattr(fit_refresh, "current_prose_doc_id", lambda _s, _u: "p2")
    monkeypatch.setattr(fit_refresh.crud, "get", lambda _s, tid: _target(tid))
    monkeypatch.setattr(
        fit_refresh.crud,
        "get_fit_score_prose_doc_id",
        lambda _s, *, user_id, target_id: rec["markers"].get(target_id, "p1"),
    )
    monkeypatch.setattr(fit_refresh.cost_log, "record", lambda *_a, **_k: None)

    async def fake_derive(llm, *, payload, target):  # type: ignore[no-untyped-def]
        rec["scored"].append(target.id)
        return MagicMock(fit_score=88, reasoning=f"fit for {target.id}"), MagicMock()

    monkeypatch.setattr(fit_refresh, "derive_fit_score", fake_derive)

    def fake_update(_s, *, user_id, target_id, fit_score, fit_score_reasoning, fit_score_prose_doc_id):  # type: ignore[no-untyped-def]
        rec["updates"].append(
            {"target_id": target_id, "fit_score": fit_score, "marker": fit_score_prose_doc_id}
        )

    monkeypatch.setattr(fit_refresh.crud, "update_fit_score", fake_update)
    return rec


@pytest.mark.asyncio
async def test_refresh_recomputes_and_restamps(refresh_patches: dict) -> None:
    n = await fit_refresh.refresh_stale_fit_scores(
        MagicMock(), MagicMock(), user_id="u1", target_ids=["a", "b"]
    )
    assert n == 2
    assert sorted(refresh_patches["scored"]) == ["a", "b"]
    # Each write carries the new score + the CURRENT version marker.
    assert all(u["fit_score"] == 88 and u["marker"] == "p2" for u in refresh_patches["updates"])
    assert {u["target_id"] for u in refresh_patches["updates"]} == {"a", "b"}


@pytest.mark.asyncio
async def test_refresh_rechecks_marker_and_skips_already_fresh(
    refresh_patches: dict,
) -> None:
    """A target already at the current version (a concurrent view refreshed it)
    is skipped before the LLM is spent."""
    refresh_patches["markers"]["b"] = "p2"  # b is already current
    n = await fit_refresh.refresh_stale_fit_scores(
        MagicMock(), MagicMock(), user_id="u1", target_ids=["a", "b"]
    )
    assert n == 1
    assert refresh_patches["scored"] == ["a"]  # b never scored (no double-spend)


@pytest.mark.asyncio
async def test_refresh_isolates_per_target_failure(
    refresh_patches: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def flaky(llm, *, payload, target):  # type: ignore[no-untyped-def]
        if target.id == "bad":
            raise RuntimeError("llm down")
        refresh_patches["scored"].append(target.id)
        return MagicMock(fit_score=88, reasoning="ok"), MagicMock()

    monkeypatch.setattr(fit_refresh, "derive_fit_score", flaky)
    n = await fit_refresh.refresh_stale_fit_scores(
        MagicMock(), MagicMock(), user_id="u1", target_ids=["bad", "good"]
    )
    assert n == 1  # good still refreshed
    assert refresh_patches["scored"] == ["good"]


@pytest.mark.asyncio
async def test_refresh_noop_when_no_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """No prose master (payload/version None) → nothing scored, nothing written."""

    async def no_payload(supabase, llm, *, cost_supabase, user_id):  # type: ignore[no-untyped-def]
        return None, None

    monkeypatch.setattr(fit_refresh, "resolve_current_payload", no_payload)
    updated = []
    monkeypatch.setattr(fit_refresh.crud, "update_fit_score", lambda *a, **k: updated.append(k))

    n = await fit_refresh.refresh_stale_fit_scores(
        MagicMock(), MagicMock(), user_id="u1", target_ids=["a"]
    )
    assert n == 0
    assert updated == []


# ---- provider-fatal breaker (the anti-churn guard) --------------------------


@pytest.mark.asyncio
async def test_provider_fatal_mid_batch_latches_and_aborts(
    refresh_patches: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 402 mid-batch latches the shared breaker and stops the remaining targets
    — no point hammering a provider that's out of credits."""
    scored: list[str] = []

    async def out_of_credits(llm, *, payload, target):  # type: ignore[no-untyped-def]
        scored.append(target.id)
        raise LLMQuotaExhaustedError(upstream_status=402)

    monkeypatch.setattr(fit_refresh, "derive_fit_score", out_of_credits)

    assert not provider_breaker.provider_fatal_active()
    n = await fit_refresh.refresh_stale_fit_scores(
        MagicMock(), MagicMock(), user_id="u1", target_ids=["a", "b", "c"]
    )
    assert n == 0
    assert scored == ["a"]  # aborted after the first fatal — b, c never attempted
    assert provider_breaker.provider_fatal_active()  # breaker latched


@pytest.mark.asyncio
async def test_provider_fatal_from_resolve_latches(monkeypatch: pytest.MonkeyPatch) -> None:
    """A provider-fatal error from the payload resolve latches the breaker + bails
    before walking any targets."""

    async def resolve_402(supabase, llm, *, cost_supabase, user_id):  # type: ignore[no-untyped-def]
        raise LLMQuotaExhaustedError(upstream_status=402)

    monkeypatch.setattr(fit_refresh, "resolve_current_payload", resolve_402)

    n = await fit_refresh.refresh_stale_fit_scores(
        MagicMock(), MagicMock(), user_id="u1", target_ids=["a"]
    )
    assert n == 0
    assert provider_breaker.provider_fatal_active()


# ---- refresh_stale_for_user (the /mine background entrypoint) ----------------


@pytest.mark.asyncio
async def test_refresh_for_user_skips_at_the_door_when_breaker_latched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Breaker latched → NO staleness query, NO refresh. This is what stops a
    credits outage from making every /mine re-run the sweep + reschedule doom."""
    provider_breaker.trip_provider_fatal(LLMQuotaExhaustedError(upstream_status=402))
    touched = {"prose": 0, "stale": 0}

    def prose_spy(_s, _u):  # type: ignore[no-untyped-def]
        touched["prose"] += 1
        return "p2"

    def stale_spy(*_a, **_k):  # type: ignore[no-untyped-def]
        touched["stale"] += 1
        return []

    monkeypatch.setattr(fit_refresh, "current_prose_doc_id", prose_spy)
    monkeypatch.setattr(fit_refresh, "stale_target_ids", stale_spy)

    await fit_refresh.refresh_stale_for_user(MagicMock(), MagicMock(), user_id="u1")
    assert touched == {"prose": 0, "stale": 0}  # short-circuited at the breaker


@pytest.mark.asyncio
async def test_refresh_for_user_delegates_when_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not latched + a profile + stale targets → delegates to the batch refresh."""
    monkeypatch.setattr(fit_refresh, "current_prose_doc_id", lambda _s, _u: "p2")
    monkeypatch.setattr(fit_refresh, "stale_target_ids", lambda _s, **_k: ["a", "b"])
    got: dict = {}

    async def spy(_s, _llm, *, user_id, target_ids):  # type: ignore[no-untyped-def]
        got["user_id"] = user_id
        got["target_ids"] = target_ids

    monkeypatch.setattr(fit_refresh, "refresh_stale_fit_scores", spy)

    await fit_refresh.refresh_stale_for_user(MagicMock(), MagicMock(), user_id="u1")
    assert got == {"user_id": "u1", "target_ids": ["a", "b"]}


@pytest.mark.asyncio
async def test_refresh_for_user_noop_without_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fit_refresh, "current_prose_doc_id", lambda _s, _u: None)
    called = {"n": 0}

    async def spy(_s, _llm, *, user_id, target_ids):  # type: ignore[no-untyped-def]
        called["n"] += 1

    monkeypatch.setattr(fit_refresh, "refresh_stale_fit_scores", spy)

    await fit_refresh.refresh_stale_for_user(MagicMock(), MagicMock(), user_id="u1")
    assert called["n"] == 0
