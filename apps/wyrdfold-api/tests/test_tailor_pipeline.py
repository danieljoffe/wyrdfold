"""Tailor pipeline end-to-end tests with mocked Supabase + real
MockLLMClient. Covers success, lint failure, and storage failure paths.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

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
from app.services.llm.mock import MockLLMClient, ats_hostile_resume_json
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
        "payload": TailoredResume.model_validate_json(_valid_resume_json()).model_dump(mode="json"),
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
    tbl = supabase.table.return_value
    tbl.insert.return_value.execute = AsyncMock(return_value=MagicMock(data=insert_data))
    tbl.update.return_value.eq.return_value.execute = AsyncMock(return_value=MagicMock(data=[]))
    # versions.record()'s _prune reads existing versions (empty → no delete).
    tbl.select.return_value.eq.return_value.order.return_value.execute = AsyncMock(
        return_value=MagicMock(data=[])
    )
    supabase.storage.from_.return_value.upload = AsyncMock(return_value=None)
    return supabase


# ---- Success path ---------------------------------------------------------


async def test_success_returns_record_and_persists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supabase = _make_supabase_mock(insert_data=[_inserted_record_row()])
    monkeypatch.setattr(cost_log_mod, "record_async", AsyncMock())

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
    cost_record = AsyncMock()
    monkeypatch.setattr(cost_log_mod, "record_async", cost_record)

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
    monkeypatch.setattr(cost_log_mod, "record_async", AsyncMock())

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


# ---- Lint failure path (#656: flagged, not discarded) ---------------------


def _lint_error(code: str = "no_tables") -> LintResult:
    return LintResult(
        ok=False,
        violations=[LintViolation(code=code, message="simulated lint failure", severity="error")],
    )


def _documents_row(supabase: MagicMock) -> dict[str, Any]:
    """The row handed to ``documents.insert(...)``.

    Selected by shape, not by call index: ``insert_row`` also writes a
    ``resume_versions`` snapshot through the same MagicMock, so
    ``call_args`` (the LAST call) is the version row, not the document.
    """
    rows = [
        c.args[0]
        for c in supabase.table.return_value.insert.call_args_list
        if isinstance(c.args[0], dict) and "jd_snapshot" in c.args[0]
    ]
    assert len(rows) == 1, f"expected exactly one documents insert, got {len(rows)}"
    return rows[0]


async def test_docx_lint_failure_persists_flagged_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#656: a resume that fails the .docx lint is PERSISTED with its
    violations rather than thrown away. The run costs an LLM call and a slot
    in the daily cap, and now that generation is backgrounded nobody may be
    watching to retry — so the draft has to survive for the user to fix."""
    supabase = _make_supabase_mock(insert_data=[_inserted_record_row()])
    monkeypatch.setattr(cost_log_mod, "record_async", AsyncMock())
    monkeypatch.setattr("app.services.tailor.pipeline.lint_docx", lambda _b, **_kw: _lint_error())

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
    # The row was written, and it carries the violations that flag it.
    row = _documents_row(supabase)
    assert [v["code"] for v in row["lint_violations"]] == ["no_tables"]
    assert row["payload_md"], "the flagged draft keeps its markdown to edit"
    assert result.record.id == "rec-1"
    assert result.payload_md == row["payload_md"]
    # No .docx is uploaded for a flagged draft — the download route re-renders
    # lazily from payload_md, so rendering now would only be thrown away the
    # moment the user edits.
    supabase.storage.from_.return_value.upload.assert_not_called()


