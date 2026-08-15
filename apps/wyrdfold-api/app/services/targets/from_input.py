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
calls) are deferred to a DETACHED task so the endpoint can return an
optimistic ``CreateOrLinkResult`` immediately. New targets are created in
``activation_status="deriving"`` and flip to ``idle`` (or ``error``) once
the background work completes; the frontend polls until then.

#57 PR-G2b: this module runs on the pooled async service client
(``AsyncClient``). The interactive create-or-link path writes its own crud
reads/writes through thin async inlines here (crud stays SYNC for the
poller/learner — see the ``_link`` etc. helpers below), the cost ledger via
``cost_log.record_async``, and the #191 shared-profile merge via
``apply_profile_merge_rpc_async``. The deferred derivation tasks touch that
same async pool, so they are spawned as DETACHED loop tasks
(``spawn_detached``) rather than starlette ``BackgroundTasks`` — the latter
deadlocks the pooled async client under uvloop (see ``app/background.py``).
The derive → contribute → fit → materialize chain rides the async service client
end to end (#57 PR-G2e-4: ``derive_profile_from_jd`` async cache path, async
``materialize_and_score_job``, the ``_upsert_user_job_async`` inline). #57 PR-G2e-5
closed the last two deep services: ``resolve_current_payload`` (experience/prose:
``prose.get_latest`` + ``optimized.get_latest``) in ``_apply_fit_score`` and
``register_source_from_url`` (source registration) in ``from_url`` are async now,
so this module holds no locally-obtained SYNC service client at all.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any, cast

import pydantic
from fastapi import HTTPException
from supabase import AsyncClient

from app.background import spawn_detached
from app.config import settings
from app.models.experience import OptimizedPayload
from app.models.llm import LLMResult
from app.models.targets import (
    CreateOrLinkResult,
    JobTarget,
    ScoringProfile,
    TargetCreate,
    TargetReferenceJD,
    TargetSuggestion,
    TargetUpdate,
    UserTarget,
)
from app.services.experience.resolve import resolve_current_payload
from app.services.job_ingest import materialize_and_score_job
from app.services.llm import cost_log
from app.services.llm.client import LLMClient
from app.services.source_registration import register_source_from_url
from app.services.targets import crud
from app.services.targets.activation import ActivationError
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
from app.services.targets.normalize_posting_title import (
    normalize_posting_title,
)
from app.services.targets.profile_writes import apply_profile_merge_rpc_async

logger = logging.getLogger(__name__)

# Hard ceiling for a deferred derivation (profile + fit-score LLM calls).
# A hung LLM call would otherwise leave the target stuck in "deriving"
# forever — the timeout cancels the work and flips the target to "error"
# so the frontend stops polling and surfaces the failure. Sized well above
# the observed 5-9s happy path while still bounding the worst case.
DERIVATION_TIMEOUT_S = 60.0


# ── Async inlines of shared crud reads/writes (#57 PR-G2b) ───────────────────
# crud stays SYNC for its poller/learner/discovery callers; a sync helper can't
# take the async client and converting crud would ripple into all of them. So
# the queries this module needs run here on the async client, reusing crud's
# row→model parsers so the persisted shape stays byte-identical. Mirrors the
# router-inline pattern in routers/targets.py.


async def _update(supabase: AsyncClient, target_id: str, payload: TargetUpdate) -> JobTarget | None:
    """Async inline of ``crud.update`` — shares its field mapping verbatim."""
    updates = crud.build_update_fields(payload)
    resp = await supabase.table(crud.TARGETS_TABLE).update(updates).eq("id", target_id).execute()
    rows = cast(list[dict[str, Any]], resp.data or [])
    return crud._parse_target(rows[0]) if rows else None


async def _get(supabase: AsyncClient, target_id: str) -> JobTarget | None:
    """Async inline of ``crud.get``."""
    resp = await supabase.table(crud.TARGETS_TABLE).select("*").eq("id", target_id).execute()
    rows = cast(list[dict[str, Any]], resp.data or [])
    return crud._parse_target(rows[0]) if rows else None


