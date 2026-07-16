"""Phase-2 grade backfill sweep (``poller._backfill_grade_stale``).

The qualify sweep's grading twin: every cycle, grade a bounded,
view-ordered batch of LIVE ``promising`` rows stuck at ``stage2`` —
deferred grades and the rows a profile-version bump reset (a profile edit
wiped one target's graded shelf and left its /jobs list 76% off-target
pending noise, 2026-07-15). Spend rides the existing gates: payer
allowance / BYOK defers here, the per-target daily quota inside
``run_phase2_for_jobs``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.models.targets import CategoryProfile, JobTarget, ScoringProfile, SeniorityProfile
from app.services import poller as poller_mod
from app.services.targets.payers import PayerBudgetGate


def _target(tid: str = "t-1") -> JobTarget:
    return JobTarget(
        id=tid,
        label="FE",
        scoring_profile=ScoringProfile(
            categories={"core_skills": CategoryProfile(keywords={"react": 3}, weight=2.0)},
            seniority=SeniorityProfile(signals=["senior"]),
        ),
        search_keywords=["frontend engineer"],
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


class _OpenGate(PayerBudgetGate):
    def user_blocked(self, _uid: str | None) -> bool:  # type: ignore[override]
        return False


class _ClosedGate(PayerBudgetGate):
    def user_blocked(self, _uid: str | None) -> bool:  # type: ignore[override]
        return True


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    gate: PayerBudgetGate,
    stale_rows: list[dict[str, Any]] | Exception,
    llm: object | None = object(),
    graded: int | Exception = 2,
) -> dict[str, Any]:
    """Patch the sweep's collaborators; return the recorders."""
    rec: dict[str, Any] = {"selects": [], "phase2_calls": [], "agg_calls": []}
    target = _target()
    doc = MagicMock()
    doc.payload = {"profile": "stub"}

    async def fake_gate(_sb: Any) -> tuple[PayerBudgetGate, bool]:
        return gate, True

    async def fake_stage3(_sb: Any, _targets: Any, _label: str) -> tuple[dict, dict]:
        return {"u1": target}, {"u1": doc}

    async def fake_read(_sb: Any, build: Any, *, label: str, retry_sync: bool = False) -> Any:
        rec["selects"].append(label)
        if isinstance(stale_rows, Exception):
            raise stale_rows
        chain = _Recorder()
        build(chain)
        rec["query_ops"] = chain.ops
        return MagicMock(data=stale_rows)

    async def fake_phase2(*a: Any, **kw: Any) -> int:
        rec["phase2_calls"].append(kw)
        if isinstance(graded, Exception):
            raise graded
        return graded

    async def fake_agg(_sb: Any, ids: list[str]) -> None:
        rec["agg_calls"].append(ids)

    monkeypatch.setattr(poller_mod, "_cycle_budget_gate", fake_gate)
    monkeypatch.setattr(poller_mod, "get_active_target", lambda _sb: [target])
    monkeypatch.setattr(poller_mod, "_resolve_user_targets_for_stage3", fake_stage3)
    monkeypatch.setattr(poller_mod, "_resolve_payer_client", lambda _c, _sb, _uid: llm)
    monkeypatch.setattr(poller_mod, "poll_db_read", fake_read)
    monkeypatch.setattr(poller_mod, "run_phase2_for_jobs", fake_phase2)
    monkeypatch.setattr(poller_mod, "batch_update_global_scores", fake_agg)
    return rec


class _Recorder:
    """Chain recorder for the build closure — captures the query shape."""

    def __init__(self) -> None:
        self.ops: list[tuple[Any, ...]] = []

    def __getattr__(self, name: str) -> Any:
        if name == "not_":
            self.ops.append(("not_",))
            return self

        def _record(*args: Any, **kwargs: Any) -> _Recorder:
            self.ops.append((name, args, kwargs))
            return self

        return _record


