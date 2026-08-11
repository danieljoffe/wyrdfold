"""Experience router.

CRUD over prose docs, optimized docs, conversation turns, and preferences.
Creating a new optimized doc also embeds + writes its chunks.
POST /experience/derive runs the end-to-end loop: prose -> LLM -> optimized
doc -> chunks, all cost-logged.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from supabase import AsyncClient

from app.constants import resolve_owner
from app.dependencies import (
    enforce_llm_budget,
    get_async_service_supabase,
    get_async_user_supabase,
    get_current_user_id,
    get_embeddings_client,
    get_llm_client,
    verify_api_key_or_jwt,
)
from app.models.conversation import (
    GapHealthResult,
    ProbeResult,
    ResetResult,
    TurnRequest,
    TurnResult,
)
from app.models.experience import (
    ConversationType,
    OptimizedDoc,
    OptimizedDocSource,
    OptimizedDocUpsert,
    OptimizedPayload,
    Preferences,
    PreferencesUpsert,
    ProseConsolidateResponse,
    ProseDoc,
    ProseDocCreate,
    ResumeUploadResponse,
    TurnAppend,
)
from app.models.llm import Message
from app.rate_limit import limiter
from app.services.conversation import orchestrator
from app.services.embeddings.client import EmbeddingsClient
from app.services.experience import (
    annotations,
    chunks,
    consolidate,
    derive,
    gap_tracker,
    optimized,
    preferences,
    prose,
    turns,
)
from app.services.ingest import merge_into_prose, parse_resume
from app.services.ingest.parse import ParseError
from app.services.ingest.storage import upload_file
from app.services.llm import cost_log
from app.services.llm.client import LLMClient, strip_markdown_fence

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/experience",
    tags=["experience"],
    dependencies=[Depends(verify_api_key_or_jwt)],
)

# Wall-clock bound on resume text extraction (#29 M6). Generous — a normal
# PDF/DOCX parses in well under a second; this only trips on a corrupt or
# adversarially-complex file, returning 422 promptly instead of hanging.
_PARSE_TIMEOUT_SECONDS = 30.0


# ---- Inline async reads/writes (#57 slice 3) ------------------------------
#
# Handlers now run on the async user/service client. A few experience-service
# helpers (``prose.get_latest`` / ``prose.create_version`` / ``optimized.get_latest``
# / ``preferences.get``) are still called SYNC by not-yet-converted modules
# (tailor, analysis, targets, fit_refresh) that hold a
# sync client — converting them would break those callers, and a sync helper
# cannot take the async client. So the sync helpers stay, and these thin async
# inlines run the same queries on the async client (the insights.py::_user_target_ids
# pattern). No sync+async twin in the shared layer.


async def _prose_latest(supabase: AsyncClient, user_id: str | None) -> ProseDoc | None:
    """Async inline of ``prose.get_latest`` (sync twin kept for orchestrator/fit_refresh)."""
    resp = await (
        supabase.table(prose.TABLE)
        .select("*")
        .order("version", desc=True)
        .limit(1)
        .eq("user_id", resolve_owner(user_id))
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    return ProseDoc.model_validate(rows[0]) if rows else None


async def _prose_create_version(
    supabase: AsyncClient, user_id: str | None, content: str
) -> ProseDoc:
    """Async inline of ``prose.create_version`` (sync twin kept for orchestrator)."""
    latest = await _prose_latest(supabase, user_id)
    next_version = (latest.version + 1) if latest else 1
    resp = await (
        supabase.table(prose.TABLE)
        .insert({"user_id": user_id, "version": next_version, "content": content})
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    if not rows:
        raise RuntimeError("Failed to insert prose doc version")
    return ProseDoc.model_validate(rows[0])


async def _optimized_latest(supabase: AsyncClient, user_id: str | None) -> OptimizedDoc | None:
    """Async inline of ``optimized.get_latest`` (sync twin kept for
    tailor/analysis/targets/orchestrator/annotations). Reads fresh: the module TTL
    cache is populated/read only by those sync callers and every write invalidates
    it, so a direct read here is always coherent."""
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


async def _optimized_create_version(
    supabase: AsyncClient,
    user_id: str | None,
    payload: OptimizedPayload,
    prose_doc_id: str | None,
    source: OptimizedDocSource,
    markdown_view: str | None = None,
) -> OptimizedDoc:
    """Async inline of ``optimized.create_version`` — the sync twin stays for its
    not-yet-converted caller chain (``annotations`` ← conversation orchestrator).
    Invalidates the shared module TTL cache so sync readers (tailor/analysis/
    targets) never serve the pre-write version (#57 slice 3)."""
    latest = await _optimized_latest(supabase, user_id)
    next_version = (latest.version + 1) if latest else 1
    resp = await (
        supabase.table(optimized.TABLE)
        .insert(
            {
                "user_id": user_id,
                "prose_doc_id": prose_doc_id,
                "version": next_version,
                "payload": payload.model_dump(mode="json"),
                "markdown_view": markdown_view,
                "source": source,
            }
        )
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    if not rows:
        raise RuntimeError("Failed to insert optimized doc version")
    doc = OptimizedDoc.model_validate(rows[0])
    optimized._doc_cache.invalidate(optimized._cache_key(user_id))
    return doc


async def _preferences_get(supabase: AsyncClient, user_id: str | None) -> Preferences | None:
    """Async inline of ``preferences.get`` (sync twin kept for tailor)."""
    resp = await (
        supabase.table(preferences.TABLE)
        .select("*")
        .limit(1)
        .eq("user_id", resolve_owner(user_id))
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    return Preferences.model_validate(rows[0]) if rows else None


async def _insert_uploaded_resume(supabase: AsyncClient, row: dict[str, Any]) -> None:
    """Async inline of the ``uploaded_resumes`` tracking insert. Lives in a
    helper (not the handler body) so the #107 guard sees the handler delegate
    the round-trip rather than run a bare ``.execute()`` on the loop."""
    await supabase.table("uploaded_resumes").insert(row).execute()


# ---- Prose doc ------------------------------------------------------------


# `async def` on the async user client (#57 slice 3): the DB read runs natively
# on the event loop, no threadpool worker held for the supabase round-trip.
@router.get("/prose")
async def get_prose(
    supabase: AsyncClient = Depends(get_async_user_supabase),
    user_id: str = Depends(get_current_user_id),
) -> ProseDoc | dict[str, None]:
    doc = await _prose_latest(supabase, user_id=user_id)
    if doc is None:
        return {"prose": None}
    return doc


@router.post("/prose")
async def create_prose(
    body: ProseDocCreate,
    supabase: AsyncClient = Depends(get_async_user_supabase),
    user_id: str = Depends(get_current_user_id),
) -> ProseDoc:
    return await _prose_create_version(supabase, user_id=user_id, content=body.content)


@router.delete("/prose")
async def delete_master_document(
    supabase: AsyncClient = Depends(get_async_user_supabase),
    user_id: str = Depends(get_current_user_id),
) -> ResetResult:
    """Delete the master document and everything derived from it.

    Wipes the prose doc(s) and the derived optimized doc (embedding chunks
    cascade) so the *next* upload starts from a clean slate instead of
    semantically merging the new resume into the old document (see
    ``merge_into_prose``). Conversation turns and preferences are kept — this
    deletes the document, not the account's experience history. Runs on the RLS
    client (Phase 1): RLS scopes the wipe to the caller's own rows
    (``auth.uid() = user_id``), backstopping the app-layer ``user_id`` filter.
    """
    return await orchestrator.reset_content(supabase, user_id=user_id, include_turns=False)


@router.post("/prose/consolidate", dependencies=[Depends(enforce_llm_budget)])
@limiter.limit("3/minute")
async def consolidate_prose(
    request: Request,
    supabase: AsyncClient = Depends(get_async_user_supabase),
    llm: LLMClient = Depends(get_llm_client),
    user_id: str = Depends(get_current_user_id),
    cost_supabase: AsyncClient = Depends(get_async_service_supabase),  # service-role cost ledger
) -> ProseConsolidateResponse:
    """LLM-dedupe the latest prose doc and persist as a new version.

    Older docs that grew via naive concat-with-divider on each upload often
    contain multiple near-identical resume copies. This pass merges them.
    The result is always a new version — the original stays in history.
    """
    latest = await _prose_latest(supabase, user_id=user_id)
    if latest is None:
        raise HTTPException(status_code=404, detail="no prose doc to consolidate")

    consolidated, result, fallback_reason = await consolidate.consolidate_prose(
        llm, content=latest.content
    )
    if result is not None:
        metadata: dict[str, str | int | float | bool] = {
            "prose_doc_id": latest.id,
            "prose_version": latest.version,
            "chars_before": len(latest.content),
            "chars_after": len(consolidated),
        }
        if fallback_reason is not None:
            metadata["fallback_reason"] = fallback_reason
        await cost_log.record_async(
            cost_supabase,
            user_id=user_id,
            purpose=consolidate.DEFAULT_PURPOSE,
            result=result,
            metadata=metadata,
        )

    no_op = consolidate.is_no_op(before=latest.content, after=consolidated)
    if no_op and consolidated == latest.content:
        # Nothing to persist — either too short to consolidate, the LLM
        # returned the input unchanged, or the safety net rejected the LLM
        # output. Return the existing version as-is, with fallback_reason
        # set when the rejection path was taken.
        return ProseConsolidateResponse(
            prose=latest,
            chars_before=len(latest.content),
            chars_after=len(latest.content),
            no_op=True,
            fallback_reason=fallback_reason,
        )

    new_doc = await _prose_create_version(supabase, user_id=user_id, content=consolidated)
    return ProseConsolidateResponse(
        prose=new_doc,
        chars_before=len(latest.content),
        chars_after=len(consolidated),
        no_op=no_op,
        fallback_reason=fallback_reason,
    )


# ---- Resume upload --------------------------------------------------------


@router.post("/upload-resume", dependencies=[Depends(enforce_llm_budget)])
@limiter.limit("3/minute")
async def upload_resume(
    request: Request,
    file: UploadFile,
    auto_derive: bool = Query(default=False),
    supabase: AsyncClient = Depends(get_async_service_supabase),
    llm: LLMClient = Depends(get_llm_client),
    embeddings: EmbeddingsClient = Depends(get_embeddings_client),
    user_id: str = Depends(get_current_user_id),
) -> ResumeUploadResponse:
    """Upload a resume file (PDF/DOCX), extract text, merge into prose doc.

    JWT-required so the file lands under the caller's ``<user_id>/`` Storage
    folder (no more ``anon/``). The write itself goes through the service-role
    client to the verified user's folder — uploads also run from background
    contexts (batch) where a per-request user client wouldn't be valid; read
    access is what storage RLS enforces, on the user client.
    """
    content_type = file.content_type or ""
    filename = file.filename or "unknown"

    max_upload_bytes = 10 * 1024 * 1024  # 10 MB
    file_bytes = await file.read(max_upload_bytes + 1)
    if not file_bytes:
        raise HTTPException(status_code=422, detail="Empty file")
    if len(file_bytes) > max_upload_bytes:
        raise HTTPException(status_code=413, detail="File exceeds 10 MB limit")

    try:
        # Bound the parse: a pathological PDF/DOCX (already ≤10 MB, authed) must
        # not hang the request and block the event-loop await indefinitely.
        # ``wait_for`` frees the request promptly on timeout; the worker thread
        # can't be hard-killed (only a subprocess could), but for an authed,
        # capped, infrequent upload that residual isn't worth subprocess
        # isolation. Normal parses are sub-second, so this only trips on abuse
        # or a corrupt file. (#29 M6)
        parsed = await asyncio.wait_for(
            asyncio.to_thread(parse_resume, file_bytes, filename, content_type),
            timeout=_PARSE_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        raise HTTPException(
            status_code=422,
            detail="Could not parse the file in time — it may be corrupt or too complex.",
        ) from None
    except ValueError as exc:
        if "too large" in str(exc).lower():
            raise HTTPException(status_code=413, detail=str(exc)) from None
        raise HTTPException(status_code=415, detail=str(exc)) from None
    except ParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    if not parsed.text.strip():
        raise HTTPException(status_code=422, detail="No text could be extracted from file")

    warnings = list(parsed.warnings)

    # Store original file in Supabase Storage
    import uuid

    upload_id = str(uuid.uuid4())
    file_ext = parsed.file_type
    try:
        storage_path = await upload_file(
            supabase,
            user_id=user_id,
            upload_id=upload_id,
            file_bytes=file_bytes,
            file_ext=file_ext,
            content_type=content_type,
        )
    except Exception:
        warnings.append("storage_upload_failed")
        storage_path = ""

    # Merge into prose doc — semantic merge via LLM (#497).
    existing = await _prose_latest(supabase, user_id=user_id)
    merged, merge_result = await merge_into_prose(
        llm,
        existing_content=existing.content if existing else None,
        parsed=parsed,
    )
    prose_doc = await _prose_create_version(supabase, user_id=user_id, content=merged)
    if merge_result is not None:
        await cost_log.record_async(
            supabase,
            user_id=user_id,
            purpose="experience.ingest_merge",
            result=merge_result,
            metadata={"prose_doc_id": prose_doc.id, "filename": filename},
        )

    # Track the upload
    upload_row: dict[str, Any] = {
        "id": upload_id,
        "user_id": user_id,
        "filename": filename,
        "file_type": parsed.file_type,
        "storage_path": storage_path,
        "extracted_text": parsed.text,
        "prose_doc_id": prose_doc.id,
        "page_count": parsed.page_count,
        "file_size_bytes": len(file_bytes),
        "warnings": warnings,
    }
    await _insert_uploaded_resume(supabase, upload_row)

    # Optional: auto-derive
    optimized_doc_id: str | None = None
    if auto_derive:
        payload, result = await derive.derive_from_prose(llm, prose_text=prose_doc.content)
        await cost_log.record_async(
            supabase,
            user_id=user_id,
            purpose=derive.DEFAULT_PURPOSE,
            result=result,
            metadata={"prose_doc_id": prose_doc.id, "prose_version": prose_doc.version},
        )

        # Carry forward annotations from previous doc and merge with any
        # the LLM extracted from inline prose comments this round (#499).
        previous_opt = await _optimized_latest(supabase, user_id=user_id)
        carried = (
            annotations.validate_annotation_refs(previous_opt.payload.annotations, payload)
            if previous_opt and previous_opt.payload.annotations
            else []
        )
        merged_annotations = annotations.merge_annotations(carried, payload.annotations)
        payload = payload.model_copy(update={"annotations": merged_annotations})

        doc = await _optimized_create_version(
            supabase,
            user_id=user_id,
            payload=payload,
            prose_doc_id=prose_doc.id,
            source="llm",
        )
        await chunks.upsert_for_optimized(supabase, embeddings, doc, user_id=user_id)
        optimized_doc_id = doc.id

    return ResumeUploadResponse(
        success=True,
        prose_doc_id=prose_doc.id,
        prose_version=prose_doc.version,
        upload_id=upload_id,
        extracted_chars=len(parsed.text),
        filename=filename,
        warnings=warnings,
        optimized_doc_id=optimized_doc_id,
    )


# ---- Optimized doc --------------------------------------------------------


@router.get("/optimized")
async def get_optimized(
    supabase: AsyncClient = Depends(get_async_user_supabase),
    user_id: str = Depends(get_current_user_id),
) -> OptimizedDoc | dict[str, None]:
    doc = await _optimized_latest(supabase, user_id=user_id)
    if doc is None:
        return {"optimized": None}
    return doc


@router.post("/optimized")
async def create_optimized(
    body: OptimizedDocUpsert,
    supabase: AsyncClient = Depends(get_async_user_supabase),
    embeddings: EmbeddingsClient = Depends(get_embeddings_client),
    user_id: str = Depends(get_current_user_id),
    cost_supabase: AsyncClient = Depends(get_async_service_supabase),  # service-role cost ledger
) -> OptimizedDoc:
    doc = await _optimized_create_version(
        supabase,
        user_id=user_id,
        payload=body.payload,
        prose_doc_id=body.prose_doc_id,
        source=body.source,
        markdown_view=body.markdown_view,
    )
    await chunks.upsert_for_optimized(
        supabase,
        embeddings,
        doc,
        user_id=user_id,
        cost_supabase=cost_supabase,
    )
    return doc


@router.post("/derive", dependencies=[Depends(enforce_llm_budget)])
@limiter.limit("10/minute")
async def derive_optimized(
    request: Request,
    supabase: AsyncClient = Depends(get_async_user_supabase),
    llm: LLMClient = Depends(get_llm_client),
    embeddings: EmbeddingsClient = Depends(get_embeddings_client),
    user_id: str = Depends(get_current_user_id),
    # Per-user data (optimized doc, chunks) goes through the RLS client above;
    # the cost ledger goes through this service-role client — llm_costs has no
    # INSERT policy for `authenticated` on purpose (a user must not be able to
    # write cost rows). #88/Phase-1 dual-client pattern.
    cost_supabase: AsyncClient = Depends(get_async_service_supabase),
) -> OptimizedDoc:
    """Read the latest prose doc, derive an OptimizedPayload via LLM,
    persist it as a new optimized version, embed its chunks, and log cost.

    Short-circuits when the latest LLM-sourced optimized doc already points
    at this prose doc — repeat derives on unchanged prose are a 40s no-op
    otherwise. User-edited optimized docs (source="user_edit") never
    short-circuit; the user has explicitly asked to regenerate.
    """
    prose_doc = await _prose_latest(supabase, user_id=user_id)
    if prose_doc is None:
        raise HTTPException(status_code=404, detail="no prose doc to derive from")

    previous = await _optimized_latest(supabase, user_id=user_id)
    if previous is not None and previous.prose_doc_id == prose_doc.id and previous.source == "llm":
        return previous

    payload, result = await derive.derive_from_prose(
        llm,
        prose_text=prose_doc.content,
    )
    await cost_log.record_async(
        cost_supabase,
        user_id=user_id,
        purpose=derive.DEFAULT_PURPOSE,
        result=result,
        metadata={"prose_doc_id": prose_doc.id, "prose_version": prose_doc.version},
    )

    # Carry forward annotations from the previous doc and merge with any
    # the LLM extracted from inline prose comments this round (#499).
    carried = (
        annotations.validate_annotation_refs(previous.payload.annotations, payload)
        if previous and previous.payload.annotations
        else []
    )
    merged = annotations.merge_annotations(carried, payload.annotations)
    payload = payload.model_copy(update={"annotations": merged})

    doc = await _optimized_create_version(
        supabase,
        user_id=user_id,
        payload=payload,
        prose_doc_id=prose_doc.id,
        source="llm",
    )
    await chunks.upsert_for_optimized(
        supabase,
        embeddings,
        doc,
        user_id=user_id,
        cost_supabase=cost_supabase,
    )
    return doc


def _sse_event(event: str, data: dict[str, Any]) -> bytes:
    """Format a Server-Sent Events frame.

    The blank line that follows ``data:`` terminates the event. ``data`` is
    JSON-encoded as a single line — the SSE spec disallows raw newlines in
    a single ``data:`` field, and ``json.dumps`` defaults to compact output
    so we satisfy that automatically.
    """
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


@router.post("/derive/stream", dependencies=[Depends(enforce_llm_budget)])
@limiter.limit("10/minute")
async def derive_optimized_stream(
    request: Request,
    supabase: AsyncClient = Depends(get_async_user_supabase),
    llm: LLMClient = Depends(get_llm_client),
    embeddings: EmbeddingsClient = Depends(get_embeddings_client),
    user_id: str = Depends(get_current_user_id),
    cost_supabase: AsyncClient = Depends(get_async_service_supabase),  # service-role cost ledger
) -> StreamingResponse:
    """Streaming variant of /derive.

    Emits SSE frames for each LLM text delta so the client can render fields
    progressively (a 40s wall-clock derive becomes "watch the resume appear"
    instead of a 40s spinner). Concludes with a single ``done`` event whose
    payload is the persisted ``OptimizedDoc``. Same skip-when-unchanged
    short-circuit as /derive — when triggered, the response contains a
    single ``done`` event with ``cached: true`` and no deltas.

    Errors that occur after the response stream opens are surfaced via an
    ``error`` SSE event rather than as an HTTP error code, since headers
    have already been sent. Pre-flight errors (missing prose) still come
    back as HTTP 404 before any SSE frame is written.
    """
    prose_doc = await _prose_latest(supabase, user_id=user_id)
    if prose_doc is None:
        raise HTTPException(status_code=404, detail="no prose doc to derive from")

    previous = await _optimized_latest(supabase, user_id=user_id)

    async def generate() -> AsyncIterator[bytes]:
        if (
            previous is not None
            and previous.prose_doc_id == prose_doc.id
            and previous.source == "llm"
        ):
            yield _sse_event(
                "done",
                {"doc": previous.model_dump(mode="json"), "cached": True},
            )
            return

        buffered_text: list[str] = []
        result = None
        # Hold the generator so we can close it in ``finally`` (#29 M-r2-4): a
        # mid-stream failure or a disconnect must not leave the upstream LLM
        # stream dangling for the GC to reap.
        stream = llm.stream(
            model=derive.DEFAULT_MODEL,
            system=derive.SYSTEM_PROMPT,
            messages=[Message(role="user", content=prose_doc.content)],
            purpose=derive.DEFAULT_PURPOSE,
            max_tokens=derive.DEFAULT_MAX_TOKENS,
            cache_system=True,
        )
        try:
            async for event in stream:
                # Stop burning LLM/BYOK tokens the moment the client goes away
                # (#29 M-r2-3) — an abandoned derive would otherwise consume the
                # whole stream + persist a doc nobody is waiting for.
                if await request.is_disconnected():
                    logger.info(
                        "derive/stream: client disconnected mid-stream "
                        "(user=%s); aborting to stop token spend",
                        user_id,
                    )
                    return
                if event.type == "delta":
                    buffered_text.append(event.text)
                    yield _sse_event("delta", {"text": event.text})
                else:
                    result = event.result

            if result is None:
                yield _sse_event("error", {"detail": "stream ended without a final event"})
                return

            try:
                payload = OptimizedPayload.model_validate_json(strip_markdown_fence(result.content))
            except ValidationError as exc:
                yield _sse_event("error", {"detail": f"invalid payload: {exc}"})
                return

            await cost_log.record_async(
                cost_supabase,
                user_id=user_id,
                purpose=derive.DEFAULT_PURPOSE,
                result=result,
                metadata={
                    "prose_doc_id": prose_doc.id,
                    "prose_version": prose_doc.version,
                    "streamed": True,
                },
            )

            carried = (
                annotations.validate_annotation_refs(previous.payload.annotations, payload)
                if previous and previous.payload.annotations
                else []
            )
            merged = annotations.merge_annotations(carried, payload.annotations)
            payload = payload.model_copy(update={"annotations": merged})

            doc = await _optimized_create_version(
                supabase,
                user_id=user_id,
                payload=payload,
                prose_doc_id=prose_doc.id,
                source="llm",
            )
            await chunks.upsert_for_optimized(
                supabase,
                embeddings,
                doc,
                user_id=user_id,
                cost_supabase=cost_supabase,
            )

            yield _sse_event(
                "done",
                {"doc": doc.model_dump(mode="json"), "cached": False},
            )
        except Exception:
            # A provider error mid-stream (or a downstream failure) must close
            # the SSE cleanly with a terminal ``error`` frame rather than a
            # truncated stream the client can only detect by timeout (#29 M-r2-4).
            logger.exception("derive/stream failed for user=%s", user_id)
            yield _sse_event("error", {"detail": "the derive stream failed; please retry"})
        finally:
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                await aclose()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # disable nginx response buffering
        },
    )


# ---- Gap health (#498) ----------------------------------------------------


@router.get("/gap-health")
async def get_gap_health(
    supabase: AsyncClient = Depends(get_async_user_supabase),
    user_id: str = Depends(get_current_user_id),
) -> GapHealthResult:
    doc = await _optimized_latest(supabase, user_id=user_id)
    if doc is None:
        return gap_tracker.gap_health(OptimizedPayload())
    return gap_tracker.gap_health(doc.payload)


# ---- Preferences ----------------------------------------------------------


# `async def` on the async user client (#57 slice 3).
@router.get("/preferences")
async def get_preferences(
    supabase: AsyncClient = Depends(get_async_user_supabase),
    user_id: str = Depends(get_current_user_id),
) -> Preferences | dict[str, None]:
    row = await _preferences_get(supabase, user_id=user_id)
    if row is None:
        return {"preferences": None}
    return row


@router.put("/preferences")
async def upsert_preferences(
    body: PreferencesUpsert,
    supabase: AsyncClient = Depends(get_async_user_supabase),
    user_id: str = Depends(get_current_user_id),
) -> Preferences:
    return await preferences.upsert(supabase, user_id=user_id, payload=body.payload)


@router.delete("/preferences")
async def reset_preferences(
    supabase: AsyncClient = Depends(get_async_user_supabase),
    user_id: str = Depends(get_current_user_id),
) -> dict[str, bool]:
    await preferences.reset(supabase, user_id=user_id)
    return {"success": True}


# ---- Conversation turns --------------------------------------------------


# `async def` on the async user client (#57 slice 4): the DB read runs natively
# on the event loop.
@router.get("/turns")
async def list_turns(
    conversation_type: ConversationType | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    supabase: AsyncClient = Depends(get_async_user_supabase),
    user_id: str = Depends(get_current_user_id),
) -> dict[str, Any]:
    rows = await turns.list_turns(
        supabase,
        user_id=user_id,
        conversation_type=conversation_type,
        limit=limit,
    )
    return {"turns": [r.model_dump(mode="json") for r in rows]}


# `async def` on the async user client (#57 slice 4): the DB write runs natively
# on the event loop.
@router.post("/turns")
async def append_turn(
    body: TurnAppend,
    supabase: AsyncClient = Depends(get_async_user_supabase),
    user_id: str = Depends(get_current_user_id),
) -> dict[str, Any]:
    if body.skipped and body.role != "user":
        raise HTTPException(status_code=400, detail="only user turns can be skipped")
    turn = await turns.append(
        supabase,
        user_id=user_id,
        conversation_type=body.conversation_type,
        role=body.role,
        content=body.content,
        skipped=body.skipped,
        prose_doc_id=body.prose_doc_id,
    )
    return turn.model_dump(mode="json")


# ---- Conversation orchestrator (P2d) -------------------------------------


@router.post("/conversation/turn", dependencies=[Depends(enforce_llm_budget)])
@limiter.limit("10/minute")
async def conversation_turn(
    request: Request,
    body: TurnRequest,
    supabase: AsyncClient = Depends(get_async_user_supabase),
    llm: LLMClient = Depends(get_llm_client),
    user_id: str = Depends(get_current_user_id),
    cost_supabase: AsyncClient = Depends(get_async_service_supabase),  # service-role cost ledger
) -> TurnResult:
    """Run one orchestrated turn. Persists user + assistant turns,
    appends to prose doc if the LLM determined fresh content was shared.
    """
    return await orchestrator.handle_turn(
        supabase,
        llm,
        user_id=user_id,
        conversation_type=body.conversation_type,
        user_content=body.content,
        skipped=body.skipped,
        cost_supabase=cost_supabase,
    )


@router.post("/conversation/reset")
async def conversation_reset(
    supabase: AsyncClient = Depends(get_async_user_supabase),
    user_id: str = Depends(get_current_user_id),
) -> ResetResult:
    """Wipe prose, optimized (chunks cascade), and turns. Preferences are
    preserved — delete them via DELETE /experience/preferences if wanted.
    """
    return await orchestrator.reset_content(supabase, user_id=user_id)


@router.get("/conversation/next-probe", dependencies=[Depends(enforce_llm_budget)])
async def conversation_next_probe(
    supabase: AsyncClient = Depends(get_async_user_supabase),
    llm: LLMClient = Depends(get_llm_client),
    user_id: str = Depends(get_current_user_id),
    cost_supabase: AsyncClient = Depends(get_async_service_supabase),  # service-role cost ledger
) -> ProbeResult:
    """Top-priority gap phrased as a user-facing question by the LLM."""
    return await orchestrator.next_probe(
        supabase, llm, user_id=user_id, cost_supabase=cost_supabase
    )
