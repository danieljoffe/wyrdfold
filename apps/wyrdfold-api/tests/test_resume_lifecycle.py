"""Tests for resume lifecycle: edit, approve, export-zip, get-by-job (#505)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from app.models.ats_lint import LintResult
from app.models.tailor import (
    BulkExportRequest,
    ContactInfo,
    ResumeEditRequest,
    TailoredBullet,
    TailoredResume,
    TailoredResumeRecord,
    TailoredRole,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now(UTC)

_CONTACT = ContactInfo(name="Daniel Joffe", email="d@example.com")

_RESUME = TailoredResume(
    summary="Original summary",
    contact=_CONTACT,
    experience=[
        TailoredRole(
            company="Acme",
            title="Engineer",
            start="2023-01",
            end="2024-01",
            bullets=[TailoredBullet(text="Built things", source_outcome_ref="o-1")],
            source_role_ref="role-1",
        ),
    ],
    skills=["Python", "TypeScript"],
)


def _make_record(
    *,
    approved_at: datetime | None = None,
    document_type: str = "resume",
    storage_path: str | None = "anon/rec-1.docx",
    job_posting_id: str | None = "job-1",
    payload_md: str | None = None,
    docx_payload_md_hash: str | None = None,
) -> TailoredResumeRecord:
    return TailoredResumeRecord(
        id="rec-1",
        user_id=None,
        job_posting_id=job_posting_id,
        document_type=document_type,
        resume_type="generic",
        jd_snapshot="JD text",
        jd_snapshot_hash="abc123",
        payload=_RESUME.model_dump(),
        payload_md=payload_md,
        docx_payload_md_hash=docx_payload_md_hash,
        storage_path=storage_path,
        warnings=[],
        model="claude-sonnet-4-6",
        input_tokens=100,
        output_tokens=50,
        cost_usd=0.001,
        latency_ms=50,
        created_at=_NOW,
        approved_at=approved_at,
    )


# ---------------------------------------------------------------------------
# Model validation
# ---------------------------------------------------------------------------


class TestResumeEditRequestValidation:
    def test_valid_markdown(self) -> None:
        req = ResumeEditRequest(markdown="# Daniel\n\n## Experience\n\n- did things")
        assert req.markdown.startswith("# Daniel")

    def test_rejects_empty_markdown(self) -> None:
        with pytest.raises(ValidationError):
            ResumeEditRequest(markdown="")

    def test_rejects_too_long_markdown(self) -> None:
        with pytest.raises(ValidationError):
            ResumeEditRequest(markdown="x" * 50_001)


class TestBulkExportRequestValidation:
    def test_valid_request(self) -> None:
        req = BulkExportRequest(resume_ids=["r-1", "r-2"])
        assert len(req.resume_ids) == 2

    def test_rejects_empty_list(self) -> None:
        with pytest.raises(ValidationError):
            BulkExportRequest(resume_ids=[])

    def test_rejects_over_20(self) -> None:
        with pytest.raises(ValidationError):
            BulkExportRequest(resume_ids=[f"r-{i}" for i in range(21)])


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


class TestPersistenceHelpers:
    def test_update_payload(self) -> None:
        from app.services.tailor.persistence import update_payload

        supabase = MagicMock()
        updated_record = _make_record()
        # Chain: update → eq(id) → is_(user_id) → execute (user_id=None path)
        supabase.table.return_value.update.return_value.eq.return_value.is_.return_value.execute.return_value.data = [
            updated_record.model_dump(mode="json")
        ]

        result = update_payload(supabase, "rec-1", {"summary": "Updated"}, user_id=None)
        assert result.id == "rec-1"
        supabase.table.assert_called_with("documents")

    def test_update_payload_with_storage_path(self) -> None:
        from app.services.tailor.persistence import update_payload

        supabase = MagicMock()
        updated_record = _make_record()
        supabase.table.return_value.update.return_value.eq.return_value.is_.return_value.execute.return_value.data = [
            updated_record.model_dump(mode="json")
        ]

        result = update_payload(
            supabase,
            "rec-1",
            {"summary": "Updated"},
            storage_path="anon/rec-1.docx",
            user_id=None,
        )
        assert result.id == "rec-1"
        # Verify the update call included storage_path
        call_args = supabase.table.return_value.update.call_args
        assert call_args[0][0]["storage_path"] == "anon/rec-1.docx"

    def test_approve(self) -> None:
        from app.services.tailor.persistence import approve

        supabase = MagicMock()
        approved_record = _make_record(approved_at=_NOW)
        supabase.table.return_value.update.return_value.eq.return_value.is_.return_value.execute.return_value.data = [
            approved_record.model_dump(mode="json")
        ]

        result = approve(supabase, "rec-1", user_id=None)
        assert result.id == "rec-1"
        assert result.approved_at is not None

    def test_get_by_job_found(self) -> None:
        from app.services.tailor.persistence import get_by_job

        supabase = MagicMock()
        record = _make_record()
        # Chain: select → eq(job) → eq(doctype) → is_(user_id) → order → limit → execute
        # (``user_id=None`` legacy path here)
        chain = supabase.table.return_value.select.return_value
        chain = chain.eq.return_value.eq.return_value.is_.return_value
        chain.order.return_value.limit.return_value.execute.return_value.data = [
            record.model_dump(mode="json")
        ]

        result = get_by_job(supabase, "job-1", user_id=None)
        assert result is not None
        assert result.id == "rec-1"

    def test_get_by_job_not_found(self) -> None:
        from app.services.tailor.persistence import get_by_job

        supabase = MagicMock()
        chain = supabase.table.return_value.select.return_value
        chain = chain.eq.return_value.eq.return_value.is_.return_value
        chain.order.return_value.limit.return_value.execute.return_value.data = []

        result = get_by_job(supabase, "nonexistent", user_id=None)
        assert result is None

    def test_get_by_job_filters_by_document_type(self) -> None:
        """Cover letter lookup must scope to document_type='cover_letter' so
        a resume on the same job posting doesn't shadow it."""
        from app.services.tailor.persistence import get_by_job

        supabase = MagicMock()
        chain = supabase.table.return_value.select.return_value
        chain = chain.eq.return_value.eq.return_value.is_.return_value
        chain.order.return_value.limit.return_value.execute.return_value.data = []

        get_by_job(supabase, "job-1", user_id=None, document_type="cover_letter")

        # Walk the .eq() calls and assert both the job + the document_type
        # filter were issued.
        eq_calls = supabase.table.return_value.select.return_value.eq.call_args_list
        nested_eq_calls = (
            supabase.table.return_value.select.return_value.eq.return_value.eq.call_args_list
        )
        all_eq_args = [c.args for c in eq_calls + nested_eq_calls]
        assert ("job_posting_id", "job-1") in all_eq_args
        assert ("document_type", "cover_letter") in all_eq_args

    def test_mark_job_resume_draft_does_not_touch_jobs_status(self) -> None:
        """#75 C3: per-user pipeline state lives in user_jobs; the helper no
        longer writes the global jobs.status. With no user_id it's a no-op."""
        from app.services.tailor.persistence import mark_job_resume_draft

        supabase = MagicMock()
        mark_job_resume_draft(supabase, "job-42", user_id=None)

        tables = [c.args[0] for c in supabase.table.call_args_list]
        assert "jobs" not in tables

    def test_mark_job_resume_draft_writes_user_jobs(self) -> None:
        """With a known user_id (#75 C3) the helper mirrors into user_jobs
        only (no global jobs.status write)."""
        from app.services.tailor.persistence import mark_job_resume_draft

        supabase = MagicMock()
        mark_job_resume_draft(supabase, "job-42", user_id="user-7")

        tables = [c.args[0] for c in supabase.table.call_args_list]
        assert "jobs" not in tables
        assert "user_jobs" in tables
        upsert_payload = supabase.table.return_value.upsert.call_args[0][0]
        assert upsert_payload["user_id"] == "user-7"
        assert upsert_payload["job_posting_id"] == "job-42"
        assert upsert_payload["status"] == "resume_draft"

    def test_mark_job_resume_draft_skips_user_jobs_for_api_key(self) -> None:
        """user_id=None (api-key/cron path) skips the mirror in C1."""
        from app.services.tailor.persistence import mark_job_resume_draft

        supabase = MagicMock()
        mark_job_resume_draft(supabase, "job-42", user_id=None)

        tables = [c.args[0] for c in supabase.table.call_args_list]
        assert "user_jobs" not in tables

    def test_upsert_user_job(self) -> None:
        from app.services.tailor.persistence import upsert_user_job

        supabase = MagicMock()
        upsert_user_job(
            supabase, user_id="user-1", job_posting_id="job-9", status="applied"
        )

        supabase.table.assert_called_with("user_jobs")
        upsert_call = supabase.table.return_value.upsert.call_args
        payload = upsert_call[0][0]
        assert payload["user_id"] == "user-1"
        assert payload["job_posting_id"] == "job-9"
        assert payload["status"] == "applied"
        assert "updated_at" in payload
        assert upsert_call.kwargs["on_conflict"] == "user_id,job_posting_id"


