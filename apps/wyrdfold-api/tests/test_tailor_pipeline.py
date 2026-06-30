"""Tailor pipeline end-to-end tests with mocked Supabase + real
MockLLMClient. Covers success, lint failure, and storage failure paths.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from docx import Document

from app.config import settings
from app.models.ats_lint import LintResult, LintViolation
from app.models.experience import (
    OptimizedDoc,
    OptimizedPayload,
    Outcome,
    PreferencesPayload,
    Role,
    Skill,
)
from app.models.tailor import (
    ContactInfo,
    TailoredBullet,
    TailoredEducation,
    TailoredResume,
    TailoredRole,
)
from app.services.llm import cost_log as cost_log_mod
from app.services.llm.mock import MockLLMClient
from app.services.tailor.faithfulness import (
    FAITHFULNESS_REVIEW_PURPOSE,
    FaithfulnessFlag,
    FaithfulnessReview,
    review_to_critique,
)
from app.services.tailor.pipeline import (
    PipelineLintFailure,
    PipelineSuccess,
    run_tailor_pipeline,
)
from app.services.tailor.tailor import DEFAULT_PURPOSE


def _optimized_doc() -> OptimizedDoc:
    return OptimizedDoc(
        id="opt-1",
        user_id="test-user",
        prose_doc_id=None,
        version=1,
        payload=OptimizedPayload(
            summary="Senior FE.",
            roles=[
                Role(
                    id="fc",
                    company="FightCamp",
                    title="Senior Frontend Engineer",
                    start="2021-11",
                    end="2024-04",
                    summary="Led the PDP rebuild.",
                    skills=["React"],
                    outcome_refs=[],
                )
            ],
            skills=[Skill(name="React")],
            outcomes=[
                Outcome(
                    description="Cut mobile load times from 10s to 2s",
                    metric="LCP",
                    value="2s",
                    role_ref="fc",
                )
            ],
        ),
        markdown_view=None,
        source="llm",
        created_at=datetime.now(UTC),
    )


def _contact() -> ContactInfo:
    return ContactInfo(name="Daniel Joffe", email="daniel@example.com")


def _valid_resume_json() -> str:
    return TailoredResume(
        summary="Senior FE with a decade of shipped work.",
        contact=_contact(),
        experience=[
            TailoredRole(
                company="FightCamp",
                title="Senior Frontend Engineer",
                start="2021-11",
                end="2024-04",
                bullets=[
                    TailoredBullet(
                        text="Cut mobile load times from 10s to 2s.",
                        source_outcome_ref="Cut mobile load times from 10s to 2s",
                    ),
                ],
                source_role_ref="fc",
            )
        ],
        skills=["React"],
        education=[TailoredEducation(school="UCLA")],
    ).model_dump_json()


def _inserted_record_row(record_id: str = "rec-1") -> dict[str, Any]:
    """The shape `supabase.table().insert(...).execute().data` returns."""
    return {
        "id": record_id,
        "user_id": None,
        "job_posting_id": None,
        "resume_type": "generic",
        "jd_snapshot": "JD text",
        "jd_snapshot_hash": "hash",
        "payload": TailoredResume.model_validate_json(
            _valid_resume_json()
        ).model_dump(mode="json"),
        "storage_path": None,
        "warnings": [],
        "model": "claude-sonnet-4-6",
        "input_tokens": 100,
        "output_tokens": 50,
        "cost_usd": 0.001,
        "latency_ms": 50,
        "created_at": datetime.now(UTC).isoformat(),
    }


def _make_supabase_mock(*, insert_data: list[dict[str, Any]]) -> MagicMock:
    supabase = MagicMock()
    supabase.table.return_value.insert.return_value.execute.return_value.data = (
        insert_data
    )
    supabase.table.return_value.update.return_value.eq.return_value.execute.return_value.data = []
    supabase.storage.from_.return_value.upload.return_value = None
    return supabase


# ---- Success path ---------------------------------------------------------


async def test_success_returns_record_and_persists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supabase = _make_supabase_mock(insert_data=[_inserted_record_row()])
    monkeypatch.setattr(cost_log_mod, "record", MagicMock())

    llm = MockLLMClient(scripted={DEFAULT_PURPOSE: _valid_resume_json()})
    result = await run_tailor_pipeline(
        supabase,
        llm,
        user_id="test-user",
        optimized=_optimized_doc(),
        job_description="We want a senior FE",
        contact=_contact(),
    )

    assert isinstance(result, PipelineSuccess)
    assert result.record.id == "rec-1"
    assert result.record.storage_path is not None
    # upload_docx was called on the storage bucket
    supabase.storage.from_.assert_any_call("tailored-resumes")


async def test_success_cost_logs_under_tailor_purpose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supabase = _make_supabase_mock(insert_data=[_inserted_record_row()])
    cost_record = MagicMock()
    monkeypatch.setattr(cost_log_mod, "record", cost_record)

    llm = MockLLMClient(scripted={DEFAULT_PURPOSE: _valid_resume_json()})
    await run_tailor_pipeline(
        supabase,
        llm,
        user_id="test-user",
        optimized=_optimized_doc(),
        job_description="jd",
        contact=_contact(),
    )
    call = cost_record.call_args
    assert call.kwargs["purpose"] == DEFAULT_PURPOSE


async def test_preferences_are_passed_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supabase = _make_supabase_mock(insert_data=[_inserted_record_row()])
    monkeypatch.setattr(cost_log_mod, "record", MagicMock())

    seen: dict[str, str] = {}

    def responder(latest_user: str, _messages: object) -> str:
        seen["latest"] = latest_user
        return _valid_resume_json()

    llm = MockLLMClient(scripted={DEFAULT_PURPOSE: responder})
    prefs = PreferencesPayload(
        rules=["lead with performance"],
        avoid=["em dashes"],
        tone_notes=["calm confidence"],
    )
    await run_tailor_pipeline(
        supabase,
        llm,
        user_id="test-user",
        optimized=_optimized_doc(),
        job_description="jd",
        contact=_contact(),
        preferences=prefs,
    )
    assert "[Preferences]" in seen["latest"]
    assert "lead with performance" in seen["latest"]
    assert "em dashes" in seen["latest"]


# ---- Lint failure path ---------------------------------------------------


async def test_lint_failure_does_not_persist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supabase = _make_supabase_mock(insert_data=[])
    monkeypatch.setattr(cost_log_mod, "record", MagicMock())

    # Force the linter to report an error.
    def fake_lint(_b: bytes) -> LintResult:
        return LintResult(
            ok=False,
            violations=[
                LintViolation(
                    code="no_tables",
                    message="simulated lint failure",
                    severity="error",
                )
            ],
        )

    monkeypatch.setattr("app.services.tailor.pipeline.lint_docx", fake_lint)

    llm = MockLLMClient(scripted={DEFAULT_PURPOSE: _valid_resume_json()})
    result = await run_tailor_pipeline(
        supabase,
        llm,
        user_id="test-user",
        optimized=_optimized_doc(),
        job_description="jd",
        contact=_contact(),
    )

    assert isinstance(result, PipelineLintFailure)
    assert any(v.code == "no_tables" for v in result.lint.errors)
    # No insert call for documents.
    supabase.table.return_value.insert.assert_not_called()


# ---- Storage failure path (row already persisted, storage_path stays None)


async def test_storage_upload_failure_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supabase = _make_supabase_mock(insert_data=[_inserted_record_row()])
    supabase.storage.from_.return_value.upload.side_effect = RuntimeError("s3 down")
    monkeypatch.setattr(cost_log_mod, "record", MagicMock())

    llm = MockLLMClient(scripted={DEFAULT_PURPOSE: _valid_resume_json()})
    result = await run_tailor_pipeline(
        supabase,
        llm,
        user_id="test-user",
        optimized=_optimized_doc(),
        job_description="jd",
        contact=_contact(),
    )
    assert isinstance(result, PipelineSuccess)
    # storage_path remains None when upload raises.
    assert result.record.storage_path is None


# ---- Rendered bytes are a valid .docx -------------------------------------


async def test_rendered_output_opens_as_valid_docx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity check: the pipeline's render_docx output is parseable."""
    captured: dict[str, bytes] = {}

    from app.services.ats_lint import lint_docx as real_lint

    def capturing_lint(data: bytes) -> LintResult:
        captured["docx"] = data
        return real_lint(data)

    supabase = _make_supabase_mock(insert_data=[_inserted_record_row()])
    monkeypatch.setattr(cost_log_mod, "record", MagicMock())
    monkeypatch.setattr("app.services.tailor.pipeline.lint_docx", capturing_lint)

    llm = MockLLMClient(scripted={DEFAULT_PURPOSE: _valid_resume_json()})
    await run_tailor_pipeline(
        supabase,
        llm,
        user_id="test-user",
        optimized=_optimized_doc(),
        job_description="jd",
        contact=_contact(),
    )

    import io

    doc = Document(io.BytesIO(captured["docx"]))
    texts = [p.text for p in doc.paragraphs]
    assert "Daniel Joffe" in texts[0]
    assert any("FightCamp" in t for t in texts)


