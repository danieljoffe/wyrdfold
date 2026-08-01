"""Tailor router.

POST  /tailor/resume                    — synthesize + render + lint + persist a resume.
POST  /tailor/cover-letter              — same pipeline shape, for cover letters.
GET   /tailor/resumes                   — recent resume tailorings.
GET   /tailor/cover-letters             — recent cover-letter tailorings.
GET   /tailor/resumes/by-job/{id}       — most recent resume for a job posting.
POST  /tailor/resumes/export-zip        — bulk .docx download as zip.
PATCH /tailor/resumes/{id}              — edit a draft resume payload.
POST  /tailor/resumes/{id}/approve      — approve (lock) a resume.
POST  /tailor/resumes/{id}/unapprove    — reopen an approved resume for editing.
GET   /tailor/resumes/{id}              — one record (either type; look up by id).
GET   /tailor/resumes/{id}/download     — serves the `.docx` bytes.

All 422 responses carry the LintFailureResponse shape.
"""

import asyncio
import logging
import os
import re
import tempfile
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import ValidationError
from supabase import AsyncClient

from app.background import spawn_detached
from app.constants import resolve_owner
from app.dependencies import (
    enforce_llm_budget,
    get_async_service_supabase,
    get_async_user_supabase,
    get_current_user_id,
    get_llm_client,
    verify_api_key_or_jwt,
)
from app.models.batch import BatchJob, BatchRequest, BatchResponse
from app.models.experience import OptimizedDoc, Preferences
from app.models.tailor import (
    BulkExportRequest,
    CoverLetterRequest,
    GapGateFailureResponse,
    ResumeCheckpointRequest,
    ResumeEditRequest,
    TailoredResumeRecord,
    TailorLintFailureResponse,
    TailorRequest,
    TailorResponse,
)
from app.models.user_profile import ResumeStyleSettings
from app.rate_limit import limiter
from app.services.ats_lint import lint_markdown
from app.services.batch import create_batch, get_batch, process_batch
from app.services.docx.pandoc_render import (
    PandocNotInstalledError,
    PandocRenderError,
    md_payload_hash,
    md_to_docx,
)
from app.services.experience import gap_tracker, optimized, preferences
from app.services.llm.client import LLMClient
from app.services.tailor import (
    CoverLetterPipelineLintFailure,
    CoverLetterPipelineSuccess,
    PipelineLintFailure,
    PipelineSuccess,
    persistence,
    run_cover_letter_pipeline,
    run_tailor_pipeline,
    versions,
)
from app.services.tailor.contact import resolve_contact
from app.services.tailor.reuse import (
    clone_resume_for_job,
    extract_profile_keywords,
    find_reusable_resume,
)

_log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/tailor",
    tags=["tailor"],
    dependencies=[Depends(verify_api_key_or_jwt)],
)


# ---- Inline async reads/writes (#57 slice 3) ------------------------------
#
# Handlers run on the async user/service client. A few shared experience-service
# helpers (``optimized.get_latest`` / ``preferences.get``) and the persistence
# ``upsert_user_job`` mirror stay SYNC for their not-yet-converted callers
# (poller / analysis / targets / the jobs+status routers) — a sync helper can't
# take the async client, and converting them would break those callers. So these
# thin async inlines run the same queries on the async client (the
# insights.py::_user_target_ids / experience.py pattern). No sync+async twin in
# the shared layer.