# ---------------------------------------------------------------------------
# Single-resume status bump
# ---------------------------------------------------------------------------


class TestSingleResumeStatusBump:
    """POST /tailor/resume must advance jobs.status to 'resume_draft'
    after a successful generation. Without this the JobDetailPanel never
    shows the 'Review Resume' button — the resume exists in the DB but is
    invisible to the user.
    """

    @pytest.mark.asyncio
    async def test_full_generation_marks_job_resume_draft(self) -> None:
        from app.models.tailor import TailorRequest
        from app.routers import tailor as tailor_router
        from app.services.tailor import PipelineSuccess

        supabase = MagicMock()
        llm = MagicMock()
        record = _make_record()

        success = PipelineSuccess(
            record=record,
            resume=_RESUME,
            warnings=[],
            lint=LintResult(ok=True, violations=[]),
            llm_result=MagicMock(),
        )

        with (
            patch("app.routers.tailor.optimized") as mock_opt,
            patch("app.routers.tailor.preferences") as mock_prefs,
            patch(
                "app.routers.tailor.resolve_contact",
                return_value=_CONTACT,
            ),
            patch(
                "app.routers.tailor.run_tailor_pipeline",
                return_value=success,
            ),
            patch(
                "app.services.tailor.persistence.mark_job_resume_draft"
            ) as mock_mark,
        ):
            mock_opt.get_latest.return_value = MagicMock(
                payload=MagicMock(roles=[MagicMock()], outcomes=[MagicMock()])
            )
            mock_prefs.get.return_value = None
            # Bypass the structural gap gate — we're testing the post-success path.
            with patch(
                "app.routers.tailor.gap_tracker.can_generate",
                return_value=MagicMock(ok=True),
            ):
                # force_fresh skips the reuse short-circuit so we hit the full
                # generation branch deterministically.
                await tailor_router.create_tailored_resume(
                    request=MagicMock(),
                    body=TailorRequest(
                        job_description="Build things.",
                        contact=_CONTACT,
                        job_posting_id="job-1",
                        force_fresh=True,
                    ),
                    supabase=supabase,
                    llm=llm,
                )

        # Dual-write threads user_id through (#75 C1); the route is invoked
        # without resolving the JWT dependency here, so just assert the
        # positional contract + that user_id is passed as a keyword.
        mock_mark.assert_called_once()
        assert mock_mark.call_args.args == (supabase, "job-1")
        assert "user_id" in mock_mark.call_args.kwargs

    @pytest.mark.asyncio
    async def test_no_status_bump_when_job_posting_id_missing(self) -> None:
        """One-off generations without a linked job (e.g. preview from a JD
        paste) should not touch any jobs row."""
        from app.models.tailor import TailorRequest
        from app.routers import tailor as tailor_router
        from app.services.tailor import PipelineSuccess

        supabase = MagicMock()
        llm = MagicMock()
        record = _make_record(job_posting_id=None)

        success = PipelineSuccess(
            record=record,
            resume=_RESUME,
            warnings=[],
            lint=LintResult(ok=True, violations=[]),
            llm_result=MagicMock(),
        )

        with (
            patch("app.routers.tailor.optimized") as mock_opt,
            patch("app.routers.tailor.preferences") as mock_prefs,
            patch(
                "app.routers.tailor.resolve_contact",
                return_value=_CONTACT,
            ),
            patch(
                "app.routers.tailor.run_tailor_pipeline",
                return_value=success,
            ),
            patch(
                "app.services.tailor.persistence.mark_job_resume_draft"
            ) as mock_mark,
        ):
            mock_opt.get_latest.return_value = MagicMock(
                payload=MagicMock(roles=[MagicMock()], outcomes=[MagicMock()])
            )
            mock_prefs.get.return_value = None
            with patch(
                "app.routers.tailor.gap_tracker.can_generate",
                return_value=MagicMock(ok=True),
            ):
                await tailor_router.create_tailored_resume(
                    request=MagicMock(),
                    body=TailorRequest(
                        job_description="Build things.",
                        contact=_CONTACT,
                        force_fresh=True,
                    ),
                    supabase=supabase,
                    llm=llm,
                )

        mock_mark.assert_not_called()