# ---- Faithfulness review pass (#6b) ---------------------------------------


def test_actionable_flags_filters_to_medium_and_high() -> None:
    review = FaithfulnessReview(
        flags=[
            FaithfulnessFlag(claim="a", issue="exaggeration", severity="low", suggestion="s"),
            FaithfulnessFlag(claim="b", issue="fabrication", severity="medium", suggestion="s"),
            FaithfulnessFlag(claim="c", issue="unsupported_skill", severity="high", suggestion="s"),
        ]
    )
    assert [f.claim for f in review.actionable_flags()] == ["b", "c"]


def test_review_to_critique_none_when_no_actionable_flags() -> None:
    review = FaithfulnessReview(
        flags=[FaithfulnessFlag(claim="a", issue="exaggeration", severity="low", suggestion="s")]
    )
    assert review_to_critique(review) is None


def test_review_to_critique_renders_actionable_flags() -> None:
    review = FaithfulnessReview(
        flags=[
            FaithfulnessFlag(
                claim="led a team of 50", issue="exaggeration", severity="high", suggestion="say 5"
            )
        ]
    )
    crit = review_to_critique(review)
    assert crit is not None
    assert "led a team of 50" in crit and "exaggeration" in crit


def _scripted_llm(review: FaithfulnessReview) -> MockLLMClient:
    return MockLLMClient(
        scripted={
            DEFAULT_PURPOSE: _valid_resume_json(),
            FAITHFULNESS_REVIEW_PURPOSE: review.model_dump_json(),
        }
    )