async def _create_and_link(
    supabase: AsyncClient,
    *,
    user_id: str,
    payload: TargetCreate,
    activation_status: str | None = None,
) -> tuple[JobTarget, UserTarget]:
    """Find-or-create a target and link the caller to it, ATOMICALLY (#667).

    Replaces the create -> update-status -> link trio of round-trips. Between
    the first and the last, the target existed with `app_active = false` and no
    membership — which IS the definition of an orphan, so an orphan was not a
    detectable state but one the happy path passes through. Every cleanup
    predicate then had to guess (by age) whether a row was being born or
    abandoned. One transaction removes the guess.

    It also fixes a real create-side bug: when the membership insert failed
    after the target insert succeeded, the user got an error AND a permanent
    orphan. Now it is both rows or neither.

    Semantics are unchanged — see the RPC's own comment for how the
    find-or-create idempotence and the activation-status update are preserved.
    """
    resp = await supabase.rpc(
        "create_target_and_link",
        {
            "p_user_id": user_id,
            "p_label": payload.label,
            "p_normalized_label": crud.normalize_label(payload.label),
            "p_activation_status": activation_status,
            "p_description": payload.description,
            "p_scoring_profile": payload.scoring_profile.model_dump(),
            "p_search_keywords": payload.search_keywords,
        },
    ).execute()
    data = cast(dict[str, Any] | None, resp.data)
    if not data or "target" not in data or "user_target" not in data:
        raise RuntimeError("create_target_and_link returned no target/user_target")
    return (
        crud._parse_target(cast(dict[str, Any], data["target"])),
        crud._parse_user_target(cast(dict[str, Any], data["user_target"])),
    )