# ---------------------------------------------------------------------------
# Edit endpoint
# ---------------------------------------------------------------------------


class TestEditResume:
    _GOOD_MD = "# Daniel Joffe\n\n## Experience\n\n### Engineer — Acme\n\n- Did things\n"

    @pytest.mark.asyncio
    async def test_edit_success(self) -> None:
        from app.routers import tailor as tailor_router

        supabase = MagicMock()
        record = _make_record()
        updated_record = _make_record()

        with (
            patch(
                "app.services.tailor.persistence.get",
                return_value=record,
            ),
            patch(
                "app.services.tailor.persistence.update_payload_md",
                return_value=updated_record,
            ),
        ):
            result = await tailor_router.edit_tailored_resume(
                resume_id="rec-1",
                body=ResumeEditRequest(markdown=self._GOOD_MD),
                supabase=supabase,
            )

        assert result.record.id == "rec-1"

    @pytest.mark.asyncio
    async def test_edit_not_found(self) -> None:
        from fastapi import HTTPException

        from app.routers import tailor as tailor_router

        supabase = MagicMock()

        with (
            patch("app.services.tailor.persistence.get", return_value=None),
            pytest.raises(HTTPException) as exc_info,
        ):
            await tailor_router.edit_tailored_resume(
                resume_id="nonexistent",
                body=ResumeEditRequest(markdown=self._GOOD_MD),
                supabase=supabase,
            )
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_edit_rejected_if_approved(self) -> None:
        from fastapi import HTTPException

        from app.routers import tailor as tailor_router

        supabase = MagicMock()
        record = _make_record(approved_at=_NOW)

        with (
            patch("app.services.tailor.persistence.get", return_value=record),
            pytest.raises(HTTPException) as exc_info,
        ):
            await tailor_router.edit_tailored_resume(
                resume_id="rec-1",
                body=ResumeEditRequest(markdown=self._GOOD_MD),
                supabase=supabase,
            )
        assert exc_info.value.status_code == 409

    @pytest.mark.asyncio
    async def test_edit_succeeds_for_cover_letter(self) -> None:
        """Cover letters share the markdown editor + autosave path; the
        linter skips resume-specific section name checks for them, so a
        plain prose body that would fail resume lint is still valid here."""
        from app.routers import tailor as tailor_router

        supabase = MagicMock()
        record = _make_record(document_type="cover_letter")
        # Plain cover letter prose (no `## Experience` heading) — would
        # fail the resume linter, must pass the cover letter linter.
        cover_md = (
            "# Daniel Joffe\n\n"
            "Dear Hiring Manager,\n\n"
            "I'm writing to apply for the role.\n\n"
            "Sincerely,\nDaniel\n"
        )

        with (
            patch("app.services.tailor.persistence.get", return_value=record),
            patch(
                "app.services.tailor.persistence.update_payload_md",
                return_value=record,
            ),
        ):
            result = await tailor_router.edit_tailored_resume(
                resume_id="rec-1",
                body=ResumeEditRequest(markdown=cover_md),
                supabase=supabase,
            )

        assert result.record == record

    @pytest.mark.asyncio
    async def test_edit_lint_failure_missing_experience(self) -> None:
        from fastapi import HTTPException

        from app.routers import tailor as tailor_router

        supabase = MagicMock()
        record = _make_record()

        # No `## Experience` heading -> markdown lint blocks the save.
        bad_md = "# Daniel Joffe\n\n## Skills\n\nPython\n"

        with (
            patch("app.services.tailor.persistence.get", return_value=record),
            pytest.raises(HTTPException) as exc_info,
        ):
            await tailor_router.edit_tailored_resume(
                resume_id="rec-1",
                body=ResumeEditRequest(markdown=bad_md),
                supabase=supabase,
            )
        assert exc_info.value.status_code == 422