async def _optimized_latest(supabase: AsyncClient, user_id: str | None) -> OptimizedDoc | None:
    """Async inline of ``optimized.get_latest`` (sync twin kept for the poller /
    analysis / targets / annotations chain). Reads fresh: the module TTL cache
    is populated only by those sync callers and every write invalidates it."""
    resp = await (
        supabase.table(optimized.TABLE)
        .select("*")
        .order("version", desc=True)
        .limit(1)
        .eq("user_id", resolve_owner(user_id))
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    return OptimizedDoc.model_validate(rows[0]) if rows else None


async def _preferences_get(supabase: AsyncClient, user_id: str | None) -> Preferences | None:
    """Async inline of ``preferences.get`` (sync twin kept for other callers)."""
    resp = await (
        supabase.table(preferences.TABLE)
        .select("*")
        .limit(1)
        .eq("user_id", resolve_owner(user_id))
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    return Preferences.model_validate(rows[0]) if rows else None


async def _upsert_user_job(
    supabase: AsyncClient, *, user_id: str, job_posting_id: str, status: str
) -> None:
    """Async inline of ``persistence.upsert_user_job`` (sync twin kept for the
    jobs / status routers). Mirrors a pipeline-status write into ``user_jobs``."""
    await supabase.table("user_jobs").upsert(
        {
            "user_id": user_id,
            "job_posting_id": job_posting_id,
            "status": status,
            "updated_at": datetime.now(UTC).isoformat(),
        },
        on_conflict="user_id,job_posting_id",
    ).execute()


async def _get_records(
    supabase: AsyncClient, resume_ids: list[str], *, user_id: str
) -> list[TailoredResumeRecord | None]:
    """Fetch each record concurrently — independent reads, overlapped via gather.
    A non-handler helper so the #107 guard sees the handler await the fan-out."""
    return await asyncio.gather(
        *(persistence.get(supabase, rid, user_id=user_id) for rid in resume_ids)
    )


async def _download_all(supabase: AsyncClient, storage_paths: list[str]) -> list[bytes]:
    """Download each .docx concurrently (independent network reads)."""
    return await asyncio.gather(
        *(persistence.download_docx(supabase, path) for path in storage_paths)
    )


async def _target_scoring_profile_row(
    supabase: AsyncClient, target_id: str
) -> dict[str, Any] | None:
    """The ``targets`` row (scoring_profile only) for *target_id*, or None.
    A non-handler helper so the #107 guard sees the handler await the read."""
    resp = await (
        supabase.table("targets").select("scoring_profile").eq("id", target_id).execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    return rows[0] if rows else None


async def _fetch_postings_by_ids(
    supabase: AsyncClient, ids: list[str]
) -> list[dict[str, Any]]:
    """Batch-fetch ``jobs`` rows for *ids* (id/title/description_html)."""
    resp = await (
        supabase.table("jobs").select("id, title, description_html").in_("id", ids).execute()
    )
    return cast(list[dict[str, Any]], resp.data or [])


async def _resolve_target_for_posting(
    supabase: AsyncClient, *, user_id: str | None, job_posting_id: str
) -> str | None:
    """Resolve which target a posting belongs to, via ``scores``.

    ``jobs.target_id`` is a vestigial column the poller never writes —
    the job ↔ target link lives in ``scores`` (same resolution as
    ``routers/jobs.py``). JWT callers are scoped to their own targets via
    ``user_targets`` and get their best-scoring one when several match;
    API-key (operator) callers resolve across all targets.
    """
    target_ids: list[str] | None = None
    if user_id is not None:
        ut_resp = await (
            supabase.table("user_targets").select("target_id").eq("user_id", user_id).execute()
        )
        target_ids = [
            cast(dict[str, Any], r)["target_id"]
            for r in (ut_resp.data or [])
            if isinstance(r, dict) and r.get("target_id")
        ]
        if not target_ids:
            return None

    score_query = supabase.table("scores").select("target_id").eq("job_posting_id", job_posting_id)
    if target_ids is not None:
        score_query = score_query.in_("target_id", target_ids)
    resp = await score_query.order("score", desc=True).limit(1).execute()
    rows = resp.data or []
    if not rows:
        return None
    return cast(str, cast(dict[str, Any], rows[0])["target_id"])


@router.post(
    "/resume",
    responses={422: {"model": TailorLintFailureResponse | GapGateFailureResponse}},
    dependencies=[Depends(enforce_llm_budget)],
)
@limiter.limit("30/minute")
async def create_tailored_resume(
    request: Request,
    body: TailorRequest,
    supabase: AsyncClient = Depends(get_async_service_supabase),
    llm: LLMClient = Depends(get_llm_client),
    # JWT-required: the generated .docx is stored under the caller's
    # <user_id>/ Storage folder, so anonymous generation is no longer allowed.
    user_id: str = Depends(get_current_user_id),
) -> TailorResponse:
    # `async def`: the LLM tailor pipeline + every DB round-trip now run
    # natively on the async service client (#57 slice 3), no threadpool worker
    # held for the supabase calls.
    current_optimized = await _optimized_latest(supabase, user_id=user_id)
    if current_optimized is None:
        raise HTTPException(
            status_code=404,
            detail="no optimized doc — derive one via POST /experience/derive first",
        )

    gate = gap_tracker.can_generate(current_optimized.payload)
    if not gate.ok:
        health = gap_tracker.gap_health(current_optimized.payload)
        raise HTTPException(
            status_code=422,
            detail={
                "ok": False,
                "code": "gap_gate",
                "reason": gate.reason,
                "message": gate.message,
                "gap_pct": health.gap_pct,
                "tier": health.tier,
            },
        )

    # Reuse check (#504): skip pipeline if a similar resume exists in the target
    if not body.force_fresh and body.job_posting_id:
        target_id = await _resolve_target_for_posting(
            supabase,
            user_id=user_id,
            job_posting_id=body.job_posting_id,
        )
        if target_id:
            target_row = await _target_scoring_profile_row(supabase, target_id)
            if target_row:
                from app.models.targets import ScoringProfile

                profile = ScoringProfile.model_validate(target_row["scoring_profile"])
                keywords = extract_profile_keywords(profile)
                if keywords:
                    reusable = await find_reusable_resume(
                        supabase,
                        target_id=target_id,
                        job_description=body.job_description,
                        profile_keywords=keywords,
                        user_id=user_id,
                    )
                    if reusable is not None:
                        cloned = await clone_resume_for_job(
                            supabase,
                            source=reusable,
                            job_posting_id=body.job_posting_id,
                            job_description=body.job_description,
                            user_id=user_id,
                        )
                        await persistence.mark_job_resume_draft(
                            supabase,
                            body.job_posting_id,
                            user_id=user_id,
                        )
                        return TailorResponse(
                            record=cloned,
                            lint_warnings=[],
                        )

    prefs_row = await _preferences_get(supabase, user_id=user_id)
    prefs_payload = prefs_row.payload if prefs_row else None
    contact = await resolve_contact(supabase, user_id, body.contact)

    result = await run_tailor_pipeline(
        supabase,
        llm,
        user_id=user_id,
        optimized=current_optimized,
        job_description=body.job_description,
        contact=contact,
        preferences=prefs_payload,
        critique=body.critique,
        resume_type=body.resume_type or "generic",
        page_budget=body.page_budget,
        job_posting_id=body.job_posting_id,
        target_label=body.target_label,
    )

    if isinstance(result, PipelineLintFailure):
        raise HTTPException(
            status_code=422,
            detail={
                "ok": False,
                "violations": [v.model_dump() for v in result.lint.violations],
            },
        )

    if not isinstance(result, PipelineSuccess):
        raise HTTPException(status_code=500, detail="Unexpected pipeline result")
    if body.job_posting_id:
        await persistence.mark_job_resume_draft(
            supabase,
            body.job_posting_id,
            user_id=user_id,
        )
    return TailorResponse(
        record=result.record,
        lint_warnings=result.lint.warnings,
    )


@router.post(
    "/cover-letter",
    responses={422: {"model": TailorLintFailureResponse | GapGateFailureResponse}},
    dependencies=[Depends(enforce_llm_budget)],
)
@limiter.limit("30/minute")
async def create_tailored_cover_letter(
    request: Request,
    body: CoverLetterRequest,
    supabase: AsyncClient = Depends(get_async_service_supabase),
    llm: LLMClient = Depends(get_llm_client),
    # JWT-required: see create_tailored_resume (per-user Storage folder).
    user_id: str = Depends(get_current_user_id),
) -> TailorResponse:
    # `async def`: the LLM cover-letter pipeline + every DB round-trip run
    # natively on the async service client (#57 slice 3).
    current_optimized = await _optimized_latest(supabase, user_id=user_id)
    if current_optimized is None:
        raise HTTPException(
            status_code=404,
            detail="no optimized doc — derive one via POST /experience/derive first",
        )

    gate = gap_tracker.can_generate(current_optimized.payload)
    if not gate.ok:
        health = gap_tracker.gap_health(current_optimized.payload)
        raise HTTPException(
            status_code=422,
            detail={
                "ok": False,
                "code": "gap_gate",
                "reason": gate.reason,
                "message": gate.message,
                "gap_pct": health.gap_pct,
                "tier": health.tier,
            },
        )

    prefs_row = await _preferences_get(supabase, user_id=user_id)
    prefs_payload = prefs_row.payload if prefs_row else None
    contact = await resolve_contact(supabase, user_id, body.contact)

    result = await run_cover_letter_pipeline(
        supabase,
        llm,
        user_id=user_id,
        optimized=current_optimized,
        job_description=body.job_description,
        company_name=body.company_name,
        contact=contact,
        role_title=body.role_title,
        preferences=prefs_payload,
        critique=body.critique,
        job_posting_id=body.job_posting_id,
        target_label=body.target_label,
    )

    if isinstance(result, CoverLetterPipelineLintFailure):
        raise HTTPException(
            status_code=422,
            detail={
                "ok": False,
                "violations": [v.model_dump() for v in result.lint.violations],
            },
        )

    if not isinstance(result, CoverLetterPipelineSuccess):
        raise HTTPException(status_code=500, detail="Unexpected pipeline result")
    return TailorResponse(
        record=result.record,
        lint_warnings=result.lint.warnings,
    )


# `async def` on the async user client (#57 slice 3): the DB read runs natively
# on the event loop, no threadpool worker held for the supabase round-trip.
@router.get("/resumes")
async def list_documents(
    limit: int = 50,
    supabase: AsyncClient = Depends(get_async_user_supabase),
    user_id: str = Depends(get_current_user_id),
) -> dict[str, list[TailoredResumeRecord]]:
    rows = await persistence.list_recent(
        supabase,
        user_id=user_id,
        limit=max(1, min(limit, 200)),
        document_type="resume",
    )
    return {"resumes": rows}


@router.get("/cover-letters")
async def list_tailored_cover_letters(
    limit: int = 50,
    supabase: AsyncClient = Depends(get_async_user_supabase),
    user_id: str = Depends(get_current_user_id),
) -> dict[str, list[TailoredResumeRecord]]:
    rows = await persistence.list_recent(
        supabase,
        user_id=user_id,
        limit=max(1, min(limit, 200)),
        document_type="cover_letter",
    )
    return {"cover_letters": rows}


# ---- Resume lifecycle (#505) -------------------------------------------------


@router.get("/resumes/by-job/{job_posting_id}")
async def get_resume_by_job(
    job_posting_id: str,
    supabase: AsyncClient = Depends(get_async_user_supabase),
    user_id: str = Depends(get_current_user_id),
) -> TailoredResumeRecord | None:
    """Most recent resume for a given job posting, or ``null`` if none exists.

    Returns ``null`` with a 200 status (rather than 404) for the
    "no record yet" case so the browser doesn't log a "Failed to
    load resource: 404" console error on every job-detail visit
    before generation. The FE consumer (``ResumeSection``) treats
    ``null`` and 404 the same way — render a "Generate Resume"
    CTA — so dropping the 404 is a no-op for the user.
    """
    return await persistence.get_by_job(supabase, job_posting_id, user_id=user_id)


@router.get("/cover-letters/by-job/{job_posting_id}")
async def get_cover_letter_by_job(
    job_posting_id: str,
    supabase: AsyncClient = Depends(get_async_user_supabase),
    user_id: str = Depends(get_current_user_id),
) -> TailoredResumeRecord | None:
    """Most recent cover letter for a given job posting, or ``null``
    if none exists. See ``get_resume_by_job`` for the 200-with-null
    rationale.
    """
    return await persistence.get_by_job(
        supabase, job_posting_id, user_id=user_id, document_type="cover_letter"
    )


# Bulk resume export streams from a SpooledTemporaryFile (spills to disk past
# _EXPORT_SPOOL_MAX_MEMORY) so a large export never holds the whole zip in RAM
# (#192 P-H2), mirroring the account-data export.
_EXPORT_SPOOL_MAX_MEMORY = 32 * 1024 * 1024
_EXPORT_STREAM_CHUNK_BYTES = 64 * 1024


@router.post("/resumes/export-zip")
async def export_resumes_zip(
    body: BulkExportRequest,
    supabase: AsyncClient = Depends(get_async_service_supabase),
    user_supabase: AsyncClient = Depends(get_async_user_supabase),
    user_id: str = Depends(get_current_user_id),
) -> Response:
    """Download approved resumes as a single .zip archive.

    JWT-required: file bytes come from per-user Storage (RLS) via
    ``user_supabase``; DB lookups stay on the service-role ``supabase``.

    The DB reads + per-file Storage downloads run natively on the async clients
    (#57 slice 3); the downloads — the dominant, network-bound cost — still run
    concurrently via ``asyncio.gather`` (in the non-handler ``_get_records`` /
    ``_download_all`` helpers). The zip itself is built into a
    ``SpooledTemporaryFile`` (spills to disk past ``_EXPORT_SPOOL_MAX_MEMORY``)
    and streamed in chunks (#192 P-H2), so a large bulk export never holds the
    whole archive in RAM.
    """
    rows = await _get_records(supabase, list(body.resume_ids), user_id=user_id)
    records: list[TailoredResumeRecord] = []
    unapproved: list[str] = []
    for rid, row in zip(body.resume_ids, rows, strict=True):
        if row is None:
            raise HTTPException(status_code=404, detail=f"resume not found: {rid}")
        if row.approved_at is None:
            unapproved.append(rid)
        records.append(row)

    if unapproved:
        raise HTTPException(
            status_code=400,
            detail=f"resumes not yet approved: {', '.join(unapproved)}",
        )

    # Download every .docx concurrently — these are independent network reads,
    # so gather() overlaps them instead of paying the round-trips serially.
    to_download = [rec for rec in records if rec.storage_path]
    downloaded = await _download_all(
        user_supabase, [cast(str, rec.storage_path) for rec in to_download]
    )

    # SIM115 suppressed: the spool must outlive this handler — it's closed by
    # the streaming generator's finally once the response body has been sent.
    spool = tempfile.SpooledTemporaryFile(max_size=_EXPORT_SPOOL_MAX_MEMORY)  # noqa: SIM115
    try:
        with zipfile.ZipFile(spool, "w", zipfile.ZIP_DEFLATED) as zf:
            for rec, docx_bytes in zip(to_download, downloaded, strict=True):
                resume = rec.as_resume()
                # Build a descriptive filename from the first experience entry
                company = "unknown"
                title = "resume"
                if resume.experience:
                    company = resume.experience[0].company
                    title = resume.experience[0].title
                safe = re.sub(r"[^\w\s-]", "", f"{company}_{title}")
                safe = re.sub(r"\s+", "_", safe).strip("_")[:80]
                zf.writestr(f"{safe}.docx", docx_bytes)
        size = spool.seek(0, os.SEEK_END)
        spool.seek(0)
    except BaseException:
        spool.close()
        raise

    def _iter_spool() -> Iterator[bytes]:
        # Sync generator: Starlette drives it in the threadpool, keeping the
        # blocking disk reads off the event loop.
        try:
            while chunk := spool.read(_EXPORT_STREAM_CHUNK_BYTES):
                yield chunk
        finally:
            spool.close()

    return StreamingResponse(
        _iter_spool(),
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="resumes.zip"',
            "Content-Length": str(size),
        },
    )


@router.patch("/resumes/{resume_id}")
async def edit_tailored_resume(
    resume_id: str,
    body: ResumeEditRequest,
    supabase: AsyncClient = Depends(get_async_user_supabase),
    user_id: str = Depends(get_current_user_id),
) -> TailorResponse:
    """Edit a draft resume's markdown. Rejected if already approved.

    The .docx isn't re-rendered eagerly — saving is cheap and the
    download endpoint detects a stale hash to re-render lazily.
    """
    row = await persistence.get(supabase, resume_id, user_id=user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="tailored document not found")
    if row.approved_at is not None:
        raise HTTPException(status_code=409, detail="document already approved — cannot edit")

    lint_result = lint_markdown(body.markdown, document_type=row.document_type)
    if lint_result.errors:
        raise HTTPException(
            status_code=422,
            detail={
                "ok": False,
                "violations": [v.model_dump() for v in lint_result.violations],
            },
        )

    record = await persistence.update_payload_md(
        supabase, resume_id, body.markdown, user_id=user_id
    )
    return TailorResponse(record=record, lint_warnings=lint_result.warnings)


@router.post("/resumes/{resume_id}/checkpoint")
async def checkpoint_tailored_resume(
    resume_id: str,
    body: ResumeCheckpointRequest | None = None,
    supabase: AsyncClient = Depends(get_async_user_supabase),
    user_id: str = Depends(get_current_user_id),
) -> dict[str, Any]:
    """Snapshot a draft resume's current markdown into version history.

    Two callers:
    - `navigator.sendBeacon` on pagehide, with `markdown` in the body, so
      a debounced autosave that hasn't yet flushed still lands in
      history before the page goes away.
    - Pre-approve / pre-readapt explicit checkpoints, with no body.

    Idempotent via dedup: if the latest snapshot already matches, no
    new row is written.
    """
    row = await persistence.get(supabase, resume_id, user_id=user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="tailored document not found")
    if row.approved_at is not None:
        # Approved documents are locked — nothing new to snapshot.
        return {"recorded": False, "reason": "approved"}

    if body and body.markdown:
        lint_result = lint_markdown(body.markdown, document_type=row.document_type)
        if lint_result.errors:
            raise HTTPException(
                status_code=422,
                detail={
                    "ok": False,
                    "violations": [v.model_dump() for v in lint_result.violations],
                },
            )
        await persistence.update_payload_md(supabase, resume_id, body.markdown, user_id=user_id)

    recorded = await versions.checkpoint(supabase, resume_id)
    return {"recorded": recorded}


@router.post("/resumes/{resume_id}/approve")
async def approve_tailored_resume(
    resume_id: str,
    supabase: AsyncClient = Depends(get_async_user_supabase),
    user_id: str = Depends(get_current_user_id),
) -> TailoredResumeRecord:
    """Approve (lock) a tailored resume or cover letter. Idempotent if already approved."""
    row = await persistence.get(supabase, resume_id, user_id=user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="tailored document not found")

    # Idempotent: if already approved, just return it
    if row.approved_at is not None:
        return row

    record = await persistence.approve(supabase, resume_id, user_id=user_id)

    # Resume approval also advances the linked job posting to resume_ready;
    # cover letters don't drive job status.
    # Per-user pipeline state lives in user_jobs (#75 C3): no longer touch
    # the global jobs.status. Api-key callers (user_id None) have no per-user
    # pipeline, so they skip the mirror; cover letters don't drive job status.
    if row.document_type == "resume" and row.job_posting_id and user_id:
        await _upsert_user_job(
            supabase,
            user_id=user_id,
            job_posting_id=row.job_posting_id,
            status="resume_ready",
        )

    return record


@router.post("/resumes/{resume_id}/unapprove")
async def unapprove_tailored_resume(
    resume_id: str,
    supabase: AsyncClient = Depends(get_async_user_supabase),
    user_id: str = Depends(get_current_user_id),
) -> TailoredResumeRecord:
    """Reopen an approved resume or cover letter for editing. Idempotent if already unlocked."""
    row = await persistence.get(supabase, resume_id, user_id=user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="tailored document not found")

    if row.approved_at is None:
        return row

    record = await persistence.unapprove(supabase, resume_id, user_id=user_id)

    # Mirror the approve side: resume unlock walks the linked job back to
    # resume_draft so the lifecycle stays in sync.
    # Per-user pipeline state lives in user_jobs (#75 C3): see
    # approve_tailored_resume.
    if row.document_type == "resume" and row.job_posting_id and user_id:
        await _upsert_user_job(
            supabase,
            user_id=user_id,
            job_posting_id=row.job_posting_id,
            status="resume_draft",
        )

    return record


# ---- Single resume lookup + download ----------------------------------------


@router.get("/resumes/{resume_id}")
async def get_tailored_resume(
    resume_id: str,
    supabase: AsyncClient = Depends(get_async_user_supabase),
    user_id: str = Depends(get_current_user_id),
) -> TailoredResumeRecord:
    row = await persistence.get(supabase, resume_id, user_id=user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="tailored resume not found")
    return row


@router.get("/resumes/{resume_id}/versions")
async def list_resume_versions(
    resume_id: str,
    supabase: AsyncClient = Depends(get_async_user_supabase),
    user_id: str = Depends(get_current_user_id),
) -> dict[str, Any]:
    """Return up to FREE_TIER_VERSION_CAP recent payload snapshots (F3-H)."""
    row = await persistence.get(supabase, resume_id, user_id=user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="tailored resume not found")
    history = await versions.list_for_resume(supabase, resume_id)
    return {
        "versions": [v.model_dump(mode="json") for v in history],
        "cap": versions.FREE_TIER_VERSION_CAP,
    }


async def _fetch_user_resume_style(
    supabase: AsyncClient, user_id: str
) -> ResumeStyleSettings | None:
    """Read the user's saved default resume style, or None if unset/malformed."""
    resp = await (
        supabase.table("user_profiles")
        .select("resume_style_settings")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    stored = rows[0].get("resume_style_settings") if rows else None
    if not stored:
        return None
    try:
        return ResumeStyleSettings.model_validate(stored)
    except ValidationError:
        return None


async def _resolve_render_style(
    supabase: AsyncClient, row: TailoredResumeRecord, user_id: str | None
) -> ResumeStyleSettings | None:
    """Effective docx style for a download: per-record override, else the
    user's profile default, else None (today's unstyled pandoc render).
    """
    if row.style_settings:
        try:
            return ResumeStyleSettings.model_validate(row.style_settings)
        except ValidationError:
            pass
    if user_id is not None:
        return await _fetch_user_resume_style(supabase, user_id)
    return None


@router.get("/resumes/{resume_id}/download")
async def download_tailored_resume(
    resume_id: str,
    supabase: AsyncClient = Depends(get_async_service_supabase),
    user_supabase: AsyncClient = Depends(get_async_user_supabase),
    user_id: str = Depends(get_current_user_id),
) -> Response:
    # JWT-required: docx bytes are read/written through per-user Storage
    # (RLS) via user_supabase; DB lookups stay on the service-role client.
    #
    # `async def`: DB round-trips run natively on the async clients (#57 slice 3);
    # only the pandoc render (CPU-bound subprocess) stays in asyncio.to_thread.
    row = await persistence.get(supabase, resume_id, user_id=user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="tailored resume not found")

    style = await _resolve_render_style(supabase, row, user_id)
    expected_hash = md_payload_hash(row.payload_md, style) if row.payload_md else None
    cache_fresh = (
        row.storage_path is not None
        and expected_hash is not None
        and row.docx_payload_md_hash == expected_hash
    )

    if not cache_fresh:
        if not row.payload_md:
            if not row.storage_path:
                raise HTTPException(status_code=404, detail="no .docx persisted for this resume")
            # Legacy row with cached docx but no markdown — serve cached bytes.
            try:
                data = await persistence.download_docx(user_supabase, row.storage_path)
            except Exception as exc:
                # Generic client message — the raw exception (Storage path,
                # internal errors) stays server-side only (audit #29 R3 / M4).
                _log.exception("docx storage fetch failed for resume_id=%s", resume_id)
                raise HTTPException(
                    status_code=502, detail="failed to fetch resume document"
                ) from exc
            filename = f"{row.id}.docx"
            return Response(
                content=data,
                media_type=persistence.DOCX_CONTENT_TYPE,
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )

        try:
            data = await asyncio.to_thread(md_to_docx, row.payload_md, style)
        except PandocNotInstalledError as exc:
            # Server misconfiguration — don't echo the raw error (which can
            # name internal paths) to the client (audit #29 R3 / M4).
            _log.exception("pandoc not installed while rendering resume_id=%s", resume_id)
            raise HTTPException(status_code=500, detail="failed to render resume document") from exc
        except PandocRenderError as exc:
            _log.exception("docx render failed for resume_id=%s", resume_id)
            raise HTTPException(status_code=500, detail="failed to render resume document") from exc

        try:
            storage_path = await persistence.upload_docx(
                supabase,
                user_id=user_id,
                resume_id=resume_id,
                docx_bytes=data,
            )
            await persistence.mark_docx_rendered(
                supabase,
                resume_id,
                storage_path=storage_path,
                payload_md_hash=expected_hash or md_payload_hash(row.payload_md, style),
                user_id=user_id,
            )
        except Exception:
            # Fall through and serve the freshly-rendered bytes regardless;
            # next download will retry the cache write. Log so a persistent
            # storage outage isn't silently masked by the in-memory render.
            _log.warning(
                "docx cache write failed for resume_id=%s; serving fresh bytes",
                resume_id,
                exc_info=True,
            )
    else:
        try:
            data = await persistence.download_docx(
                user_supabase,
                row.storage_path,  # type: ignore[arg-type]
            )
        except Exception as exc:
            # Generic client message — raw exception stays server-side only
            # (audit #29 R3 / M4).
            _log.exception("docx storage fetch failed for resume_id=%s", resume_id)
            raise HTTPException(status_code=502, detail="failed to fetch resume document") from exc

    filename = f"{row.id}.docx"
    return Response(
        content=data,
        media_type=persistence.DOCX_CONTENT_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---- Batch resume generation (#503) ----------------------------------------


@router.post("/batch", dependencies=[Depends(enforce_llm_budget)])
@limiter.limit("5/minute")
async def create_batch_resumes(
    request: Request,
    body: BatchRequest,
    supabase: AsyncClient = Depends(get_async_service_supabase),
    llm: LLMClient = Depends(get_llm_client),
    # JWT-required: batch generation stores each .docx under the caller's
    # <user_id>/ Storage folder (the background task uploads via service-role
    # to that verified folder).
    user_id: str = Depends(get_current_user_id),
) -> BatchResponse:
    """Kick off batch resume generation for multiple job postings.

    Returns immediately with a batch_id. Poll GET /tailor/batch/{id}
    for progress.

    `async def`: the setup DB round-trips run natively on the async service
    client (#57 slice 3); ``process_batch`` (an async task on the SAME pooled
    client) is spawned as a DETACHED task, NOT via starlette ``BackgroundTasks``
    — ``add_task`` work on the pooled async client deadlocks the httpx pool under
    uvloop (see app/background.py), which is why /analysis uses ``spawn_detached``
    too.
    """
    current_optimized = await _optimized_latest(supabase, user_id=user_id)
    if current_optimized is None:
        raise HTTPException(
            status_code=404,
            detail="no optimized doc — derive one via POST /experience/derive first",
        )

    # Verify all job posting IDs exist and fetch their descriptions.
    # Single .in_() round-trip; .in_() does not guarantee row order, so re-map
    # by id to preserve the input ordering (downstream processes jobs in
    # request order).
    posting_rows = await _fetch_postings_by_ids(supabase, list(body.job_posting_ids))
    fetched = {row["id"]: row for row in posting_rows}

    warnings: list[str] = []
    postings: list[dict[str, Any]] = []
    for jid in body.job_posting_ids:
        row = fetched.get(jid)
        if row is None:
            raise HTTPException(status_code=404, detail=f"job posting not found: {jid}")
        if not row.get("description_html"):
            warnings.append(f"no_description:{jid}")
        postings.append(row)

    # Derive the common target from the first posting (all batch jobs share
    # a target). Resolved via ``scores`` — ``jobs.target_id`` is vestigial
    # and always NULL, which silently disabled batch reuse.
    target_id: str | None = (
        await _resolve_target_for_posting(
            supabase,
            user_id=user_id,
            job_posting_id=body.job_posting_ids[0],
        )
        if body.job_posting_ids
        else None
    )

    prefs_row = await _preferences_get(supabase, user_id=user_id)
    prefs_payload = prefs_row.payload if prefs_row else None
    contact = await resolve_contact(supabase, user_id, body.contact)

    batch = await create_batch(
        supabase,
        user_id=user_id,
        job_posting_ids=body.job_posting_ids,
    )

    spawn_detached(
        process_batch(
            supabase,
            llm,
            batch_id=batch.id,
            user_id=user_id,
            optimized=current_optimized,
            jobs=postings,
            contact=contact,
            preferences=prefs_payload,
            resume_type=body.resume_type or "generic",
            page_budget=body.page_budget,
            force_fresh=body.force_fresh,
            target_id=target_id,
        ),
        name=f"batch:{batch.id}",
    )

    return BatchResponse(
        batch_id=batch.id,
        total=batch.total,
        status=batch.status,
        warnings=warnings,
    )


@router.get("/batch/{batch_id}")
async def get_batch_status(
    batch_id: str,
    supabase: AsyncClient = Depends(get_async_user_supabase),
    user_id: str = Depends(get_current_user_id),
) -> BatchJob:
    """Poll batch processing progress."""
    batch = await get_batch(supabase, batch_id, user_id=user_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="batch not found")
    return batch