async def test_markdown_lint_failure_persists_flagged_draft_without_rendering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The earlier of the two lint gates behaves identically — and short-circuits
    before pandoc runs at all."""
    supabase = _make_supabase_mock(insert_data=[_inserted_record_row()])
    monkeypatch.setattr(cost_log_mod, "record_async", AsyncMock())
    monkeypatch.setattr(
        "app.services.tailor.pipeline.lint_markdown",
        lambda _md, **_kw: _lint_error("md_boom"),
    )
    rendered: list[str] = []
    monkeypatch.setattr(
        "app.services.tailor.pipeline.md_to_docx",
        lambda md, *a, **k: rendered.append(md) or b"x",
    )

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
    assert rendered == [], "md-lint failure must not pay for a pandoc render"
    row = _documents_row(supabase)
    assert [v["code"] for v in row["lint_violations"]] == ["md_boom"]


async def test_clean_generation_persists_empty_violations_not_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The negative case that gives the flag meaning: a resume that PASSES
    lint stores ``[]`` — 'linted, clean' — never NULL. NULL is reserved for
    rows that predate the column, so conflating them would make every clean
    draft indistinguishable from an unlinted one."""
    supabase = _make_supabase_mock(insert_data=[_inserted_record_row()])
    monkeypatch.setattr(cost_log_mod, "record_async", AsyncMock())

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
    row = _documents_row(supabase)
    assert row["lint_violations"] == []


async def test_clean_generation_persists_warnings_so_they_survive_the_202(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Warnings are stored too, not just blocking errors. Pre-#656 they only
    ever rode back on the POST response; now the POST returns 202 and the
    client polls for the record, so a warnings-only advisory would vanish
    entirely if the column held errors alone."""
    supabase = _make_supabase_mock(insert_data=[_inserted_record_row()])
    monkeypatch.setattr(cost_log_mod, "record_async", AsyncMock())
    monkeypatch.setattr(
        "app.services.tailor.pipeline.lint_docx",
        lambda _b, **_kw: LintResult(
            ok=True,
            violations=[LintViolation(code="long_line", message="a bit long", severity="warning")],
        ),
    )

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
    row = _documents_row(supabase)
    assert [v["code"] for v in row["lint_violations"]] == ["long_line"]
    # ...and a warnings-only list is NOT a flagged draft.
    assert all(v["severity"] == "warning" for v in row["lint_violations"])


# ---- Storage failure path (row already persisted, storage_path stays None)


async def test_storage_upload_failure_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supabase = _make_supabase_mock(insert_data=[_inserted_record_row()])
    supabase.storage.from_.return_value.upload.side_effect = RuntimeError("s3 down")
    monkeypatch.setattr(cost_log_mod, "record_async", AsyncMock())

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
    monkeypatch.setattr(cost_log_mod, "record_async", AsyncMock())
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
    rec = AsyncMock()
    monkeypatch.setattr(cost_log_mod, "record_async", rec)

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
    rec = AsyncMock()
    monkeypatch.setattr(cost_log_mod, "record_async", rec)

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
    rec = AsyncMock()
    monkeypatch.setattr(cost_log_mod, "record_async", rec)

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


async def test_flagged_draft_path_end_to_end_with_the_real_linter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flagged-draft path (#656) driven from LLM output all the way through
    the production linter — no ``lint_markdown``/``lint_docx`` stub anywhere.

    The other lint-failure tests monkeypatch the linter to force the branch,
    which proves the branch works but not that anything real reaches it. This
    one scripts the mock's ATS-hostile resume (bug corpus) and lets the actual
    rules decide, so a linter change that stops catching tables — or a renderer
    change that stops emitting them — fails here instead of silently turning
    every "flagged" test into a success-path test.
    """
    supabase = _make_supabase_mock(insert_data=[_inserted_record_row()])
    monkeypatch.setattr(cost_log_mod, "record_async", AsyncMock())

    llm = MockLLMClient(scripted={DEFAULT_PURPOSE: ats_hostile_resume_json()})
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

    # The draft SURVIVED: a real row, flagged, with the markdown to edit.
    row = _documents_row(supabase)
    assert "no_tables" in {v["code"] for v in row["lint_violations"]}
    assert "| Metric | Before | After |" in row["payload_md"]
    assert result.record.id == "rec-1"