# ---------------------------------------------------------------------------
# Approve endpoint
# ---------------------------------------------------------------------------


class TestApproveResume:
    @pytest.mark.asyncio
    async def test_approve_success(self) -> None:
        from app.routers import tailor as tailor_router

        supabase = MagicMock()
        record = _make_record()
        approved_record = _make_record(approved_at=_NOW)

        with (
            patch("app.services.tailor.persistence.get", return_value=record),
            patch(
                "app.services.tailor.persistence.approve",
                return_value=approved_record,
            ),
        ):
            result = await tailor_router.approve_tailored_resume(
                resume_id="rec-1",
                supabase=supabase,
            )

        assert result.approved_at is not None
        # #75 C3: with no JWT user_id there's no per-user pipeline to write,
        # and the global jobs.status is no longer touched.
        for call in supabase.table.call_args_list:
            assert call.args[0] != "jobs"

    @pytest.mark.asyncio
    async def test_approve_idempotent(self) -> None:
        from app.routers import tailor as tailor_router

        supabase = MagicMock()
        already_approved = _make_record(approved_at=_NOW)

        with patch(
            "app.services.tailor.persistence.get",
            return_value=already_approved,
        ):
            result = await tailor_router.approve_tailored_resume(
                resume_id="rec-1",
                supabase=supabase,
            )

        assert result.approved_at is not None

    @pytest.mark.asyncio
    async def test_approve_not_found(self) -> None:
        from fastapi import HTTPException

        from app.routers import tailor as tailor_router

        supabase = MagicMock()

        with (
            patch("app.services.tailor.persistence.get", return_value=None),
            pytest.raises(HTTPException) as exc_info,
        ):
            await tailor_router.approve_tailored_resume(
                resume_id="nonexistent",
                supabase=supabase,
            )
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_approve_cover_letter_does_not_advance_job_status(self) -> None:
        """Approving a cover letter locks it but must not flip the linked
        job posting to resume_ready — that's a resume-only side effect."""
        from app.routers import tailor as tailor_router

        supabase = MagicMock()
        record = _make_record(document_type="cover_letter")
        approved_record = _make_record(
            document_type="cover_letter", approved_at=_NOW
        )

        with (
            patch("app.services.tailor.persistence.get", return_value=record),
            patch(
                "app.services.tailor.persistence.approve",
                return_value=approved_record,
            ),
        ):
            result = await tailor_router.approve_tailored_resume(
                resume_id="rec-1",
                supabase=supabase,
            )

        assert result.approved_at is not None
        # No jobs.update — cover letters don't drive job status.
        for call in supabase.table.call_args_list:
            assert call.args[0] != "jobs"

    @pytest.mark.asyncio
    async def test_approve_writes_user_jobs(self) -> None:
        """#75 C3: approving a resume with a JWT user_id writes the
        resume_ready status into user_jobs only (no global jobs.status)."""
        from app.routers import tailor as tailor_router

        supabase = MagicMock()
        record = _make_record()
        approved_record = _make_record(approved_at=_NOW)

        with (
            patch("app.services.tailor.persistence.get", return_value=record),
            patch(
                "app.services.tailor.persistence.approve",
                return_value=approved_record,
            ),
        ):
            await tailor_router.approve_tailored_resume(
                resume_id="rec-1",
                supabase=supabase,
                user_id="user-7",
            )

        tables = [c.args[0] for c in supabase.table.call_args_list]
        assert "jobs" not in tables
        assert "user_jobs" in tables
        upsert_payload = supabase.table.return_value.upsert.call_args[0][0]
        assert upsert_payload["user_id"] == "user-7"
        assert upsert_payload["status"] == "resume_ready"

    @pytest.mark.asyncio
    async def test_approve_skips_user_jobs_for_api_key(self) -> None:
        """api-key callers (user_id None) have no per-user pipeline, so they
        write neither user_jobs nor the (now-untouched) global jobs.status."""
        from app.routers import tailor as tailor_router

        supabase = MagicMock()
        record = _make_record()
        approved_record = _make_record(approved_at=_NOW)

        with (
            patch("app.services.tailor.persistence.get", return_value=record),
            patch(
                "app.services.tailor.persistence.approve",
                return_value=approved_record,
            ),
        ):
            await tailor_router.approve_tailored_resume(
                resume_id="rec-1",
                supabase=supabase,
                user_id=None,
            )

        tables = [c.args[0] for c in supabase.table.call_args_list]
        assert "jobs" not in tables
        assert "user_jobs" not in tables

    @pytest.mark.asyncio
    async def test_unapprove_writes_user_jobs(self) -> None:
        """#75 C3: unapproving a resume with a JWT user_id writes the
        resume_draft status into user_jobs only (no global jobs.status)."""
        from app.routers import tailor as tailor_router

        supabase = MagicMock()
        approved = _make_record(approved_at=_NOW)
        reopened = _make_record(approved_at=None)

        with (
            patch("app.services.tailor.persistence.get", return_value=approved),
            patch(
                "app.services.tailor.persistence.unapprove",
                return_value=reopened,
            ),
        ):
            await tailor_router.unapprove_tailored_resume(
                resume_id="rec-1",
                supabase=supabase,
                user_id="user-7",
            )

        tables = [c.args[0] for c in supabase.table.call_args_list]
        assert "jobs" not in tables
        assert "user_jobs" in tables
        upsert_payload = supabase.table.return_value.upsert.call_args[0][0]
        assert upsert_payload["user_id"] == "user-7"
        assert upsert_payload["status"] == "resume_draft"