def _rows(n: int) -> list[dict[str, Any]]:
    return [
        {
            "recency_score": 50 - i,
            "jobs": {"id": f"j-{i}", "title": f"T{i}", "description_html": "x"},
        }
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_zero_limit_is_a_full_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {"n": 0}

    async def boom(*_a: Any, **_k: Any) -> Any:
        called["n"] += 1

    monkeypatch.setattr(poller_mod, "_cycle_budget_gate", boom)
    await poller_mod._backfill_grade_stale(MagicMock(), 0)
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_grades_live_stale_rows_and_reaggregates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _wire(monkeypatch, gate=_OpenGate(), stale_rows=_rows(3), graded=3)

    await poller_mod._backfill_grade_stale(MagicMock(), 25)

    assert rec["selects"] == ["poll grade-backfill select"]
    # Selection is LIVE-only + promising/stage2 + view-ordered + bounded —
    # the query-shape pin for the 2026-07-15 lesson (78% of the stale
    # backlog was attached to dead jobs).
    ops = rec["query_ops"]
    assert ("eq", ("promising", True), {}) in ops
    assert ("eq", ("scoring_status", "stage2"), {}) in ops
    assert ("eq", ("excluded", False), {}) in ops
    assert ("is_", ("jobs.archived_at", "null"), {}) in ops
    assert ("is_", ("jobs.purged_at", "null"), {}) in ops
    assert ("not_",) in ops and ("is_", ("jobs.is_us", "false"), {}) in ops
    assert ("limit", (25,), {}) in ops
    assert any(o[0] == "order" and o[1] == ("recency_score",) for o in ops)

    assert len(rec["phase2_calls"]) == 1
    call = rec["phase2_calls"][0]
    assert [j["id"] for j in call["jobs"]] == ["j-0", "j-1", "j-2"]
    assert call["user_id"] == "u1"
    # Graded rows re-aggregate jobs.score (mirrors the cycle path).
    assert rec["agg_calls"] == [["j-0", "j-1", "j-2"]]


@pytest.mark.asyncio
async def test_blocked_payer_defers_without_selecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _wire(monkeypatch, gate=_ClosedGate(), stale_rows=_rows(3))
    await poller_mod._backfill_grade_stale(MagicMock(), 25)
    assert rec["selects"] == []
    assert rec["phase2_calls"] == []


@pytest.mark.asyncio
async def test_byok_missing_key_defers(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _wire(monkeypatch, gate=_OpenGate(), stale_rows=_rows(3), llm=None)
    await poller_mod._backfill_grade_stale(MagicMock(), 25)
    assert rec["phase2_calls"] == []


@pytest.mark.asyncio
async def test_select_failure_fails_soft(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _wire(monkeypatch, gate=_OpenGate(), stale_rows=RuntimeError("db down"))
    await poller_mod._backfill_grade_stale(MagicMock(), 25)  # must not raise
    assert rec["phase2_calls"] == []


@pytest.mark.asyncio
async def test_phase2_failure_fails_soft_and_skips_reaggregate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _wire(monkeypatch, gate=_OpenGate(), stale_rows=_rows(2), graded=RuntimeError("llm down"))
    await poller_mod._backfill_grade_stale(MagicMock(), 25)  # must not raise
    assert rec["agg_calls"] == []


@pytest.mark.asyncio
async def test_zero_graded_skips_reaggregate(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _wire(monkeypatch, gate=_OpenGate(), stale_rows=_rows(2), graded=0)
    await poller_mod._backfill_grade_stale(MagicMock(), 25)
    assert rec["agg_calls"] == []


@pytest.mark.asyncio
async def test_empty_backlog_is_quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _wire(monkeypatch, gate=_OpenGate(), stale_rows=[])
    await poller_mod._backfill_grade_stale(MagicMock(), 25)
    assert rec["phase2_calls"] == []
    assert rec["agg_calls"] == []


@pytest.mark.asyncio
async def test_poll_due_sources_runs_sweep_before_early_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sweep must run even on a quiet cycle (no due sources) — same
    placement contract as the qualify/embed sweeps."""
    from app.config import settings

    monkeypatch.setattr(settings, "phase2_enabled", True)
    monkeypatch.setattr(settings, "phase2_backfill_batch", 25)
    monkeypatch.setattr(settings, "qualification_enabled", False)
    monkeypatch.setattr(settings, "prescan_embed_enabled", False)
    monkeypatch.setattr(settings, "archival_sweep_enabled", False)

    swept = {"n": 0}

    async def fake_sweep(_sb: Any, limit: int) -> None:
        swept["n"] += 1
        assert limit == 25

    async def fake_read(_sb: Any, build: Any, *, label: str, retry_sync: bool = False) -> Any:
        return MagicMock(data=[])  # no enabled sources → quiet cycle

    async def fake_recover(_sb: Any) -> int:
        return 0

    async def fake_lifecycle(_sb: Any) -> None:
        return None

    monkeypatch.setattr(poller_mod, "_backfill_grade_stale", fake_sweep)
    monkeypatch.setattr(poller_mod, "poll_db_read", fake_read)
    monkeypatch.setattr(poller_mod, "recover_stale_sources", fake_recover)
    monkeypatch.setattr(poller_mod, "_maybe_run_lifecycle_sweep", fake_lifecycle)
    monkeypatch.setattr(poller_mod, "_maybe_run_archival_sweep", fake_lifecycle)

    result = await poller_mod.poll_due_sources(MagicMock())

    assert swept["n"] == 1
    assert result.sources_polled == 0