async def _link(
    supabase: AsyncClient,
    *,
    user_id: str,
    target_id: str,
    is_active: bool = False,
    fit_score: int | None = None,
    fit_score_reasoning: str | None = None,
    fit_score_prose_doc_id: str | None = None,
) -> UserTarget:
    """Async inline of ``crud.link_user_to_target`` for the create-or-link path.

    Every from-input link is ``is_active=False`` (following a target never trips
    the active-target cap — activation is a separate step), so the cap-check
    branch of ``crud.link_user_to_target`` never runs here and this is a plain
    upsert. The active-cap read path stays in sync crud for the activate route.
    """
    row: dict[str, Any] = {
        "user_id": user_id,
        "target_id": target_id,
        "is_active": is_active,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    if fit_score is not None:
        row["fit_score"] = fit_score
    if fit_score_reasoning is not None:
        row["fit_score_reasoning"] = fit_score_reasoning
    if fit_score_prose_doc_id is not None:
        row["fit_score_prose_doc_id"] = fit_score_prose_doc_id
    resp = await (
        supabase.table(crud.USER_TARGETS_TABLE)
        .upsert(row, on_conflict="user_id,target_id")
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    if not rows:
        raise RuntimeError("Failed to upsert user_targets row")
    return crud._parse_user_target(rows[0])


async def _add_reference_jd(
    supabase: AsyncClient,
    *,
    target_id: str,
    jd_text: str,
    jd_url: str | None,
    extracted_profile: ScoringProfile,
    user_id: str | None = None,
) -> TargetReferenceJD:
    """Async inline of ``crud.add_reference_jd``."""
    row = {
        "target_id": target_id,
        "user_id": user_id,
        "jd_text": jd_text,
        "jd_url": jd_url,
        "extracted_profile": extracted_profile.model_dump(),
    }
    resp = await supabase.table(crud.REF_JDS_TABLE).insert(row).execute()
    rows = cast(list[dict[str, Any]], resp.data or [])
    if not rows:
        raise RuntimeError("Failed to insert reference_jds row")
    return crud._parse_ref_jd(rows[0])


async def _list_reference_jds(supabase: AsyncClient, target_id: str) -> list[TargetReferenceJD]:
    """Async inline of ``crud.list_reference_jds``."""
    resp = (
        await supabase.table(crud.REF_JDS_TABLE)
        .select("*")
        .eq("target_id", target_id)
        .order("created_at")
        .execute()
    )
    return [crud._parse_ref_jd(cast(dict[str, Any], r)) for r in (resp.data or [])]


async def _count_user_reference_jds(supabase: AsyncClient, *, target_id: str, user_id: str) -> int:
    """Async inline of ``crud.count_user_reference_jds`` (the #47 per-user cap)."""
    resp = (
        await supabase.table(crud.REF_JDS_TABLE)
        .select("id", count="exact", head=True)  # type: ignore[arg-type]
        .eq("target_id", target_id)
        .eq("user_id", user_id)
        .execute()
    )
    return resp.count or 0


# ---- Deferred fit-score helper ---------------------------------------------


async def _upsert_user_job_async(
    supabase: AsyncClient, *, user_id: str, job_posting_id: str, status: str
) -> None:
    """Async inline of ``persistence.upsert_user_job`` — the per-user pipeline
    status write (``user_jobs``). ``persistence.upsert_user_job`` stays sync for
    the not-yet-converted callers, so the deferred URL derive inlines the same
    upsert rather than fork a twin (#57 PR-G2e-4 — mirrors
    ``jobs._upsert_user_job_async``)."""
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


async def _apply_fit_score(
    supabase: AsyncClient,
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
    # ``resolve_current_payload`` now rides the pooled async service client
    # (#57 PR-G2e-5), so it takes ``supabase`` directly — the fit-score link + cost
    # write already ride it too.
    fresh, prose_doc_id = await resolve_current_payload(
        supabase, llm, cost_supabase=supabase, user_id=user_id
    )
    payload = fresh if fresh is not None else payload
    fit_result, llm_result = await derive_fit_score(llm, payload=payload, target=target)
    await cost_log.record_async(
        supabase,
        user_id=user_id,
        purpose=FIT_SCORE_PURPOSE,
        result=llm_result,
        metadata={"target_id": target.id, "user_id": user_id},
    )
    await _link(
        supabase,
        user_id=user_id,
        target_id=target.id,
        is_active=False,
        fit_score=fit_result.fit_score,
        fit_score_reasoning=fit_result.reasoning,
        fit_score_prose_doc_id=prose_doc_id,
    )


# ---- Background derivation tasks --------------------------------------------


async def derive_manual_target_bg(
    supabase: AsyncClient,
    llm: LLMClient,
    *,
    user_id: str,
    target_id: str,
    label: str,
    payload: OptimizedPayload | None,
) -> None:
    """Derive profile-from-label then fit score for a new manual target.

    Runs as a DETACHED loop task (``spawn_detached``). The target already
    exists in ``activation_status="deriving"``; on success it flips to
    ``idle`` with the derived scoring profile, on failure (or timeout) to
    ``error``.

    ``payload`` is ``None`` when the caller has no experience profile (the
    ``from_suggestion`` search-fallback path): the label-derived scoring
    profile is still produced, but the per-user fit score — which needs the
    experience payload — is skipped.
    """
    try:
        async with asyncio.timeout(DERIVATION_TIMEOUT_S):
            derived, derive_result = await derive_profile_from_label(llm, label=label)
            await cost_log.record_async(
                supabase,
                user_id=user_id,
                purpose=DERIVE_LABEL_PURPOSE,
                result=derive_result,
                metadata={"user_id": user_id, "label": label},
            )
            updated = await _update(
                supabase,
                target_id,
                TargetUpdate(
                    scoring_profile=derived.scoring_profile,
                    search_keywords=derived.search_keywords,
                    example_promising_titles=derived.example_promising_titles,
                    example_unpromising_titles=derived.example_unpromising_titles,
                    # Slim shape — populated when the LLM emits them; None is
                    # treated as "leave unchanged" by _update, so the
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
        await _update(
            supabase,
            target_id,
            TargetUpdate(
                activation_status="error", activation_error=ActivationError.DERIVE_TIMEOUT
            ),
        )
    except Exception:
        logger.exception("Deferred manual-target derivation failed for target %s", target_id)
        await _update(
            supabase,
            target_id,
            TargetUpdate(
                activation_status="error", activation_error=ActivationError.PIPELINE_FAILED
            ),
        )


async def _contribute_reference_jd(
    supabase: AsyncClient,
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

    Runs inside the deferred derivation task, so an over-cap contribution is
    logged and skipped (the caller still fit-scores against the current profile)
    rather than surfaced as an HTTP error.
    """
    # #47 per-user cap — bounds a single follower's footprint on the shared
    # profile. Over cap: skip the contribution entirely (do NOT touch the shared
    # profile).
    contributed = await _count_user_reference_jds(supabase, target_id=target_id, user_id=user_id)
    if contributed >= settings.reference_jd_max_per_user_per_target:
        logger.info(
            "URL corpus contribution skipped: user %s at reference-JD cap (%d) for target %s",
            user_id,
            settings.reference_jd_max_per_user_per_target,
            target_id,
        )
        return

    # Attributed to the contributing user so the merge can de-bias by contributor.
    await _add_reference_jd(
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
        current = await _get(supabase, target_id)
        if current is None:
            logger.error("Target %s vanished during URL corpus merge", target_id)
            return
        composite = merge_reference_jds(await _list_reference_jds(supabase, target_id))
        outcome, _new_version = await apply_profile_merge_rpc_async(
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
    supabase: AsyncClient,
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

    Runs as a DETACHED loop task for both the new-target and matched
    (corpus-building) URL flows. The shared-profile write goes through
    :func:`_contribute_reference_jd` so the corpus builder is held to the same
    per-user cap + contributor de-bias + version-checked RPC as
    ``POST /targets/{id}/reference-jds`` (SEC-1). On failure (or timeout) the
    target flips to ``error``.
    """
    # The whole derive → contribute → fit → materialize chain now rides the async
    # service client: ``derive_profile_from_jd`` takes it (async cache path),
    # ``materialize_and_score_job`` is async, and the ``user_jobs`` write goes
    # through the ``_upsert_user_job_async`` inline (#57 PR-G2e-4).
    try:
        async with asyncio.timeout(DERIVATION_TIMEOUT_S):
            derived, derive_result = await derive_profile_from_jd(
                llm, jd_text=jd_text, supabase=supabase
            )
            await cost_log.record_async(
                supabase,
                user_id=user_id,
                purpose=DERIVE_JD_PURPOSE,
                result=derive_result,
                metadata={"user_id": user_id, "jd_url": final_url},
            )

            await _contribute_reference_jd(
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
            await _update(supabase, target_id, TargetUpdate(activation_status="idle"))
            target = await _get(supabase, target_id)
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
                await _upsert_user_job_async(
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
        await _update(
            supabase,
            target_id,
            TargetUpdate(
                activation_status="error", activation_error=ActivationError.DERIVE_TIMEOUT
            ),
        )
    except Exception:
        logger.exception("Deferred URL-target derivation failed for target %s", target_id)
        await _update(
            supabase,
            target_id,
            TargetUpdate(
                activation_status="error", activation_error=ActivationError.PIPELINE_FAILED
            ),
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
    supabase: AsyncClient,
    llm: LLMClient,
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
    matched = await find_matching_target(supabase, suggestion.label)
    if matched is not None:
        link = await _link(supabase, user_id=user_id, target_id=matched.id, is_active=False)
        if payload is not None:
            spawn_detached(
                _apply_fit_score(supabase, llm, user_id=user_id, target=matched, payload=payload),
                name=f"fit-score-{matched.id}",
            )
        return CreateOrLinkResult(user_target=link, target=matched, was_matched=True)

    # New target: create immediately in "deriving" so it appears in the
    # list with a pending indicator while the background task derives the
    # scoring profile (+ fit score when we have a profile).
    target, link = await _create_and_link(
        supabase,
        user_id=user_id,
        payload=TargetCreate(
            label=suggestion.label,
            description=suggestion.description,
        ),
        activation_status="deriving",
    )
    spawn_detached(
        derive_manual_target_bg(
            supabase,
            llm,
            user_id=user_id,
            target_id=target.id,
            label=suggestion.label,
            payload=payload,
        ),
        name=f"derive-manual-{target.id}",
    )
    return CreateOrLinkResult(user_target=link, target=target, was_matched=False)


async def from_manual(
    supabase: AsyncClient,
    llm: LLMClient,
    *,
    user_id: str,
    label: str,
    description: str | None,
    payload: OptimizedPayload,
) -> CreateOrLinkResult:
    """Manual flow: user-typed title + description.

    Inline (fast): LLM-normalize the input, match against existing
    targets, and link the user. Deferred (detached task): derive the
    scoring profile (new targets) and the per-user fit score.

    1. LLM normalizes input into a canonical ``TargetSuggestion``
    2. Match against existing targets
    3. If matched, link the user; defer the fit score
    4. If new, create in ``deriving`` status, link, defer profile + fit score
    """
    suggestion, norm_result = await _normalize_suggestion(
        llm, label=label, description=description, payload=payload
    )
    await cost_log.record_async(
        supabase,
        user_id=user_id,
        purpose=NORMALIZE_PURPOSE,
        result=norm_result,
        metadata={"user_id": user_id, "raw_label": label},
    )
    return await _create_or_link_from_suggestion(
        supabase,
        llm,
        user_id=user_id,
        suggestion=suggestion,
        payload=payload,
    )


async def from_suggestion(
    supabase: AsyncClient,
    llm: LLMClient,
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
        user_id=user_id,
        suggestion=suggestion,
        payload=payload,
    )


def _schedule_url_bg_tasks(
    supabase: AsyncClient,
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
    """Schedule the two deferred halves of the from-url flow for ``target_id``.

    1. ``derive_url_target_bg`` — profile derivation, reference-JD contribution,
       fit score, and job materialization (async client).
    2. ``register_source_from_url`` — register the company's board as a pollable
       source (best-effort, capped) so we pull MORE jobs from it going forward.
       Now async (#57 PR-G2e-5), so it rides the same pooled ``supabase`` client.

    Both are spawned DETACHED (never starlette ``BackgroundTasks`` — the derive
    task fans out on the pooled async client, which deadlocks under uvloop
    there). Registration is scheduled AFTER derive so the user-visible
    deriving→ready flip isn't held behind the ATS probe. Shared by both the
    matched and newly-created branches so their scheduling can't drift.
    """
    spawn_detached(
        derive_url_target_bg(
            supabase,
            llm,
            user_id=user_id,
            target_id=target_id,
            jd_text=jd_text,
            final_url=final_url,
            extracted_title=extracted_title,
            company_name=company_name,
            location=location,
            salary_text=salary_text,
            payload=payload,
        ),
        name=f"derive-url-{target_id}",
    )
    spawn_detached(
        register_source_from_url(supabase, user_id=user_id, final_url=final_url),
        name=f"register-source-{target_id}",
    )


def _raw_url_label(extracted_title: str | None) -> str:
    """The pre-canonicalization label: strip, fall back, truncate."""
    return ((extracted_title or "").strip() or "Untitled Target")[:200]


async def _canonical_url_label(llm: LLMClient, extracted_title: str | None, jd_text: str) -> str:
    """Canonical role label for a posting, falling back to the raw title.

    Deliberately non-fatal. This step improves the NAME; it is not what makes
    the target work. A normalizer outage (provider 5xx, malformed JSON, schema
    violation, budget breaker) must not turn a working create-from-URL into a
    502 — the user would lose the whole flow to cosmetics. On any failure we
    keep today's behavior exactly: the raw posting title.
    """
    raw = _raw_url_label(extracted_title)
    try:
        normalized, _ = await normalize_posting_title(llm, title=raw, jd_text=jd_text)
    except Exception:
        logger.warning(
            "normalize_posting_title failed; falling back to the raw posting title",
            exc_info=True,
            extra={"raw_label": raw},
        )
        return raw

    canonical = normalized.label.strip()
    # An empty/whitespace label would produce a blank card and a useless dedup
    # key; the schema's min_length should prevent it, but the fallback is one
    # line and the failure mode is user-visible.
    return canonical[:200] if canonical else raw


async def from_url(
    supabase: AsyncClient,
    llm: LLMClient,
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
    shared catalog). The profile derivation + merge + fit score + job
    materialization are all deferred to a detached task.

    The raw posting title is CANONICALIZED first (one inline LLM call). A
    posting title sells one requisition at one company, so verbatim it yields
    targets like "Senior Product Builder (Product Manager), Enterprise
    Readiness & Admin Platform" — not a role profile, and unable to dedup,
    because ``crud.normalize_label`` keeps punctuation and comma-suffixes in
    the UNIQUE key. Canonicalizing has to precede ``find_matching_target``:
    the canonical form is what matches and what becomes the dedup key.
    """
    label = await _canonical_url_label(llm, extracted_title, jd_text)

    matched = await find_matching_target(supabase, label)
    if matched is not None:
        link = await _link(supabase, user_id=user_id, target_id=matched.id, is_active=False)
        _schedule_url_bg_tasks(
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
        return CreateOrLinkResult(user_target=link, target=matched, was_matched=True)

    target, link = await _create_and_link(
        supabase,
        user_id=user_id,
        payload=TargetCreate(label=label),
        activation_status="deriving",
    )
    _schedule_url_bg_tasks(
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
    return CreateOrLinkResult(user_target=link, target=target, was_matched=False)