# ---------------------------------------------------------------------------
# Export zip endpoint
# ---------------------------------------------------------------------------


class TestExportZip:
    @pytest.mark.asyncio
    async def test_export_zip_success(self) -> None:
        import zipfile as zf
        from io import BytesIO

        from app.routers import tailor as tailor_router

        supabase = MagicMock()
        record = _make_record(approved_at=_NOW)

        with (
            patch("app.services.tailor.persistence.get", return_value=record),
            patch(
                "app.services.tailor.persistence.download_docx",
                return_value=b"fake-docx",
            ),
        ):
            result = await tailor_router.export_resumes_zip(
                body=BulkExportRequest(resume_ids=["rec-1"]),
                supabase=supabase,
            )

        assert result.media_type == "application/zip"
        # Verify it's a valid zip
        with zf.ZipFile(BytesIO(result.body)) as z:
            assert len(z.namelist()) == 1
            name = z.namelist()[0]
            assert name.endswith(".docx")
            assert "Acme" in name

    @pytest.mark.asyncio
    async def test_export_zip_rejects_unapproved(self) -> None:
        from fastapi import HTTPException

        from app.routers import tailor as tailor_router

        supabase = MagicMock()
        unapproved = _make_record(approved_at=None)

        with (
            patch("app.services.tailor.persistence.get", return_value=unapproved),
            pytest.raises(HTTPException) as exc_info,
        ):
            await tailor_router.export_resumes_zip(
                body=BulkExportRequest(resume_ids=["rec-1"]),
                supabase=supabase,
            )
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_export_zip_not_found(self) -> None:
        from fastapi import HTTPException

        from app.routers import tailor as tailor_router

        supabase = MagicMock()

        with (
            patch("app.services.tailor.persistence.get", return_value=None),
            pytest.raises(HTTPException) as exc_info,
        ):
            await tailor_router.export_resumes_zip(
                body=BulkExportRequest(resume_ids=["nonexistent"]),
                supabase=supabase,
            )
        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# Get by job endpoint
# ---------------------------------------------------------------------------


