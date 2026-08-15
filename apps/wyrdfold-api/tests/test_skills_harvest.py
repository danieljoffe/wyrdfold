"""Skills harvest (plan-phase2-structured-harvest.md): prompt composition,
normalization, and persistence contracts.

The parse-path edge battery lives in ``test_llm_mock.py`` (harvest-corpus
entries); this file pins the surrounding plumbing:

- the system prompt is BYTE-IDENTICAL to the legacy composition whenever
  ``extract_skills`` is off (prompt-cache + golden hygiene), and the
  addenda compose in the pinned order logistics → skills;
- ``normalize_skill`` write-time cleanup semantics;
- the scores UPDATE carries the three lists when the grader emitted them
  and omits the keys entirely when it didn't (flag-flip never blanks
  history);
- the canonical ``jobs.skills_required`` write is best-effort: its
  failure never costs the already-persisted grade.
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
from app.services.fit.job_fit import (
    _LOGISTICS_PROMPT_ADDENDUM,
    _SKILLS_PROMPT_ADDENDUM,
    _SYSTEM_PROMPT,
    AxisScores,
    JobFitResult,
    JobSkills,
    derive_job_fit,
    normalize_skill,
)
from app.services.fit.score_persistence import score_with_phase2_and_persist


def _target() -> JobTarget:
    return JobTarget(
        id="t-1",
        label="Staff Frontend Engineer",
        scoring_profile=ScoringProfile(
            categories={"core_skills": CategoryProfile(keywords={"x": 1}, weight=2.0)},
            seniority=SeniorityProfile(signals=["staff"]),
        ),
        app_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _payload() -> OptimizedPayload:
    return OptimizedPayload(summary="...", roles=[], skills=[], outcomes=[])


def _fit_with_skills() -> JobFitResult:
    return JobFitResult(
        fit_score=100,
        axes=AxisScores(title_fit=95, skills_fit=80, seniority_fit=85, domain_fit=70),
        reasoning="Strong title + skills match; missing e-commerce domain.",
        skills=JobSkills(
            skills_required=["react", "typescript", "graphql"],
            skills_matched=["react", "typescript"],
            skills_missing=["graphql"],
        ),
    )


# ---- normalize_skill ---------------------------------------------------------


def test_normalize_skill_strips_evidence_and_case_and_whitespace() -> None:
    assert normalize_skill("Automated Testing — listed in skills with no refs") == (
        "automated testing"
    )
    assert normalize_skill("  State   Management ") == "state management"
    assert normalize_skill("React") == "react"


# ---- prompt composition ------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("logistics", "skills", "expected_suffixes"),
    [
        (False, False, ()),
        (True, False, (_LOGISTICS_PROMPT_ADDENDUM,)),
        (False, True, (_SKILLS_PROMPT_ADDENDUM,)),
        (True, True, (_LOGISTICS_PROMPT_ADDENDUM, _SKILLS_PROMPT_ADDENDUM)),
    ],
)
async def test_system_prompt_composes_addenda_in_pinned_order(
    monkeypatch: pytest.MonkeyPatch,
    logistics: bool,
    skills: bool,
    expected_suffixes: tuple[str, ...],
) -> None:
    """Off = byte-identical legacy prompt; on = base + addenda in the
    pinned logistics → skills order. Byte parity in the off case is what
    keeps prompt-cache hits and the golden diff clean."""
    captured: dict[str, Any] = {}

    async def fake_complete_json(*_args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return (_fit_with_skills(), MagicMock())

    monkeypatch.setattr("app.services.fit.job_fit.complete_json", fake_complete_json)

    await derive_job_fit(
        MagicMock(),
        payload=_payload(),
        target=_target(),
        job_title="Senior FE",
        jd_text="JD body",
        extract_logistics=logistics,
        extract_skills=skills,
    )

    expected = _SYSTEM_PROMPT + "".join(expected_suffixes)
    assert captured["system"] == expected


# ---- persistence -------------------------------------------------------------


def _routed_supabase() -> tuple[MagicMock, MagicMock, MagicMock]:
    """Supabase mock routing ``scores`` and ``jobs`` to separate table
    mocks, so assertions can't conflate the two UPDATE targets."""
    scores_table = MagicMock()
    jobs_table = MagicMock()
    supabase = MagicMock()
    supabase.table.side_effect = lambda name: {
        "scores": scores_table,
        "jobs": jobs_table,
    }[name]
    return supabase, scores_table, jobs_table


