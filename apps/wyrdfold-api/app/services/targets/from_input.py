"""Create-or-link a target from user-authored input.

Both flows (manual title+description, or JD URL) funnel through a common
shape: LLM normalize -> match against existing -> link or create+link.
This guarantees the user always ends up with a ``user_targets`` row, so
the new target appears in ``GET /targets/mine`` immediately.

URL mode also acts as a corpus builder — when the URL maps to an existing
shared target, the JD is appended as a reference and the composite profile
is re-merged so all linked users benefit from the new data point.

Performance: the only LLM call that has to run inline is
``normalize_manual_input`` — its canonical label drives duplicate
detection via ``find_matching_target``. The expensive steps
(``derive_profile_*`` + ``derive_fit_score``, 5-9s of sequential Sonnet
calls) are deferred to a ``BackgroundTask`` so the endpoint can return an
optimistic ``CreateOrLinkResult`` immediately. New targets are created in
``activation_status="deriving"`` and flip to ``idle`` (or ``error``) once
the background work completes; the frontend polls until then. This mirrors
the ``_activate_pipeline`` BackgroundTask pattern in ``routers/targets``.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pydantic
from fastapi import BackgroundTasks, HTTPException
from supabase import Client

from app.config import settings
from app.models.experience import OptimizedPayload
from app.models.llm import LLMResult
from app.models.targets import (
    CreateOrLinkResult,
    JobTarget,
    ScoringProfile,
    TargetCreate,
    TargetSuggestion,
    TargetUpdate,
)
from app.services.experience.resolve import resolve_current_payload
from app.services.job_ingest import materialize_and_score_job
from app.services.llm import cost_log
from app.services.llm.client import LLMClient
from app.services.source_registration import register_source_from_url
from app.services.tailor import persistence
from app.services.targets import crud
from app.services.targets.derive_profile import (
    DEFAULT_PURPOSE as DERIVE_JD_PURPOSE,
)
from app.services.targets.derive_profile import (
    derive_profile_from_jd,
)
from app.services.targets.derive_profile_from_label import (
    DEFAULT_PURPOSE as DERIVE_LABEL_PURPOSE,
)
from app.services.targets.derive_profile_from_label import (
    derive_profile_from_label,
)
from app.services.targets.fit_score import (
    DEFAULT_PURPOSE as FIT_SCORE_PURPOSE,
)
from app.services.targets.fit_score import (
    derive_fit_score,
)
from app.services.targets.match import find_matching_target
from app.services.targets.merge import merge_reference_jds
from app.services.targets.normalize_manual import (
    DEFAULT_PURPOSE as NORMALIZE_PURPOSE,
)
from app.services.targets.normalize_manual import (
    normalize_manual_input,
)
from app.services.targets.profile_writes import apply_profile_merge_rpc

logger = logging.getLogger(__name__)

# Hard ceiling for a deferred derivation (profile + fit-score LLM calls).
# A hung LLM call would otherwise leave the target stuck in "deriving"
# forever — the timeout cancels the work and flips the target to "error"
# so the frontend stops polling and surfaces the failure. Sized well above
# the observed 5-9s happy path while still bounding the worst case.
DERIVATION_TIMEOUT_S = 60.0


# ---- Deferred fit-score helper ---------------------------------------------


async def _apply_fit_score(
    supabase: Client,
    llm: LLMClient,
    *,
    user_id: str,
    target: JobTarget,
    payload: OptimizedPayload,
) -> None:
    """Derive a per-user fit score and upsert it onto the user's link.

    The user is already linked (the inline path created the
    ``user_targets`` row), so this is an idempotent upsert that just fills
    in ``fit_score`` / ``fit_score_reasoning`` once the LLM returns.

    Scores against a payload FRESH vs. the user's current master document
    (``resolve_current_payload``), not the one captured inline at request time.
    A profile edited just before this target was created must affect its fit —
    otherwise the score is silently computed against stale experience (BUG 2,
    the stale-payload seam). ``payload`` (captured inline) is kept only as a
    fallback for the rare case the live resolve yields nothing.
    """
    fresh = await resolve_current_payload(
        supabase, llm, cost_supabase=supabase, user_id=user_id
    )
    payload = fresh if fresh is not None else payload
    fit_result, llm_result = await derive_fit_score(llm, payload=payload, target=target)
    # supabase-py is sync; this runs inside a BackgroundTask that FastAPI *awaits*
    # on the event loop (an async bg task is not threadpooled), so each blocking
    # round-trip is offloaded to keep it off the loop (#107).
    await asyncio.to_thread(
        cost_log.record,
        supabase,
        user_id=user_id,
        purpose=FIT_SCORE_PURPOSE,
        result=llm_result,
        metadata={"target_id": target.id, "user_id": user_id},
    )
    await asyncio.to_thread(
        crud.link_user_to_target,
        supabase,
        user_id=user_id,
        target_id=target.id,
        is_active=False,
        fit_score=fit_result.fit_score,
        fit_score_reasoning=fit_result.reasoning,
    )


# ---- Background derivation tasks --------------------------------------------


async def derive_manual_target_bg(
    supabase: Client,
    llm: LLMClient,
    *,
    user_id: str,
    target_id: str,
    label: str,
    payload: OptimizedPayload | None,
) -> None:
    """Derive profile-from-label then fit score for a new manual target.

    Runs as a ``BackgroundTask``. The target already exists in
    ``activation_status="deriving"``; on success it flips to ``idle`` with
    the derived scoring profile, on failure (or timeout) to ``error``.

    ``payload`` is ``None`` when the caller has no experience profile (the
    ``from_suggestion`` search-fallback path): the label-derived scoring
    profile is still produced, but the per-user fit score — which needs the
    experience payload — is skipped.
    """
    try:
        async with asyncio.timeout(DERIVATION_TIMEOUT_S):
            derived, derive_result = await derive_profile_from_label(llm, label=label)
            # Blocking supabase calls run in the threadpool: this bg task is an
            # async fn FastAPI awaits on the event loop, so a bare call would
            # block every concurrent request (#107).
            await asyncio.to_thread(
                cost_log.record,
                supabase,
                user_id=user_id,
                purpose=DERIVE_LABEL_PURPOSE,
                result=derive_result,
                metadata={"user_id": user_id, "label": label},
            )
            updated = await asyncio.to_thread(
                crud.update,
                supabase,
                target_id,
                TargetUpdate(
                    scoring_profile=derived.scoring_profile,
                    search_keywords=derived.search_keywords,
                    example_promising_titles=derived.example_promising_titles,
                    example_unpromising_titles=derived.example_unpromising_titles,
                    # Slim shape — populated when the LLM emits them; None is
                    # treated as "leave unchanged" by crud.update, so the
                    # canonical description from normalize survives.
                    description=derived.description,
                    seniority_hint=derived.seniority_hint,
                    domain_hints=derived.domain_hints or None,
                    role_family=derived.role_family,
                    activation_status="idle",
                ),
            )
            if updated is None:
                logger.error("Failed to update target %s after deferred derive", target_id)
                return
            if payload is not None:
                await _apply_fit_score(
                    supabase, llm, user_id=user_id, target=updated, payload=payload
                )
    except TimeoutError:
        logger.error(
            "Deferred manual-target derivation timed out after %ss for target %s",
            DERIVATION_TIMEOUT_S,
            target_id,
        )
        await asyncio.to_thread(
            crud.update, supabase, target_id, TargetUpdate(activation_status="error")
        )
    except Exception:
        logger.exception("Deferred manual-target derivation failed for target %s", target_id)
        await asyncio.to_thread(
            crud.update, supabase, target_id, TargetUpdate(activation_status="error")
        )


def _contribute_reference_jd(
    supabase: Client,
    *,
    user_id: str,
    target_id: str,
    jd_text: str,
    jd_url: str,
    extracted_profile: ScoringProfile,
    search_keywords: list[str],
) -> None:
    """Append a reference JD to a shared target and re-merge its profile under
    the shared-profile write controls: the #47 per-user contribution cap, the
    #5 contributor de-bias, and the #191 version-checked / follower-re-checked
    merge RPC.

    Mirrors ``POST /targets/{id}/reference-jds``. The URL corpus-builder used to
    bypass all three — writing the JD unattributed, merging flat (so one
    contributor's JDs dominated by count), and updating the shared profile with
    a raw version-unguarded ``.update()`` — which let any authenticated follower
    tilt a shared target's scoring profile (hardening review 2026-07-21, SEC-1).

    Runs inside the deferred BackgroundTask, so an over-cap contribution is
    logged and skipped (the caller still fit-scores against the current profile)
    rather than surfaced as an HTTP error.
    """
    # #47 per-user cap — bounds a single follower's footprint on the shared
    # profile. Over cap: skip the contribution entirely (do NOT touch the shared
    # profile).
    contributed = crud.count_user_reference_jds(supabase, target_id=target_id, user_id=user_id)
    if contributed >= settings.reference_jd_max_per_user_per_target:
        logger.info(
            "URL corpus contribution skipped: user %s at reference-JD cap (%d) for target %s",
            user_id,
            settings.reference_jd_max_per_user_per_target,
            target_id,
        )
        return

    # Attributed to the contributing user so the merge can de-bias by contributor.
    crud.add_reference_jd(
        supabase,
        target_id=target_id,
        jd_text=jd_text,
        jd_url=jd_url,
        extracted_profile=extracted_profile,
        user_id=user_id,
    )

    # De-biased re-merge written through the #191 merge RPC (in-DB follower
    # re-check + optimistic version guard). Retry once on a concurrent-write
    # version conflict, mirroring the reference-JD router.
    for _attempt in range(2):
        current = crud.get(supabase, target_id)
        if current is None:
            logger.error("Target %s vanished during URL corpus merge", target_id)
            return
        composite = merge_reference_jds(crud.list_reference_jds(supabase, target_id))
        outcome, _new_version = apply_profile_merge_rpc(
            supabase,
            user_id=user_id,
            target_id=target_id,
            next_profile=composite.model_dump(),
            expected_version=current.profile_version,
            search_keywords=search_keywords,
        )
        if outcome == "version_conflict":
            continue
        if outcome != "applied":
            logger.warning(
                "URL corpus merge for target %s did not apply (outcome=%s)", target_id, outcome
            )
        return
    logger.warning(
        "URL corpus merge for target %s exhausted retries on version conflict", target_id
    )


async def derive_url_target_bg(
    supabase: Client,
    llm: LLMClient,
    *,
    user_id: str,
    target_id: str,
    jd_text: str,
    final_url: str,
    extracted_title: str | None,
    company_name: str | None,
    location: str | None,
    salary_text: str | None,
    payload: OptimizedPayload,
) -> None:
    """Derive profile-from-JD, contribute the reference JD, re-merge, fit score,
    then materialize the posting itself as a saved, tailorable job.

    Runs as a ``BackgroundTask`` for both the new-target and matched
    (corpus-building) URL flows. The shared-profile write goes through
    :func:`_contribute_reference_jd` so the corpus builder is held to the same
    per-user cap + contributor de-bias + version-checked RPC as
    ``POST /targets/{id}/reference-jds`` (SEC-1). On failure (or timeout) the
    target flips to ``error``.
    """
    try:
        async with asyncio.timeout(DERIVATION_TIMEOUT_S):
            derived, derive_result = await derive_profile_from_jd(
                llm, jd_text=jd_text, supabase=supabase
            )
            # Blocking supabase work runs in the threadpool: this bg task is an
            # async fn FastAPI awaits on the event loop, so bare calls would
            # freeze every concurrent request (#107). _contribute_reference_jd
            # bundles its own several round-trips, so one to_thread covers them.
            await asyncio.to_thread(
                cost_log.record,
                supabase,
                user_id=user_id,
                purpose=DERIVE_JD_PURPOSE,
                result=derive_result,
                metadata={"user_id": user_id, "jd_url": final_url},
            )

            await asyncio.to_thread(
                _contribute_reference_jd,
                supabase,
                user_id=user_id,
                target_id=target_id,
                jd_text=jd_text,
                jd_url=final_url,
                extracted_profile=derived.scoring_profile,
                search_keywords=derived.search_keywords,
            )

            # Flip activation status (a per-target column, not the shared
            # profile) and re-read the canonical post-merge target for scoring.
            await asyncio.to_thread(
                crud.update, supabase, target_id, TargetUpdate(activation_status="idle")
            )
            target = await asyncio.to_thread(crud.get, supabase, target_id)
            if target is None:
                logger.error("Target %s vanished during deferred URL derive", target_id)
                return
            await _apply_fit_score(supabase, llm, user_id=user_id, target=target, payload=payload)

            # Materialize the posting itself as a real job: a ``scores`` row
            # under THIS target (so it shows in /jobs and the resume/cover tailor
            # can key on its job_posting_id) plus a "saved" user_jobs row — the
            # user deliberately added this posting. Shared with POST /jobs/manual
            # via materialize_and_score_job. Without this the URL was dissolved
            # into the target profile as a reference JD and never became a job.
            posting_id = await materialize_and_score_job(
                supabase,
                final_url=final_url,
                title=extracted_title,
                company_name=company_name,
                location=location,
                description_html=jd_text,
                salary_text=salary_text,
                targets=[target],
            )
            if posting_id is not None:
                await asyncio.to_thread(
                    persistence.upsert_user_job,
                    supabase,
                    user_id=user_id,
                    job_posting_id=posting_id,
                    status="saved",
                )
    except TimeoutError:
        logger.error(
            "Deferred URL-target derivation timed out after %ss for target %s",
            DERIVATION_TIMEOUT_S,
            target_id,
        )
        await asyncio.to_thread(
            crud.update, supabase, target_id, TargetUpdate(activation_status="error")
        )
    except Exception:
        logger.exception("Deferred URL-target derivation failed for target %s", target_id)
        await asyncio.to_thread(
            crud.update, supabase, target_id, TargetUpdate(activation_status="error")
        )


# ---- Inline create-or-link orchestration -----------------------------------


# User-facing message when the LLM returns output we can't parse into a
# TargetSuggestion. 502 (Bad Gateway): the upstream LLM gave us a malformed
# response, not the client's fault. Matches the transient, retry-friendly
# framing of the LLM error hierarchy in app/services/llm/errors.py without
# leaking the raw pydantic/JSON traceback.
_MALFORMED_SUGGESTION_DETAIL = (
    "Couldn't derive a target profile from the role title — please try again."
)


async def _normalize_suggestion(
    llm: LLMClient,
    *,
    label: str,
    description: str | None,
    payload: OptimizedPayload,
) -> tuple[TargetSuggestion, LLMResult]:
    """Normalize user input into a ``TargetSuggestion``, guarding the parse.

    ``normalize_manual_input`` validates the LLM's tool output against the
    ``TargetSuggestion`` schema. A real LLM occasionally returns output that
    doesn't match (missing/extra fields, non-JSON), which raises
    ``pydantic.ValidationError`` (or a JSON decode error). Left unhandled
    these propagate as a raw 500 with a traceback. Translate them into a
    clean 502 so the caller gets an actionable, retry-friendly message.

    Centralized here so every entry point that derives a ``TargetSuggestion``
    inline (currently ``from_manual``) shares the same guard.
    """
    try:
        return await normalize_manual_input(
            llm, label=label, description=description, payload=payload
        )
    except (pydantic.ValidationError, json.JSONDecodeError) as exc:
        logger.warning(
            "LLM returned malformed TargetSuggestion for label=%r: %s",
            label,
            exc,
        )
        raise HTTPException(status_code=502, detail=_MALFORMED_SUGGESTION_DETAIL) from exc


async def _create_or_link_from_suggestion(
    supabase: Client,
    llm: LLMClient,
    background_tasks: BackgroundTasks,
    *,
    user_id: str,
    suggestion: TargetSuggestion,
    payload: OptimizedPayload | None,
) -> CreateOrLinkResult:
    """Match a canonical ``TargetSuggestion`` against the shared catalog, then
    link the caller to the existing target or create a new one.

    The shared core of ``from_manual`` (after LLM normalization) and
    ``from_suggestion`` (labels already canonical). Deduplicating on
    ``suggestion.label`` here — server-side, at write time — is what keeps two
    users (or a client retry) from minting duplicate catalog rows for the same
    role. Links with ``is_active=False`` so it never trips the active-target
    cap; the user follows the target and activates it separately.

    ``payload`` is the caller's experience profile, or ``None`` when they have
    none. When ``None`` the label-derived scoring profile is still produced,
    but the per-user fit score (which needs the payload) is skipped.
    """
    matched = find_matching_target(supabase, suggestion.label)
    if matched is not None:
        link = crud.link_user_to_target(
            supabase, user_id=user_id, target_id=matched.id, is_active=False
        )
        if payload is not None:
            background_tasks.add_task(
                _apply_fit_score,
                supabase,
                llm,
                user_id=user_id,
                target=matched,
                payload=payload,
            )
        return CreateOrLinkResult(user_target=link, target=matched, was_matched=True)

    # New target: create immediately in "deriving" so it appears in the
    # list with a pending indicator while the background task derives the
    # scoring profile (+ fit score when we have a profile).
    target = crud.create(
        supabase,
        payload=TargetCreate(
            label=suggestion.label,
            description=suggestion.description,
        ),
    )
    target = crud.update(supabase, target.id, TargetUpdate(activation_status="deriving")) or target
    link = crud.link_user_to_target(supabase, user_id=user_id, target_id=target.id, is_active=False)
    background_tasks.add_task(
        derive_manual_target_bg,
        supabase,
        llm,
        user_id=user_id,
        target_id=target.id,
        label=suggestion.label,
        payload=payload,
    )
    return CreateOrLinkResult(user_target=link, target=target, was_matched=False)


async def from_manual(
    supabase: Client,
    llm: LLMClient,
    background_tasks: BackgroundTasks,
    *,
    user_id: str,
    label: str,
    description: str | None,
    payload: OptimizedPayload,
) -> CreateOrLinkResult:
    """Manual flow: user-typed title + description.

    Inline (fast): LLM-normalize the input, match against existing
    targets, and link the user. Deferred (BackgroundTask): derive the
    scoring profile (new targets) and the per-user fit score.

    1. LLM normalizes input into a canonical ``TargetSuggestion``
    2. Match against existing targets
    3. If matched, link the user; defer the fit score
    4. If new, create in ``deriving`` status, link, defer profile + fit score
    """
    suggestion, norm_result = await _normalize_suggestion(
        llm, label=label, description=description, payload=payload
    )
    cost_log.record(
        supabase,
        user_id=user_id,
        purpose=NORMALIZE_PURPOSE,
        result=norm_result,
        metadata={"user_id": user_id, "raw_label": label},
    )
    return await _create_or_link_from_suggestion(
        supabase,
        llm,
        background_tasks,
        user_id=user_id,
        suggestion=suggestion,
        payload=payload,
    )


async def from_suggestion(
    supabase: Client,
    llm: LLMClient,
    background_tasks: BackgroundTasks,
    *,
    user_id: str,
    label: str,
    description: str | None,
    payload: OptimizedPayload | None = None,
) -> CreateOrLinkResult:
    """Create-or-link from an AI search-suggestion the user picked.

    Backs ``POST /targets/from-suggestion`` (the catalog-search LLM fallback).
    The label already came from ``suggest_targets_from_query``, which
    canonicalised it, so — unlike ``from_manual`` — we skip the inline
    normalization LLM call and match the shared catalog directly. Server-side
    dedup means a stale client ``is_new`` (or a race) links the existing row
    instead of minting a duplicate. Profile-independent: ``payload`` is
    optional, and only the per-user fit score is skipped when it's absent.
    """
    suggestion = TargetSuggestion(
        label=label.strip()[:200],
        description=(description or "").strip(),
        core_skills=[],
    )
    return await _create_or_link_from_suggestion(
        supabase,
        llm,
        background_tasks,
        user_id=user_id,
        suggestion=suggestion,
        payload=payload,
    )


async def from_url(
    supabase: Client,
    llm: LLMClient,
    background_tasks: BackgroundTasks,
    *,
    user_id: str,
    final_url: str,
    extracted_title: str | None,
    jd_text: str,
    company_name: str | None,
    location: str | None,
    salary_text: str | None,
    payload: OptimizedPayload,
) -> CreateOrLinkResult:
    """URL flow: validated URL + already-fetched JD.

    The label is ALWAYS derived from the posting's own title — there is no
    user-supplied title override (an inaccurate one poisons matching + the
    shared catalog). Matching keys off that derived title, so duplicate
    detection runs inline without any LLM call. The profile derivation + merge
    + fit score + job materialization are all deferred to a BackgroundTask.
    """
    label = ((extracted_title or "").strip() or "Untitled Target")[:200]

    matched = find_matching_target(supabase, label)
    if matched is not None:
        link = crud.link_user_to_target(
            supabase, user_id=user_id, target_id=matched.id, is_active=False
        )
        background_tasks.add_task(
            derive_url_target_bg,
            supabase,
            llm,
            user_id=user_id,
            target_id=matched.id,
            jd_text=jd_text,
            final_url=final_url,
            extracted_title=extracted_title,
            company_name=company_name,
            location=location,
            salary_text=salary_text,
            payload=payload,
        )
        # Also feed the job search: register the company's board as a pollable
        # source (best-effort, capped) so we pull MORE jobs from it going
        # forward. Scheduled AFTER derive so the user-visible deriving→ready
        # flip isn't held behind the ATS probe.
        background_tasks.add_task(
            register_source_from_url, supabase, user_id=user_id, final_url=final_url
        )
        return CreateOrLinkResult(user_target=link, target=matched, was_matched=True)

    target = crud.create(supabase, payload=TargetCreate(label=label))
    target = crud.update(supabase, target.id, TargetUpdate(activation_status="deriving")) or target
    link = crud.link_user_to_target(supabase, user_id=user_id, target_id=target.id, is_active=False)
    background_tasks.add_task(
        derive_url_target_bg,
        supabase,
        llm,
        user_id=user_id,
        target_id=target.id,
        jd_text=jd_text,
        final_url=final_url,
        extracted_title=extracted_title,
        company_name=company_name,
        location=location,
        salary_text=salary_text,
        payload=payload,
    )
    # Also feed the job search — see the matched branch above.
    background_tasks.add_task(
        register_source_from_url, supabase, user_id=user_id, final_url=final_url
    )
    return CreateOrLinkResult(user_target=link, target=target, was_matched=False)