class TestGetByJob:
    @pytest.mark.asyncio
    async def test_get_by_job_found(self) -> None:
        from app.routers import tailor as tailor_router

        supabase = MagicMock()
        record = _make_record()

        with patch(
            "app.services.tailor.persistence.get_by_job",
            return_value=record,
        ):
            result = await tailor_router.get_resume_by_job(
                job_posting_id="job-1",
                supabase=supabase,
            )

        assert result.id == "rec-1"
        assert result.job_posting_id == "job-1"

    @pytest.mark.asyncio
    async def test_get_by_job_returns_none_when_missing(self) -> None:
        """The route returns ``None`` (200 + null body) instead of raising
        a 404 when no record exists — the FE consumer treats null as
        "no record yet, render the Generate CTA", and dropping the 404
        avoids polluting Sentry with a console error on every
        job-detail visit before generation.
        """
        from app.routers import tailor as tailor_router

        supabase = MagicMock()

        with patch(
            "app.services.tailor.persistence.get_by_job",
            return_value=None,
        ):
            result = await tailor_router.get_resume_by_job(
                job_posting_id="nonexistent",
                supabase=supabase,
            )
        assert result is None


# ---------------------------------------------------------------------------
# Markdown payload + docx cache invalidation
# ---------------------------------------------------------------------------


_GOOD_MD = "# Daniel Joffe\n\n## Experience\n\n### Engineer — Acme\n\n- Did things\n"


class TestUpdatePayloadMd:
    """`update_payload_md` is the persistence half of PATCH /resumes/{id}.
    It writes the markdown and sets docx_payload_md_hash to NULL so the
    next download triggers a re-render. Version snapshots are NOT taken
    here — autosave fires on every keystroke (debounced), and snapshotting
    each one would flood the free-tier cap. Snapshots come from explicit
    `versions.checkpoint` calls (session-end flush, pre-approve, pre-readapt).
    """

    def test_invalidates_docx_cache_hash(self) -> None:
        from app.services.tailor.persistence import update_payload_md

        supabase = MagicMock()
        updated = _make_record(payload_md=_GOOD_MD, docx_payload_md_hash=None)
        supabase.table.return_value.update.return_value.eq.return_value.is_.return_value.execute.return_value.data = [
            updated.model_dump(mode="json")
        ]

        with patch("app.services.tailor.versions.record") as mock_record:
            update_payload_md(supabase, "rec-1", _GOOD_MD, user_id=None)

        # Update payload includes the markdown and explicitly NULLs the cache hash.
        update_call = supabase.table.return_value.update.call_args[0][0]
        assert update_call["payload_md"] == _GOOD_MD
        assert update_call["docx_payload_md_hash"] is None
        # No version snapshot — those come from `versions.checkpoint`.
        mock_record.assert_not_called()