async def test_review_disabled_skips_review(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "faithfulness_review_enabled", False)
    supabase = _make_supabase_mock(insert_data=[_inserted_record_row()])
    rec = MagicMock()
    monkeypatch.setattr(cost_log_mod, "record", rec)

    result = await run_tailor_pipeline(
        supabase,
        _scripted_llm(FaithfulnessReview(flags=[])),
        user_id="test-user",
        optimized=_optimized_doc(),
        job_description="JD",
        contact=_contact(),
    )
    assert isinstance(result, PipelineSuccess)
    assert rec.call_count == 1  # generate only — no review


async def test_review_clean_does_not_regenerate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "faithfulness_review_enabled", True)
    supabase = _make_supabase_mock(insert_data=[_inserted_record_row()])
    rec = MagicMock()
    monkeypatch.setattr(cost_log_mod, "record", rec)

    # Only a low-severity flag → not actionable → no corrective regen.
    review = FaithfulnessReview(
        flags=[FaithfulnessFlag(claim="x", issue="exaggeration", severity="low", suggestion="s")]
    )
    result = await run_tailor_pipeline(
        supabase,
        _scripted_llm(review),
        user_id="test-user",
        optimized=_optimized_doc(),
        job_description="JD",
        contact=_contact(),
    )
    assert isinstance(result, PipelineSuccess)
    assert rec.call_count == 2  # generate + review, no regen


async def test_review_flags_trigger_one_regeneration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "faithfulness_review_enabled", True)
    supabase = _make_supabase_mock(insert_data=[_inserted_record_row()])
    rec = MagicMock()
    monkeypatch.setattr(cost_log_mod, "record", rec)

    review = FaithfulnessReview(
        flags=[
            FaithfulnessFlag(
                claim="led 50 engineers", issue="exaggeration", severity="high", suggestion="say 5"
            )
        ]
    )
    result = await run_tailor_pipeline(
        supabase,
        _scripted_llm(review),
        user_id="test-user",
        optimized=_optimized_doc(),
        job_description="JD",
        contact=_contact(),
    )
    assert isinstance(result, PipelineSuccess)
    assert rec.call_count == 3  # generate + review + ONE corrective regen