def _wire(monkeypatch: pytest.MonkeyPatch, fit: JobFitResult) -> None:
    async def fake_derive(*_args: object, **_kwargs: object) -> object:
        return (fit, MagicMock())

    async def fake_cost(*_args: object, **_kwargs: object) -> object:
        return MagicMock()

    monkeypatch.setattr("app.services.fit.score_persistence.derive_job_fit", fake_derive)
    monkeypatch.setattr("app.services.fit.score_persistence.record_llm_cost_async", fake_cost)


@pytest.mark.asyncio
async def test_persists_pair_level_skills_and_never_touches_the_facet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The harvest writes PAIR-level skills (for insights) and must NOT write
    ``jobs.skills_required``.

    That column is the search facet and has exactly one writer — the skill
    dictionary. Two writers would mean last-write-wins between a canonical
    vocabulary and the model's free-form one (1,757 values, 68% singletons),
    so a posting's findability would depend on whether it was graded or
    re-tagged more recently."""
    supabase, scores_table, jobs_table = _routed_supabase()
    _wire(monkeypatch, _fit_with_skills())

    result = await score_with_phase2_and_persist(
        supabase,
        MagicMock(),
        payload=_payload(),
        target=_target(),
        job_posting_id="job-1",
        title="Senior FE",
        jd_text="JD body",
    )

    assert result is not None
    scores_update = scores_table.update.call_args.args[0]
    assert scores_update["skills_required"] == ["react", "typescript", "graphql"]
    assert scores_update["skills_matched"] == ["react", "typescript"]
    assert scores_update["skills_missing"] == ["graphql"]
    # The facet column is left entirely alone.
    jobs_table.update.assert_not_called()


@pytest.mark.asyncio
async def test_no_skills_means_no_keys_and_no_jobs_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A grade without harvest output (flag off / model omitted it) must not
    write the keys at all — a later flag flip can never blank history — and
    must not touch the jobs table."""
    supabase, scores_table, jobs_table = _routed_supabase()
    fit = JobFitResult(
        fit_score=100,
        axes=AxisScores(title_fit=95, skills_fit=80, seniority_fit=85, domain_fit=70),
        reasoning="Strong title + skills match; missing e-commerce domain.",
    )
    assert fit.skills is None  # precondition
    _wire(monkeypatch, fit)

    result = await score_with_phase2_and_persist(
        supabase,
        MagicMock(),
        payload=_payload(),
        target=_target(),
        job_posting_id="job-1",
        title="Senior FE",
        jd_text="JD body",
    )

    assert result is not None
    scores_update = scores_table.update.call_args.args[0]
    assert "skills_required" not in scores_update
    assert "skills_matched" not in scores_update
    assert "skills_missing" not in scores_update
    jobs_table.update.assert_not_called()


@pytest.mark.asyncio
async def test_grade_persists_without_any_jobs_table_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard for the single-writer rule: a supabase whose ``jobs``
    table raises on touch must not affect grading at all. Before the dictionary
    owned the facet, this path wrote there and had to be failure-tolerant; now
    it must never reach the table in the first place."""
    scores_table = MagicMock()
    supabase = MagicMock()

    def _route(name: str) -> MagicMock:
        if name == "jobs":
            raise AssertionError("harvest must not touch jobs.skills_required")
        return scores_table

    supabase.table.side_effect = _route
    _wire(monkeypatch, _fit_with_skills())

    result = await score_with_phase2_and_persist(
        supabase,
        MagicMock(),
        payload=_payload(),
        target=_target(),
        job_posting_id="job-1",
        title="Senior FE",
        jd_text="JD body",
    )

    assert result is not None
    assert scores_table.update.called
