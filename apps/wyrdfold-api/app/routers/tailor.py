"""Tailor router.

POST  /tailor/resume                    — synthesize + render + lint + persist a resume.
POST  /tailor/cover-letter              — same pipeline shape, for cover letters.
GET   /tailor/resumes                   — recent resume tailorings.
GET   /tailor/cover-letters             — recent cover-letter tailorings.
GET   /tailor/resumes/by-job/{id}       — poll state + most recent resume for a posting.
POST  /tailor/resumes/export-zip        — bulk .docx download as zip.
PATCH /tailor/resumes/{id}              — edit a draft resume payload.
POST  /tailor/resumes/{id}/ats-recheck  — re-run ATS lint over the saved markdown.
POST  /tailor/resumes/{id}/approve      — approve (lock) a resume.
POST  /tailor/resumes/{id}/unapprove    — reopen an approved resume for editing.
GET   /tailor/resumes/{id}              — one record (either type; look up by id).
GET   /tailor/resumes/{id}/download     — serves the `.docx` bytes.

Generation is **non-blocking** (#656), the shape ``/analysis`` took in #459.
``POST /tailor/resume`` (~39s) and ``POST /tailor/cover-letter`` (~27s) used
to hold the request open for the whole LLM pipeline. They now hand it to a
detached task and return ``202 {"status": "running"}``; the client polls
``GET /tailor/resumes/by-job/{id}`` (or the cover-letter sibling) until the
``documents`` row lands. Because the task persists regardless of the client,
the user is free to navigate away mid-generation and come back to a finished
draft.

Two things stay synchronous on purpose, so they surface as a real HTTP status
on the POST instead of a silent task failure the user only learns about by
polling: the structural gap gate (422) and contact resolution (400) — the
frontend has dedicated recovery UI for both. A 202 only means the LLM work
was accepted.

The blocking path survives for callers with no ``job_posting_id`` (operator /
api-key drives against a bare JD): that id IS the poll surface, so there is
nothing to poll without it.

422 responses carry the LintFailureResponse shape — but as of #656 a
generation lint failure is no longer one of them: the document (resume OR
cover letter) is persisted as a flagged draft carrying its violations
(regenerating burns the daily cap, and with the run backgrounded nobody may
be watching to retry).
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
from fastapi import status as http_status
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import ValidationError
from supabase import AsyncClient

from app.background import spawn_detached
from app.config import Settings
from app.constants import resolve_owner
from app.dependencies import (
    enforce_llm_budget,
    get_async_service_supabase,
    get_async_user_supabase,
    get_current_user_id,
    get_llm_client,
    get_settings,
    verify_api_key_or_jwt,
)
from app.models.batch import BatchJob, BatchRequest, BatchResponse
from app.models.experience import OptimizedDoc, Preferences, PreferencesPayload
from app.models.tailor import (
    AtsRecheckResponse,
    BulkExportRequest,
    ContactInfo,
    CoverLetterRequest,
    DocumentType,
    GapGateFailureResponse,
    ResumeCheckpointRequest,
    ResumeEditRequest,
    TailoredDocumentState,
    TailoredResumeRecord,
    TailorLintFailureResponse,
    TailorRequest,
    TailorResponse,
    TailorStatusResponse,
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
    PipelineResult,
    PipelineSuccess,
    persistence,
    run_cover_letter_pipeline,
    run_registry,
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
    await (
        supabase.table("user_jobs")
        .upsert(
            {
                "user_id": user_id,
                "job_posting_id": job_posting_id,
                "status": status,
                "updated_at": datetime.now(UTC).isoformat(),
            },
            on_conflict="user_id,job_posting_id",
        )
        .execute()
    )


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
    resp = await supabase.table("targets").select("scoring_profile").eq("id", target_id).execute()
    rows = cast(list[dict[str, Any]], resp.data or [])
    return rows[0] if rows else None


async def _posting_exists(supabase: AsyncClient, job_posting_id: str) -> bool:
    """Does this ``jobs`` row exist?

    Guards the 202 (#656 follow-up). ``TailorRequest.job_posting_id`` is a
    plain ``str``, so a bogus id sails through validation, gets accepted, and
    the detached run then spends a FULL LLM call before dying on the foreign
    key at insert — burning the caller's daily cap on work that could never
    have succeeded. Verified live during the release gate: a
    ``job_posting_id="not-a-uuid"`` kick returned 202 and still wrote a
    ``llm_costs`` row.

    ``/analysis`` already validates its posting synchronously before spawning
    (``_fetch_job_description`` → 404); this is the same contract — a 202 must
    only ever mean "accepted work that can actually run".
    """
    resp = await supabase.table("jobs").select("id").eq("id", job_posting_id).limit(1).execute()
    return bool(cast(list[dict[str, Any]], resp.data or []))


async def _fetch_postings_by_ids(supabase: AsyncClient, ids: list[str]) -> list[dict[str, Any]]:
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


def _gap_gate_or_422(current_optimized: OptimizedDoc) -> None:
    """Structural gap gate (#498). Stays SYNCHRONOUS in front of the 202
    (#656 decision 1): a master doc too thin to generate from is a setup
    problem the user must fix, and the frontend renders a dedicated
    "update your master doc" CTA off this exact ``gap_gate`` code. Deferring
    it into the background task would turn an instant, actionable 422 into a
    30-second wait that ends in a generic poll error."""
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


def _running_202() -> JSONResponse:
    """The "keep polling" marker both POSTs return once a run is in flight."""
    return JSONResponse(
        status_code=http_status.HTTP_202_ACCEPTED,
        content=TailorStatusResponse(status="running").model_dump(),
    )


def _already_running(
    *, user_id: str, document_type: DocumentType, job_posting_id: str | None
) -> bool:
    """Cheap pre-check for an in-flight run on this exact document.

    Deliberately called BEFORE the expensive preamble (the #504 reuse probe is
    several DB round-trips), and it claims nothing — so a path that legitimately
    returns without spawning stays free of claim-lifecycle bookkeeping. Without
    it, a second tab kicking the same posting mid-run would sail past dedup into
    the reuse probe and could clone a sibling resume alongside the run that's
    already generating one.
    """
    if job_posting_id is None:
        return False
    return run_registry.is_running(
        run_registry.key_for(
            user_id=user_id,
            document_type=document_type,
            job_posting_id=job_posting_id,
        )
    )


def _claim_run_or_202(
    *,
    user_id: str,
    document_type: DocumentType,
    job_posting_id: str,
    max_concurrent: int,
) -> tuple[run_registry.Key, JSONResponse | None]:
    """Dedup + claim the in-flight slot for a background generation (#656).

    Returns ``(key, response)``: a non-None response is what the handler must
    return instead of spawning — a 202 ``running`` marker when an identical run
    is already in flight.

    The ``is_running`` check and the ``begin`` claim happen with no ``await``
    between them, so on the single event loop two concurrent kicks can't both
    pass and both spawn (the panel's auto-fire, a StrictMode double-invoke, or
    an impatient double-click would otherwise pay twice for one document).

    Raises 429 past ``max_concurrent`` in-flight runs for this user.
    Backgrounding removed the natural serialization a 39s blocking request
    imposed on a browser tab, and ``enforce_llm_budget`` meters *spend* whose
    ``llm_costs`` rows don't exist until each run's LLM returns — so without
    this, N simultaneous kicks across N different postings all read the same
    pre-burst spend and all pass.
    """
    key = run_registry.key_for(
        user_id=user_id,
        document_type=document_type,
        job_posting_id=job_posting_id,
    )
    if run_registry.is_running(key):
        return key, _running_202()
    if max_concurrent > 0 and run_registry.running_count_for_user(user_id) >= max_concurrent:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "tailor_concurrent_limit",
                "limit": max_concurrent,
                "message": (
                    "Too many documents generating at once. Wait for one to finish, then try again."
                ),
            },
        )
    run_registry.begin(key, user_id=user_id)
    return key, None


@router.post(
    "/resume",
    response_model=None,
    responses={
        # The 202 is the normal outcome for a client with a job_posting_id;
        # declare it so the contract is discoverable rather than inferred from
        # a handler that returns a raw JSONResponse (response_model=None).
        202: {"model": TailorStatusResponse},
        422: {"model": TailorLintFailureResponse | GapGateFailureResponse},
    },
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
    s: Settings = Depends(get_settings),
) -> TailorResponse | JSONResponse:
    """Kick off a tailored resume. Non-blocking when a posting is known (#656).

    Returns 202 ``{"status": "running"}`` and runs the ~39s pipeline in a
    detached task; the client polls ``GET /tailor/resumes/by-job/{id}``. A
    reuse-clone hit (#504) still returns 200 with the record inline — it costs
    no LLM call, so there's nothing to wait for. Callers without a
    ``job_posting_id`` keep the blocking path (see the module docstring).

    `async def`: the LLM tailor pipeline + every DB round-trip run natively on
    the async service client (#57 slice 3), no threadpool worker held for the
    supabase calls.
    """
    current_optimized = await _optimized_latest(supabase, user_id=user_id)
    if current_optimized is None:
        raise HTTPException(
            status_code=404,
            detail="no optimized doc — derive one via POST /experience/derive first",
        )

    _gap_gate_or_422(current_optimized)

    if _already_running(
        user_id=user_id, document_type="resume", job_posting_id=body.job_posting_id
    ):
        return _running_202()

    # A 202 must only ever mean "accepted work that can actually run" — see
    # _posting_exists. Checked before the reuse probe and the claim, so a bogus
    # id costs one indexed lookup and nothing else.
    if body.job_posting_id is not None and not await _posting_exists(supabase, body.job_posting_id):
        raise HTTPException(status_code=404, detail="job posting not found")

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

    if body.job_posting_id is None:
        # No posting → no poll surface (``by-job/{id}`` is keyed on it), so the
        # operator/api-key JD-only path keeps blocking for the full pipeline.
        prefs_row = await _preferences_get(supabase, user_id=user_id)
        contact = await resolve_contact(supabase, user_id, body.contact)
        result = await _run_resume_pipeline(
            supabase,
            llm,
            user_id=user_id,
            optimized=current_optimized,
            body=body,
            contact=contact,
            preferences=prefs_row.payload if prefs_row else None,
        )
        return _resume_response(result)

    key, dedup = _claim_run_or_202(
        user_id=user_id,
        document_type="resume",
        job_posting_id=body.job_posting_id,
        max_concurrent=s.tailor_max_concurrent_runs,
    )
    if dedup is not None:
        return dedup

    try:
        # Resolved BEFORE the 202. ``resolve_contact`` 400s when the profile
        # carries no name, and the frontend answers that with an inline
        # name prompt + retry (``promptForMissingContactName``) — a recovery
        # flow that only works if the failure is the POST's own status.
        prefs_row = await _preferences_get(supabase, user_id=user_id)
        contact = await resolve_contact(supabase, user_id, body.contact)
    except Exception:
        # Release the claim so a corrected retry re-kicks immediately, then
        # surface the real error.
        run_registry.finish(key)
        raise

    spawn_detached(
        _run_resume_task(
            key=key,
            supabase=supabase,
            llm=llm,
            user_id=user_id,
            optimized=current_optimized,
            body=body,
            contact=contact,
            preferences=prefs_row.payload if prefs_row else None,
        ),
        name=f"tailor:resume:{body.job_posting_id}",
    )
    return _running_202()


async def _run_resume_pipeline(
    supabase: AsyncClient,
    llm: LLMClient,
    *,
    user_id: str,
    optimized: OptimizedDoc,
    body: TailorRequest,
    contact: ContactInfo,
    preferences: PreferencesPayload | None,
) -> PipelineResult:
    """Run the resume pipeline and mirror the job's pipeline status.

    Shared by the blocking path and the detached task so the two can't drift —
    notably ``mark_job_resume_draft``, which used to sit only in the handler
    and would silently stop firing for every backgrounded run.
    """
    result = await run_tailor_pipeline(
        supabase,
        llm,
        user_id=user_id,
        optimized=optimized,
        job_description=body.job_description,
        contact=contact,
        preferences=preferences,
        critique=body.critique,
        resume_type=body.resume_type or "generic",
        page_budget=body.page_budget,
        job_posting_id=body.job_posting_id,
        target_label=body.target_label,
    )
    # A flagged draft is still a draft the user is about to work on, so the
    # posting advances either way — the row exists in both branches now.
    if body.job_posting_id:
        await persistence.mark_job_resume_draft(
            supabase,
            body.job_posting_id,
            user_id=user_id,
        )
    return result


def _resume_response(result: PipelineResult) -> TailorResponse:
    """The blocking path's 200 body.

    A lint failure is no longer a 422 (#656): the draft is persisted flagged,
    so it comes back as a normal record whose ``lint_violations`` say why it
    needs attention. Keeping one success shape means the operator path and the
    poll surface describe a flagged draft identically.
    """
    if isinstance(result, PipelineLintFailure):
        return TailorResponse(record=result.record, lint_warnings=result.lint.warnings)
    if not isinstance(result, PipelineSuccess):
        raise HTTPException(status_code=500, detail="Unexpected pipeline result")
    return TailorResponse(record=result.record, lint_warnings=result.lint.warnings)


async def _run_resume_task(
    *,
    key: run_registry.Key,
    supabase: AsyncClient,
    llm: LLMClient,
    user_id: str,
    optimized: OptimizedDoc,
    body: TailorRequest,
    contact: ContactInfo,
    preferences: PreferencesPayload | None,
) -> None:
    """Detached worker: run the resume pipeline, then clear the in-flight flag.

    A lint failure is NOT a failure here — the pipeline persisted a flagged
    draft, so the run ``finish``es and the client's poll finds a real record
    carrying its violations. Only an exception (LLM, trace validation, DB)
    marks the run ``error`` so the next poll can offer a retry; it must never
    let one escape (``spawn_detached``'s done-callback only logs, it can't
    recover the user's request).
    """
    try:
        await _run_resume_pipeline(
            supabase,
            llm,
            user_id=user_id,
            optimized=optimized,
            body=body,
            contact=contact,
            preferences=preferences,
        )
        run_registry.finish(key)
    except Exception:
        _log.exception("tailor resume task failed job=%s", body.job_posting_id)
        run_registry.fail(key, message="Resume generation failed. Please retry.")


@router.post(
    "/cover-letter",
    response_model=None,
    responses={
        # The 202 is the normal outcome for a client with a job_posting_id;
        # declare it so the contract is discoverable rather than inferred from
        # a handler that returns a raw JSONResponse (response_model=None).
        202: {"model": TailorStatusResponse},
        422: {"model": TailorLintFailureResponse | GapGateFailureResponse},
    },
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
    s: Settings = Depends(get_settings),
) -> TailorResponse | JSONResponse:
    """Kick off a tailored cover letter. Non-blocking when a posting is known.

    Mirror of ``create_tailored_resume`` (#656): 202 + detached task + poll via
    ``GET /tailor/cover-letters/by-job/{id}``. The ~27s pipeline is shorter
    than the resume's but still far past a comfortable request.

    `async def`: the LLM cover-letter pipeline + every DB round-trip run
    natively on the async service client (#57 slice 3).
    """
    current_optimized = await _optimized_latest(supabase, user_id=user_id)
    if current_optimized is None:
        raise HTTPException(
            status_code=404,
            detail="no optimized doc — derive one via POST /experience/derive first",
        )

    _gap_gate_or_422(current_optimized)

    if _already_running(
        user_id=user_id, document_type="cover_letter", job_posting_id=body.job_posting_id
    ):
        return _running_202()

    if body.job_posting_id is not None and not await _posting_exists(supabase, body.job_posting_id):
        raise HTTPException(status_code=404, detail="job posting not found")

    if body.job_posting_id is None:
        prefs_row = await _preferences_get(supabase, user_id=user_id)
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
            preferences=prefs_row.payload if prefs_row else None,
            critique=body.critique,
            job_posting_id=body.job_posting_id,
            target_label=body.target_label,
            # The JD-only path silently dropped this (#780 wired only the
            # backgrounded branch), so a caller without a posting was accepted
            # and then ignored. Same flag, same meaning, both branches.
            allow_stretch=body.allow_stretch,
        )
        # A lint failure is no longer a 422 — the draft is persisted flagged,
        # so it comes back as a normal record whose ``lint_violations`` say why
        # it needs attention (mirrors ``_resume_response``).
        if not isinstance(result, CoverLetterPipelineSuccess | CoverLetterPipelineLintFailure):
            raise HTTPException(status_code=500, detail="Unexpected pipeline result")
        return TailorResponse(record=result.record, lint_warnings=result.lint.warnings)

    key, dedup = _claim_run_or_202(
        user_id=user_id,
        document_type="cover_letter",
        job_posting_id=body.job_posting_id,
        max_concurrent=s.tailor_max_concurrent_runs,
    )
    if dedup is not None:
        return dedup

    try:
        # See create_tailored_resume: contact resolution 400s before the 202
        # so the frontend's inline name prompt still has a status to react to.
        prefs_row = await _preferences_get(supabase, user_id=user_id)
        contact = await resolve_contact(supabase, user_id, body.contact)
    except Exception:
        run_registry.finish(key)
        raise

    spawn_detached(
        _run_cover_letter_task(
            key=key,
            supabase=supabase,
            llm=llm,
            user_id=user_id,
            optimized=current_optimized,
            body=body,
            contact=contact,
            preferences=prefs_row.payload if prefs_row else None,
        ),
        name=f"tailor:cover-letter:{body.job_posting_id}",
    )
    return _running_202()


async def _run_cover_letter_task(
    *,
    key: run_registry.Key,
    supabase: AsyncClient,
    llm: LLMClient,
    user_id: str,
    optimized: OptimizedDoc,
    body: CoverLetterRequest,
    contact: ContactInfo,
    preferences: PreferencesPayload | None,
) -> None:
    """Detached worker for the cover-letter pipeline (#656).

    Same contract as ``_run_resume_task``: a lint failure is a RESULT, not a
    failure — the pipeline persisted a flagged draft, so the run finishes and
    the client's poll finds a real record carrying its violations. Only an
    exception marks the run ``error``.
    """
    try:
        await run_cover_letter_pipeline(
            supabase,
            llm,
            user_id=user_id,
            optimized=optimized,
            job_description=body.job_description,
            company_name=body.company_name,
            contact=contact,
            role_title=body.role_title,
            preferences=preferences,
            critique=body.critique,
            job_posting_id=body.job_posting_id,
            target_label=body.target_label,
            allow_stretch=body.allow_stretch,
        )
        run_registry.finish(key)
    except Exception:
        _log.exception("tailor cover-letter task failed job=%s", body.job_posting_id)
        run_registry.fail(key, message="Cover letter generation failed. Please retry.")


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


async def _document_state(
    supabase: AsyncClient,
    job_posting_id: str,
    *,
    user_id: str,
    document_type: DocumentType,
) -> TailoredDocumentState:
    """Shared poll body: newest persisted document + in-flight run state (#656).

    Read-only — no LLM spend and no writes, so it carries no budget gate and is
    safe to poll on a timer.

    The record is read FIRST and wins: a task that has persisted its row but
    hasn't yet cleared its registry entry must report the finished document,
    not ``running``. The reverse order would make a poll bounce back to
    "generating…" for the width of that window.
    """
    record = await persistence.get_by_job(
        supabase, job_posting_id, user_id=user_id, document_type=document_type
    )
    key = run_registry.key_for(
        user_id=user_id, document_type=document_type, job_posting_id=job_posting_id
    )
    st = run_registry.get(key)
    if record is not None:
        # A settled document. Any lingering ``error`` belongs to a run that
        # already has a persisted predecessor to fall back on, so don't dress
        # a usable draft up as a failure.
        return TailoredDocumentState(record=record, status="idle")
    if st is not None and st.status == "running":
        return TailoredDocumentState(record=None, status="running")
    if st is not None and st.status == "error":
        return TailoredDocumentState(record=None, status="error", message=st.error)
    return TailoredDocumentState(record=None, status="idle")


@router.get("/resumes/by-job/{job_posting_id}")
async def get_resume_by_job(
    job_posting_id: str,
    supabase: AsyncClient = Depends(get_async_user_supabase),
    user_id: str = Depends(get_current_user_id),
) -> TailoredDocumentState:
    """Poll the state of a (possibly backgrounded) resume for a posting (#656).

    Returns a ``TailoredDocumentState`` envelope — ``{record, status,
    message}`` — where it used to return the bare record or ``null``:

    * ``record`` non-null → the document exists (``lint_violations`` marks a
      flagged draft). ``status`` is ``idle``.
    * ``running`` with a null record → a detached generation is in flight;
      keep polling. The work persists regardless of the client, so navigating
      away and coming back picks up the finished draft.
    * ``error`` with a null record → the run failed; offer a retry (POST again).
    * ``idle`` with a null record → nothing here and nothing coming; render the
      "Generate" CTA.

    Still 200-with-null rather than 404 for the empty state, so the browser
    doesn't log a failed request on every job-detail visit before generation.
    """
    return await _document_state(supabase, job_posting_id, user_id=user_id, document_type="resume")


@router.get("/cover-letters/by-job/{job_posting_id}")
async def get_cover_letter_by_job(
    job_posting_id: str,
    supabase: AsyncClient = Depends(get_async_user_supabase),
    user_id: str = Depends(get_current_user_id),
) -> TailoredDocumentState:
    """Poll the state of a (possibly backgrounded) cover letter for a posting.

    Same envelope and semantics as ``get_resume_by_job``, except that a lint
    failure arrives as ``status="error"`` rather than a flagged record — see
    ``CoverLetterPipelineLintFailure``.
    """
    return await _document_state(
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

    A user edit that fails lint is still a 422 (unlike a *generation* lint
    failure, which persists flagged — #656): nothing was spent producing it,
    the user is right there watching, and rejecting the write is what keeps a
    known-bad edit out of the row. Conversely a passing edit writes the fresh
    lint result through, which is what CLEARS a flagged draft the user just
    fixed.
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
        supabase,
        resume_id,
        body.markdown,
        user_id=user_id,
        lint_violations=[v.model_dump() for v in lint_result.violations],
    )
    return TailorResponse(record=record, lint_warnings=lint_result.warnings)


@router.post("/resumes/{resume_id}/ats-recheck")
async def recheck_tailored_resume(
    resume_id: str,
    supabase: AsyncClient = Depends(get_async_user_supabase),
    user_id: str = Depends(get_current_user_id),
) -> AtsRecheckResponse:
    """Re-run ATS lint over the saved markdown and refresh ``lint_violations``.

    The companion to the flagged-draft decision (#656): a generation that
    fails lint is kept rather than discarded, so the user needs a way to fix
    it and confirm the fix. Lint is deterministic — no LLM, no cost, no budget
    gate, no daily cap — so this is free to call as often as they like, which
    is the whole point of not making them regenerate.

    Allowed on approved documents: re-checking inspects content that didn't
    change and only refreshes metadata about it, so there's nothing for the
    approval lock to protect (and knowing a locked resume has an ATS problem
    is exactly when you'd want to unlock it).

    Works for cover letters as well as resumes — both run the same linter and
    both persist flagged, so both need the same free way to confirm a fix.

    Ownership follows the PATCH handler — ``persistence.get(..., user_id=...)``
    returning ``None`` is a 404 whether the row is missing or someone else's,
    so cross-tenant existence never leaks.
    """
    row = await persistence.get(supabase, resume_id, user_id=user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="tailored document not found")
    if not row.payload_md:
        # Legacy rows persisted before markdown became the source of truth have
        # nothing to lint. 422 (not 500) — the request is well-formed, the
        # document just can't answer it.
        raise HTTPException(
            status_code=422,
            detail="this document has no markdown to check",
        )

    lint_result = lint_markdown(row.payload_md, document_type=row.document_type)
    record = await persistence.update_lint_violations(
        supabase,
        resume_id,
        [v.model_dump() for v in lint_result.violations],
        user_id=user_id,
    )
    return AtsRecheckResponse(
        ok=lint_result.ok,
        violations=lint_result.violations,
        record=record,
    )


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
