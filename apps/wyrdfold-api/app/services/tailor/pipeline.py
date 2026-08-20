"""End-to-end tailor pipeline (#185 P3d).

Glue between the four isolated units:
  tailor_resume (P3a)  -> render_docx (P3b) -> lint_docx (P3c) -> persist

Splits cleanly between "LLM synthesis" (can fail on hallucination
trace-check with ValueError) and "format check" (returns LintResult).

Lint errors short-circuit the *rendering* pipeline but no longer discard the
work (#656): a document that fails ATS lint is persisted as a flagged draft
carrying its violations, so the generation — now backgrounded, and charged
against the user's daily cap — isn't lost when nobody is watching. The user
edits the draft and re-checks (free; lint is deterministic) instead of paying
to regenerate. **Resumes and cover letters behave identically here**: letters
run the same linter, so the resume-only carve-out was dropped.

The router layer just calls `run_tailor_pipeline(...)` and converts the
result into an HTTP response.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from supabase import AsyncClient

from app.config import settings
from app.models.ats_lint import LintResult
from app.models.experience import OptimizedDoc, PreferencesPayload
from app.models.llm import LLMResult
from app.models.tailor import (
    ContactInfo,
    ResumeType,
    TailoredCoverLetter,
    TailoredResume,
    TailoredResumeRecord,
)
from app.services.ats_lint import lint_docx, lint_markdown
from app.services.docx.pandoc_render import md_to_docx
from app.services.experience.annotations import (
    apply_exclusions,
    build_annotations_text,
    resolve_for_target,
)
from app.services.llm import cost_log
from app.services.llm.client import LLMClient
from app.services.tailor import persistence
from app.services.tailor.faithfulness import (
    FAITHFULNESS_REVIEW_PURPOSE,
    review_resume_faithfulness,
    review_to_critique,
)
from app.services.tailor.markdown_render import (
    to_markdown,
    to_markdown_cover_letter,
)
from app.services.tailor.tailor import (
    DEFAULT_COVER_LETTER_PURPOSE,
    DEFAULT_PURPOSE,
    tailor_cover_letter,
    tailor_resume,
)


@dataclass
class PipelineSuccess:
    record: TailoredResumeRecord
    resume: TailoredResume
    warnings: list[str]
    lint: LintResult
    llm_result: LLMResult


@dataclass
class PipelineLintFailure:
    """A generated resume that failed ATS lint — persisted FLAGGED (#656).

    ``record`` is a real ``documents`` row with ``lint_violations`` populated,
    NOT a discarded result: the caller surfaces the violations against a draft
    the user can edit and re-check, rather than 422-ing away work that already
    cost an LLM call and a slot in the daily cap.
    """

    lint: LintResult
    resume: TailoredResume
    warnings: list[str]
    llm_result: LLMResult
    record: TailoredResumeRecord
    payload_md: str


PipelineResult = PipelineSuccess | PipelineLintFailure


async def run_tailor_pipeline(
    supabase: AsyncClient,
    llm: LLMClient,
    *,
    user_id: str | None,
    optimized: OptimizedDoc,
    job_description: str,
    contact: ContactInfo,
    preferences: PreferencesPayload | None = None,
    critique: str | None = None,
    resume_type: ResumeType = "generic",
    page_budget: int = 2,
    job_posting_id: str | None = None,
    target_label: str | None = None,
) -> PipelineResult:
    """Run the full tailor pipeline end-to-end.

    Returns PipelineSuccess on clean lint, PipelineLintFailure when the
    rendered doc has blocking errors. On lint failure nothing is
    persisted and no `.docx` is uploaded — the caller should surface the
    violations and retry with a critique.
    """
    if user_id is None:
        # Generated .docx is stored under the caller's <user_id>/ Storage
        # folder — there is no anonymous tailoring path anymore.
        raise ValueError("tailored generation requires an authenticated user")
    # Resolve annotations for the target (#499)
    emphasize, exclude, de_emph = resolve_for_target(optimized.payload.annotations, target_label)
    filtered_payload = apply_exclusions(optimized.payload, exclude)
    annotations_text = build_annotations_text(emphasize, de_emph)

    async def _generate(
        crit: str | None,
    ) -> tuple[TailoredResume, list[str], LLMResult]:
        """One generation pass + its cost-log. Reused for the corrective
        regen so the review-pass doesn't duplicate the (long) call."""
        gen_resume, gen_warnings, gen_result = await tailor_resume(
            llm,
            optimized=filtered_payload,
            job_description=job_description,
            contact=contact,
            resume_type=resume_type,
            preferences_rules=(preferences.rules if preferences else None),
            preferences_avoid=(preferences.avoid if preferences else None),
            preferences_tone_notes=(preferences.tone_notes if preferences else None),
            annotations_text=annotations_text,
            critique=crit,
            page_budget=page_budget,
        )
        await cost_log.record_async(
            supabase,
            user_id=user_id,
            purpose=DEFAULT_PURPOSE,
            result=gen_result,
            metadata={
                "optimized_doc_id": optimized.id,
                "job_posting_id": job_posting_id or "",
            },
        )
        return gen_resume, gen_warnings, gen_result

    resume, trace_warnings, llm_result = await _generate(critique)

    # Faithfulness review pass (#6b). Flag claims the source doesn't support;
    # on medium/high-severity flags, regenerate ONCE with the flags folded into
    # the critique. The corrective run is NOT re-reviewed — a single
    # generate -> review -> fix cycle, never a loop.
    if settings.faithfulness_review_enabled:
        review, review_result = await review_resume_faithfulness(
            llm, resume=resume, optimized=filtered_payload
        )
        await cost_log.record_async(
            supabase,
            user_id=user_id,
            purpose=FAITHFULNESS_REVIEW_PURPOSE,
            result=review_result,
            metadata={
                "optimized_doc_id": optimized.id,
                "job_posting_id": job_posting_id or "",
            },
        )
        fix_critique = review_to_critique(review)
        if fix_critique is not None:
            combined = "\n\n".join(c for c in (critique, fix_critique) if c)
            resume, trace_warnings, llm_result = await _generate(combined)

    payload_md = to_markdown(resume)

    async def _persist(lint_result: LintResult) -> TailoredResumeRecord:
        """Insert the ``documents`` row carrying this run's lint state.

        The stored list is the *decisive* lint result verbatim — warnings
        included, not just the blocking errors. Post-#656 the POST returns 202
        and the client polls for the record, so the transient
        ``TailorResponse.lint_warnings`` channel no longer reaches the user on
        a backgrounded run; persisting the whole list is what keeps advisories
        on a clean draft from vanishing. Hence the column's three states are
        really four: ``NULL`` never linted, ``[]`` nothing to report, a
        warnings-only list = clean with advisories, and any ``severity ==
        "error"`` entry = flagged draft.
        """
        return await persistence.persist(
            supabase,
            user_id=user_id,
            job_posting_id=job_posting_id,
            resume=resume,
            payload_md=payload_md,
            job_description=job_description,
            warnings=trace_warnings,
            llm_result=llm_result,
            storage_path=None,
            lint_violations=[v.model_dump() for v in lint_result.violations],
        )

    md_lint = lint_markdown(payload_md, document_type="resume")
    if not md_lint.ok:
        # Flagged, not discarded (#656). No .docx is rendered or uploaded for a
        # flagged draft — the download route re-renders lazily from
        # payload_md when the hash is stale, so the failure path skips a
        # pandoc subprocess and a Storage round-trip it would only throw away
        # the moment the user edits.
        return PipelineLintFailure(
            lint=md_lint,
            resume=resume,
            warnings=trace_warnings,
            llm_result=llm_result,
            record=await _persist(md_lint),
            payload_md=payload_md,
        )
    # pandoc is a sync subprocess; offload to a worker thread so the event
    # loop keeps serving other requests during the ~hundreds-of-ms render.
    docx_bytes = await asyncio.to_thread(md_to_docx, payload_md)
    lint = lint_docx(docx_bytes)
    if not lint.ok:
        return PipelineLintFailure(
            lint=lint,
            resume=resume,
            warnings=trace_warnings,
            llm_result=llm_result,
            record=await _persist(lint),
            payload_md=payload_md,
        )

    record = await _persist(lint)
    try:
        storage_path = await persistence.upload_docx(
            supabase,
            user_id=user_id,
            resume_id=record.id,
            docx_bytes=docx_bytes,
        )
    except Exception:
        storage_path = None
    if storage_path:
        await (
            supabase.table(persistence.TABLE)
            .update({"storage_path": storage_path})
            .eq("id", record.id)
            .execute()
        )
        record = record.model_copy(update={"storage_path": storage_path})

    return PipelineSuccess(
        record=record,
        resume=resume,
        warnings=trace_warnings,
        lint=lint,
        llm_result=llm_result,
    )


# ---------------------------------------------------------------------------
# Cover letter pipeline
# ---------------------------------------------------------------------------


@dataclass
class CoverLetterPipelineSuccess:
    record: TailoredResumeRecord
    letter: TailoredCoverLetter
    warnings: list[str]
    lint: LintResult
    llm_result: LLMResult


@dataclass
class CoverLetterPipelineLintFailure:
    """A generated cover letter that failed ATS lint — persisted FLAGGED.

    Mirrors ``PipelineLintFailure``: ``record`` is a real ``documents`` row
    with ``lint_violations`` populated, not a discarded result. The letter runs
    the same linter as a resume and costs the same daily-cap slot, so it gets
    the same treatment (the earlier resume-only carve-out is gone).
    """

    lint: LintResult
    letter: TailoredCoverLetter
    warnings: list[str]
    llm_result: LLMResult
    record: TailoredResumeRecord
    payload_md: str


CoverLetterPipelineResult = CoverLetterPipelineSuccess | CoverLetterPipelineLintFailure


async def run_cover_letter_pipeline(
    supabase: AsyncClient,
    llm: LLMClient,
    *,
    user_id: str | None,
    optimized: OptimizedDoc,
    job_description: str,
    company_name: str,
    contact: ContactInfo,
    role_title: str | None = None,
    preferences: PreferencesPayload | None = None,
    critique: str | None = None,
    job_posting_id: str | None = None,
    target_label: str | None = None,
    allow_stretch: bool = False,
) -> CoverLetterPipelineResult:
    """Run the full cover-letter pipeline end-to-end.

    Returns CoverLetterPipelineSuccess on clean lint, CoverLetterPipelineLintFailure
    when the rendered doc has blocking errors. On lint failure nothing is
    persisted and no `.docx` is uploaded — the caller should surface the
    violations and retry with a critique.
    """
    if user_id is None:
        # Stored under the caller's <user_id>/ Storage folder — no anonymous path.
        raise ValueError("cover-letter generation requires an authenticated user")
    # Resolve annotations for the target (#499)
    emphasize, exclude, de_emph = resolve_for_target(optimized.payload.annotations, target_label)
    filtered_payload = apply_exclusions(optimized.payload, exclude)
    annotations_text = build_annotations_text(emphasize, de_emph)

    letter, trace_warnings, llm_result = await tailor_cover_letter(
        llm,
        optimized=filtered_payload,
        job_description=job_description,
        company_name=company_name,
        contact=contact,
        role_title=role_title,
        preferences_rules=(preferences.rules if preferences else None),
        preferences_avoid=(preferences.avoid if preferences else None),
        preferences_tone_notes=(preferences.tone_notes if preferences else None),
        annotations_text=annotations_text,
        critique=critique,
        allow_stretch=allow_stretch,
    )

    await cost_log.record_async(
        supabase,
        user_id=user_id,
        purpose=DEFAULT_COVER_LETTER_PURPOSE,
        result=llm_result,
        metadata={
            "optimized_doc_id": optimized.id,
            "job_posting_id": job_posting_id or "",
            "recipient_company": company_name,
        },
    )

    payload_md = to_markdown_cover_letter(letter)

    async def _persist(lint_result: LintResult) -> TailoredResumeRecord:
        """Insert the row carrying this run's lint state — see the resume
        pipeline's ``_persist`` for why the WHOLE violation list is stored,
        warnings included, rather than just the blocking errors."""
        return await persistence.persist_cover_letter(
            supabase,
            user_id=user_id,
            job_posting_id=job_posting_id,
            letter=letter,
            payload_md=payload_md,
            job_description=job_description,
            warnings=trace_warnings,
            llm_result=llm_result,
            storage_path=None,
            lint_violations=[v.model_dump() for v in lint_result.violations],
            # #785: the opt-in is a property of THIS letter, so it rides with
            # the row. Re-generate on the review page reads it back rather than
            # re-deriving a per-(job, target) verdict it has no target for.
            allow_stretch=allow_stretch,
        )

    md_lint = lint_markdown(payload_md, document_type="cover_letter")
    if not md_lint.ok:
        # Flagged, not discarded. No .docx is rendered for a flagged draft —
        # the download route re-renders lazily from payload_md.
        return CoverLetterPipelineLintFailure(
            lint=md_lint,
            letter=letter,
            warnings=trace_warnings,
            llm_result=llm_result,
            record=await _persist(md_lint),
            payload_md=payload_md,
        )
    docx_bytes = await asyncio.to_thread(md_to_docx, payload_md)
    lint = lint_docx(docx_bytes, document_type="cover_letter")
    if not lint.ok:
        return CoverLetterPipelineLintFailure(
            lint=lint,
            letter=letter,
            warnings=trace_warnings,
            llm_result=llm_result,
            record=await _persist(lint),
            payload_md=payload_md,
        )

    record = await _persist(lint)
    try:
        storage_path = await persistence.upload_docx(
            supabase,
            user_id=user_id,
            resume_id=record.id,
            docx_bytes=docx_bytes,
        )
    except Exception:
        storage_path = None
    if storage_path:
        await (
            supabase.table(persistence.TABLE)
            .update({"storage_path": storage_path})
            .eq("id", record.id)
            .execute()
        )
        record = record.model_copy(update={"storage_path": storage_path})

    return CoverLetterPipelineSuccess(
        record=record,
        letter=letter,
        warnings=trace_warnings,
        lint=lint,
        llm_result=llm_result,
    )
