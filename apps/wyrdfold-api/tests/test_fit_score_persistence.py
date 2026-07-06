"""Tests for the Phase 2 score-and-persist helper.

Mocks ``derive_job_fit`` to assert:
- LLM success: scores row gets UPDATE with score + axis_scores +
  fit_reasoning + scoring_status; cost log entry recorded.
- LLM failure: returns None, no DB update, no cost log (we didn't
  spend tokens).
- DB persist failure after successful LLM call: still returns None
  but the cost log entry WAS recorded (we did spend the tokens, so
  the daily cap should account for them).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.models.experience import OptimizedPayload
from app.models.targets import (
    CategoryProfile,
    JobTarget,
    ScoringProfile,
    SeniorityProfile,
)
from app.services.fit.job_fit import AxisScores, JobFitResult
from app.services.fit.score_persistence import score_with_phase2_and_persist


def _target() -> JobTarget:
    return JobTarget(
        id="t-1",
        label="Staff Frontend Engineer",
        scoring_profile=ScoringProfile(
            categories={"core_skills": CategoryProfile(keywords={"x": 1}, weight=2.0)},
            seniority=SeniorityProfile(signals=["staff"]),
        ),
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _payload() -> OptimizedPayload:
    return OptimizedPayload(summary="...", roles=[], skills=[], outcomes=[])


def _fake_fit() -> JobFitResult:
    return JobFitResult(
        fit_score=82,
        axes=AxisScores(title_fit=95, skills_fit=80, seniority_fit=85, domain_fit=70),
        reasoning="Strong title + skills match; missing e-commerce domain.",
    )


@pytest.mark.asyncio
async def test_success_updates_scores_row_with_full_phase2_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: LLM returns a fit, cost is logged, scores row UPDATE
    carries the four Phase 2 fields (score, axis_scores, fit_reasoning,
    scoring_status='complete')."""
    supabase = MagicMock()
    llm = MagicMock()

    async def fake_derive(*args: object, **kwargs: object) -> object:
        return (_fake_fit(), MagicMock())

    cost_calls: list[dict[str, object]] = []

    def fake_cost(supabase, *, user_id, purpose, result, metadata=None) -> object:
        cost_calls.append({"purpose": purpose, "metadata": metadata, "user_id": user_id})
        return MagicMock()

    monkeypatch.setattr("app.services.fit.score_persistence.derive_job_fit", fake_derive)
    monkeypatch.setattr("app.services.fit.score_persistence.record_llm_cost", fake_cost)

    result = await score_with_phase2_and_persist(
        supabase,
        llm,
        payload=_payload(),
        target=_target(),
        job_posting_id="job-1",
        title="Senior FE",
        jd_text="JD body",
    )

    assert result is not None
    assert result.fit_score == 82

    # Cost log fired exactly once with the right scoping.
    assert len(cost_calls) == 1
    assert cost_calls[0]["purpose"] == "fit.job"
    assert cost_calls[0]["metadata"] == {
        "target_id": "t-1",
        "job_posting_id": "job-1",
    }

    # UPDATE fired with all four Phase 2 fields.
    update_args = supabase.table.return_value.update.call_args.args[0]
    assert update_args["score"] == 82
    assert update_args["axis_scores"] == {
        "title_fit": 95,
        "skills_fit": 80,
        "seniority_fit": 85,
        "domain_fit": 70,
    }
    assert update_args["fit_reasoning"].startswith("Strong title")
    assert update_args["scoring_status"] == "complete"
    assert "updated_at" in update_args