class TestCheckpointEndpoint:
    """POST /tailor/resumes/{id}/checkpoint — writes a `user_edit` version
    snapshot of the current draft. Two callers:
    - `navigator.sendBeacon` on pagehide (with `markdown` body): flushes
      a not-yet-saved edit before snapshotting.
    - Explicit pre-approve / pre-readapt (no body): snapshot whatever is
      already in the row.

    Dedup is critical: routine autosave produces many no-op checkpoints
    that would otherwise blow through the 5-version free-tier cap in a
    single editing session.
    """

    @pytest.mark.asyncio
    async def test_no_body_snapshots_current_state(self) -> None:
        from app.models.tailor import ResumeCheckpointRequest
        from app.routers import tailor as tailor_router

        supabase = MagicMock()
        record = _make_record(payload_md=_GOOD_MD)

        with (
            patch("app.services.tailor.persistence.get", return_value=record),
            patch(
                "app.services.tailor.versions.checkpoint",
                return_value=True,
            ) as mock_checkpoint,
            patch(
                "app.services.tailor.persistence.update_payload_md"
            ) as mock_update,
        ):
            result = await tailor_router.checkpoint_tailored_resume(
                resume_id="rec-1",
                body=ResumeCheckpointRequest(),
                supabase=supabase,
            )

        assert result == {"recorded": True}
        mock_update.assert_not_called()
        mock_checkpoint.assert_called_once_with(supabase, "rec-1")

    @pytest.mark.asyncio
    async def test_body_with_markdown_saves_then_checkpoints(self) -> None:
        from app.models.tailor import ResumeCheckpointRequest
        from app.routers import tailor as tailor_router

        supabase = MagicMock()
        record = _make_record(payload_md="old md")
        new_md = "# New\n\n## Experience\n\n### Eng — Acme\n\n- Did things\n"

        with (
            patch("app.services.tailor.persistence.get", return_value=record),
            patch(
                "app.services.tailor.persistence.update_payload_md"
            ) as mock_update,
            patch(
                "app.services.tailor.versions.checkpoint",
                return_value=True,
            ) as mock_checkpoint,
        ):
            await tailor_router.checkpoint_tailored_resume(
                resume_id="rec-1",
                body=ResumeCheckpointRequest(markdown=new_md),
                supabase=supabase,
            )

        # Save lands first so checkpoint reads the fresh markdown.
        # ``user_id`` kwarg comes from the route's ``Depends`` default
        # when called directly without a real request — the test
        # exercises the call shape, not the resolved value.
        mock_update.assert_called_once()
        args, kwargs = mock_update.call_args
        assert args[:3] == (supabase, "rec-1", new_md)
        assert "user_id" in kwargs
        mock_checkpoint.assert_called_once_with(supabase, "rec-1")

    @pytest.mark.asyncio
    async def test_body_with_invalid_markdown_returns_422(self) -> None:
        from fastapi import HTTPException

        from app.models.tailor import ResumeCheckpointRequest
        from app.routers import tailor as tailor_router

        supabase = MagicMock()
        record = _make_record()
        # No `## Experience` heading — fails markdown lint.
        bad_md = "# Daniel\n\n## Skills\n\nPython\n"

        with (
            patch("app.services.tailor.persistence.get", return_value=record),
            patch(
                "app.services.tailor.versions.checkpoint"
            ) as mock_checkpoint,
            pytest.raises(HTTPException) as exc_info,
        ):
            await tailor_router.checkpoint_tailored_resume(
                resume_id="rec-1",
                body=ResumeCheckpointRequest(markdown=bad_md),
                supabase=supabase,
            )

        assert exc_info.value.status_code == 422
        mock_checkpoint.assert_not_called()

    @pytest.mark.asyncio
    async def test_approved_resume_skips_checkpoint(self) -> None:
        from app.models.tailor import ResumeCheckpointRequest
        from app.routers import tailor as tailor_router

        supabase = MagicMock()
        record = _make_record(approved_at=_NOW)

        with (
            patch("app.services.tailor.persistence.get", return_value=record),
            patch(
                "app.services.tailor.versions.checkpoint"
            ) as mock_checkpoint,
        ):
            result = await tailor_router.checkpoint_tailored_resume(
                resume_id="rec-1",
                body=ResumeCheckpointRequest(),
                supabase=supabase,
            )

        assert result == {"recorded": False, "reason": "approved"}
        mock_checkpoint.assert_not_called()

    @pytest.mark.asyncio
    async def test_not_found_returns_404(self) -> None:
        from fastapi import HTTPException

        from app.models.tailor import ResumeCheckpointRequest
        from app.routers import tailor as tailor_router

        supabase = MagicMock()

        with (
            patch("app.services.tailor.persistence.get", return_value=None),
            pytest.raises(HTTPException) as exc_info,
        ):
            await tailor_router.checkpoint_tailored_resume(
                resume_id="missing",
                body=ResumeCheckpointRequest(),
                supabase=supabase,
            )
        assert exc_info.value.status_code == 404


class TestMarkDocxRendered:
    """Atomically writes the rendered storage_path + the markdown hash that
    produced it. Guarantees `docx_payload_md_hash != None` only after a
    real upload exists at storage_path.
    """

    def test_writes_both_storage_path_and_hash(self) -> None:
        from app.services.tailor.persistence import mark_docx_rendered

        supabase = MagicMock()
        mark_docx_rendered(
            supabase,
            "rec-1",
            storage_path="anon/rec-1.docx",
            payload_md_hash="hash-xyz",
            user_id=None,
        )

        update_call = supabase.table.return_value.update.call_args[0][0]
        assert update_call["storage_path"] == "anon/rec-1.docx"
        assert update_call["docx_payload_md_hash"] == "hash-xyz"
        supabase.table.return_value.update.return_value.eq.assert_called_with(
            "id", "rec-1"
        )


class TestDownloadCache:
    """Hash-based cache invalidation in GET /resumes/{id}/download.

    Behaviour matrix:
    - cache fresh (hash matches): serve cached bytes, no pandoc call.
    - cache stale: re-render via pandoc, mark_docx_rendered, return new bytes.
    - legacy row (no payload_md, has storage_path): serve cached bytes.
    - no payload_md AND no storage_path: 404.
    """

    @pytest.mark.asyncio
    async def test_cache_fresh_serves_cached_bytes(self) -> None:
        from app.routers import tailor as tailor_router
        from app.services.docx.pandoc_render import md_payload_hash

        supabase = MagicMock()
        record = _make_record(
            payload_md=_GOOD_MD,
            docx_payload_md_hash=md_payload_hash(_GOOD_MD),
            storage_path="anon/rec-1.docx",
        )

        with (
            patch("app.services.tailor.persistence.get", return_value=record),
            patch(
                "app.services.tailor.persistence.download_docx",
                return_value=b"PKcached-bytes",
            ) as mock_download,
            patch("app.routers.tailor.md_to_docx") as mock_render,
            patch(
                "app.services.tailor.persistence.mark_docx_rendered"
            ) as mock_mark,
        ):
            user_supabase = MagicMock()
            response = await tailor_router.download_tailored_resume(
                resume_id="rec-1",
                supabase=supabase,
                user_supabase=user_supabase,
                user_id="test-user",
            )

        assert response.body == b"PKcached-bytes"
        # Downloads go through the JWT-bound user client (storage RLS).
        mock_download.assert_called_once_with(user_supabase, "anon/rec-1.docx")
        mock_render.assert_not_called()
        mock_mark.assert_not_called()

    @pytest.mark.asyncio
    async def test_cache_stale_rerenders_and_marks(self) -> None:
        from app.routers import tailor as tailor_router

        supabase = MagicMock()
        # Stale: stored hash doesn't match what payload_md hashes to now.
        record = _make_record(
            payload_md=_GOOD_MD,
            docx_payload_md_hash="stale-hash",
            storage_path="anon/rec-1.docx",
        )

        with (
            patch("app.services.tailor.persistence.get", return_value=record),
            patch(
                "app.routers.tailor.md_to_docx",
                return_value=b"PKfresh-bytes",
            ) as mock_render,
            patch(
                "app.services.tailor.persistence.upload_docx",
                return_value="anon/rec-1.docx",
            ),
            patch(
                "app.services.tailor.persistence.mark_docx_rendered"
            ) as mock_mark,
        ):
            response = await tailor_router.download_tailored_resume(
                resume_id="rec-1",
                supabase=supabase,
            )

        assert response.body == b"PKfresh-bytes"
        # Style resolves to None here (no per-record override, no user default),
        # so the render is the unstyled pandoc path — same output as before.
        mock_render.assert_called_once_with(_GOOD_MD, None)
        # mark_docx_rendered receives the freshly computed hash, not the stale one.
        from app.services.docx.pandoc_render import md_payload_hash

        mock_mark.assert_called_once()
        args, kwargs = mock_mark.call_args
        assert args == (supabase, "rec-1")
        assert kwargs["storage_path"] == "anon/rec-1.docx"
        assert kwargs["payload_md_hash"] == md_payload_hash(_GOOD_MD)
        assert "user_id" in kwargs

    @pytest.mark.asyncio
    async def test_cache_stale_render_succeeds_even_if_upload_fails(self) -> None:
        """If storage upload errors, the user still gets the freshly rendered
        bytes — the docx isn't lost. Next download retries the cache write.
        """
        from app.routers import tailor as tailor_router

        supabase = MagicMock()
        record = _make_record(
            payload_md=_GOOD_MD,
            docx_payload_md_hash=None,
            storage_path=None,
        )

        with (
            patch("app.services.tailor.persistence.get", return_value=record),
            patch(
                "app.routers.tailor.md_to_docx",
                return_value=b"PKfresh-bytes",
            ),
            patch(
                "app.services.tailor.persistence.upload_docx",
                side_effect=RuntimeError("storage down"),
            ),
        ):
            response = await tailor_router.download_tailored_resume(
                resume_id="rec-1",
                supabase=supabase,
            )

        assert response.body == b"PKfresh-bytes"

    @pytest.mark.asyncio
    async def test_legacy_row_serves_cached_bytes(self) -> None:
        """Pre-backfill rows with storage_path but no payload_md still work."""
        from app.routers import tailor as tailor_router

        supabase = MagicMock()
        record = _make_record(
            payload_md=None,
            docx_payload_md_hash=None,
            storage_path="anon/rec-1.docx",
        )

        with (
            patch("app.services.tailor.persistence.get", return_value=record),
            patch(
                "app.services.tailor.persistence.download_docx",
                return_value=b"PKlegacy-bytes",
            ) as mock_download,
            patch("app.routers.tailor.md_to_docx") as mock_render,
        ):
            user_supabase = MagicMock()
            response = await tailor_router.download_tailored_resume(
                resume_id="rec-1",
                supabase=supabase,
                user_supabase=user_supabase,
                user_id="test-user",
            )

        assert response.body == b"PKlegacy-bytes"
        mock_download.assert_called_once_with(user_supabase, "anon/rec-1.docx")
        mock_render.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_md_no_storage_returns_404(self) -> None:
        from fastapi import HTTPException

        from app.routers import tailor as tailor_router

        supabase = MagicMock()
        record = _make_record(
            payload_md=None,
            docx_payload_md_hash=None,
            storage_path=None,
        )

        with (
            patch("app.services.tailor.persistence.get", return_value=record),
            pytest.raises(HTTPException) as exc_info,
        ):
            await tailor_router.download_tailored_resume(
                resume_id="rec-1",
                supabase=supabase,
            )
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_pandoc_missing_returns_500(self) -> None:
        from fastapi import HTTPException

        from app.routers import tailor as tailor_router
        from app.services.docx.pandoc_render import PandocNotInstalledError

        supabase = MagicMock()
        record = _make_record(
            payload_md=_GOOD_MD,
            docx_payload_md_hash=None,
            storage_path=None,
        )

        with (
            patch("app.services.tailor.persistence.get", return_value=record),
            patch(
                "app.routers.tailor.md_to_docx",
                side_effect=PandocNotInstalledError("pandoc missing"),
            ),
            pytest.raises(HTTPException) as exc_info,
        ):
            await tailor_router.download_tailored_resume(
                resume_id="rec-1",
                supabase=supabase,
            )
        assert exc_info.value.status_code == 500