@pytest.mark.asyncio
async def test_empty_jd_drops_job_without_grading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A whitespace-only JD is dropped — the row is excluded + marked terminal
    with NO LLM grade and NO cost (not graded, not left Pending). (#47)"""
    supabase = MagicMock()
    llm = MagicMock()

    derive_calls = 0

    async def fake_derive(*args: object, **kwargs: object) -> object:
        nonlocal derive_calls
        derive_calls += 1
        return (_fake_fit(), MagicMock())

    cost_calls = 0

    def fake_cost(*args: object, **kwargs: object) -> object:
        nonlocal cost_calls
        cost_calls += 1
        return MagicMock()

    monkeypatch.setattr("app.services.fit.score_persistence.derive_job_fit", fake_derive)
    monkeypatch.setattr("app.services.fit.score_persistence.record_llm_cost", fake_cost)

    result = await score_with_phase2_and_persist(
        supabase,
        llm,
        payload=_payload(),
        target=_target(),
        job_posting_id="job-1",
        title="Senior FE",
        jd_text="   \n\t  ",  # whitespace only
    )

    assert result is None
    assert derive_calls == 0  # never graded
    assert cost_calls == 0  # never spent

    # The row is dropped: excluded + terminal so it neither surfaces nor retries.
    update_args = supabase.table.return_value.update.call_args.args[0]
    assert update_args["excluded"] is True
    assert update_args["scoring_status"] == "complete"
    assert "updated_at" in update_args


@pytest.mark.asyncio
async def test_llm_failure_returns_none_and_skips_db_and_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM threw → we didn't spend tokens, so no cost log fires, and the
    scores row stays exactly as Phase 1 (or prior runs) left it."""
    supabase = MagicMock()
    llm = MagicMock()

    async def boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("anthropic-503")

    cost_calls: list[object] = []
    monkeypatch.setattr("app.services.fit.score_persistence.derive_job_fit", boom)
    monkeypatch.setattr(
        "app.services.fit.score_persistence.record_llm_cost",
        lambda *a, **k: cost_calls.append(1),
    )

    result = await score_with_phase2_and_persist(
        supabase,
        llm,
        payload=_payload(),
        target=_target(),
        job_posting_id="job-1",
        title="x",
        jd_text="x",
    )

    assert result is None
    assert cost_calls == []
    supabase.table.return_value.update.assert_not_called()


@pytest.mark.asyncio
async def test_persist_failure_still_records_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the DB UPDATE fails AFTER a successful LLM call, we still
    record the cost — we DID spend the tokens. The daily cap counter
    needs to know.
    """
    supabase = MagicMock()
    supabase.table.return_value.update.return_value.eq.return_value.eq.return_value.execute.side_effect = RuntimeError(
        "supabase-503"
    )
    llm = MagicMock()

    async def fake_derive(*args: object, **kwargs: object) -> object:
        return (_fake_fit(), MagicMock())

    cost_calls: list[int] = []
    monkeypatch.setattr("app.services.fit.score_persistence.derive_job_fit", fake_derive)
    monkeypatch.setattr(
        "app.services.fit.score_persistence.record_llm_cost",
        lambda *a, **k: cost_calls.append(1),
    )

    result = await score_with_phase2_and_persist(
        supabase,
        llm,
        payload=_payload(),
        target=_target(),
        job_posting_id="job-1",
        title="x",
        jd_text="x",
    )

    assert result is None
    assert cost_calls == [1]


# ---- #57: writes route through the poll-write seam --------------------------
# The async/sync backend selection, fail-safe, and retry are exhaustively
# tested in test_db_write.py (the seam's home). Here we assert that
# score_persistence *uses* the seam correctly — both write sites go through
# poll_db_write with the right ``scores`` query shape.

_SP = "app.services.fit.score_persistence"


class _FakeQuery:
    """Records a poll_db_write build closure's table/update/eq chain."""

    def __init__(self) -> None:
        self.table_name: str | None = None
        self.payload: dict[str, object] | None = None
        self.eqs: list[tuple[str, object]] = []

    def table(self, name: str) -> _FakeQuery:
        self.table_name = name
        return self

    def update(self, payload: dict[str, object]) -> _FakeQuery:
        self.payload = payload
        return self

    def eq(self, col: str, val: object) -> _FakeQuery:
        self.eqs.append((col, val))
        return self


def _spy_seam(monkeypatch: pytest.MonkeyPatch) -> list[tuple[_FakeQuery, str]]:
    """Patch poll_db_write to run each build closure against a _FakeQuery and
    record ``(query, label)``. Returns the capture list."""
    captured: list[tuple[_FakeQuery, str]] = []

    async def _spy(supabase: object, build: Any, *, label: str) -> object:
        q = _FakeQuery()
        build(q)
        captured.append((q, label))
        return MagicMock(data=[])

    monkeypatch.setattr(f"{_SP}.poll_db_write", _spy)
    return captured


@pytest.mark.asyncio
async def test_persist_routes_scores_update_through_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_derive(*a: object, **k: object) -> object:
        return (_fake_fit(), MagicMock())

    monkeypatch.setattr(f"{_SP}.derive_job_fit", fake_derive)
    monkeypatch.setattr(f"{_SP}.record_llm_cost", lambda *a, **k: MagicMock())
    captured = _spy_seam(monkeypatch)

    target = _target()
    result = await score_with_phase2_and_persist(
        MagicMock(),
        MagicMock(),
        payload=_payload(),
        target=target,
        job_posting_id="job-1",
        title="Senior FE",
        jd_text="JD body",
    )

    assert result is not None
    assert len(captured) == 1
    query, label = captured[0]
    assert query.table_name == "scores"
    assert query.payload is not None and query.payload["score"] == 82
    assert ("job_posting_id", "job-1") in query.eqs
    assert ("target_id", target.id) in query.eqs
    assert label == "phase2 scores update"


@pytest.mark.asyncio
async def test_empty_jd_drop_routes_through_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A whitespace JD is excluded via the seam without spending an LLM call."""
    derive_called: list[int] = []
    monkeypatch.setattr(f"{_SP}.derive_job_fit", lambda *a, **k: derive_called.append(1))
    captured = _spy_seam(monkeypatch)

    target = _target()
    result = await score_with_phase2_and_persist(
        MagicMock(),
        MagicMock(),
        payload=_payload(),
        target=target,
        job_posting_id="job-2",
        title="x",
        jd_text="   ",
    )

    assert result is None
    assert derive_called == []  # no grade spent on an empty JD
    assert len(captured) == 1
    query, _ = captured[0]
    assert query.table_name == "scores"
    assert query.payload is not None
    assert query.payload["excluded"] is True
    assert query.payload["scoring_status"] == "complete"
    assert ("job_posting_id", "job-2") in query.eqs
    assert ("target_id", target.id) in query.eqs
