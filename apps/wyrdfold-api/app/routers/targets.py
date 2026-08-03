"""Targets router (#495).

CRUD for job targets + reference JD management. Adding a reference JD
triggers LLM-powered profile derivation and merges the result into the
target's composite scoring profile.
"""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, cast

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from postgrest.types import CountMethod
from pydantic import ValidationError
from supabase import AsyncClient, Client

from app.background import spawn_detached
from app.cache import job_list_cache, jobs_cache_prefix
from app.config import settings
from app.dependencies import (
    enforce_llm_budget,
    get_async_service_supabase,
    get_async_user_supabase,
    get_current_user_id,
    get_current_user_id_optional,
    get_llm_client,
    get_supabase,
    verify_api_key,
    verify_api_key_or_jwt,
)
from app.http_client import ResponseTooLargeError, UnsafeURLError, get_with_size_cap
from app.models.diagnostics import TargetFunnelResponse
from app.models.learning import ProfilePatch
from app.models.schemas import PollResult
from app.models.targets import (
    AxisWeights,
    ContributionVoteResult,
    CreateOrLinkResult,
    DeleteResponse,
    JobTarget,
    MatchedSuggestions,
    MyTargetsSummaryListResponse,
    NotificationThresholdsUpdate,
    ReferenceJDAdd,
    ReferenceJDsListResponse,
    ReferenceJDVote,
    ScoringProfile,
    SuggestFromQueryRequest,
    TargetCreate,
    TargetFromManual,
    TargetFromSuggestion,
    TargetFromUrl,
    TargetPreferences,
    TargetPreferencesUpdate,
    TargetReferenceJD,
    TargetSearchResponse,
    TargetSearchResult,
    TargetsListResponse,
    TargetStatusResponse,
    TargetUpdate,
    UserTarget,
    UserTargetWithSummary,
    UserTargetWithTarget,
)
from app.rate_limit import limiter
from app.services.diagnostics.funnel import compute_target_funnel
from app.services.experience import optimized
from app.services.experience.resolve import resolve_current_payload
from app.services.extract import (
    ExtractionResult,
    _extract_from_firecrawl,
    extract_job_from_html,
)
from app.services.llm import cost_log
from app.services.llm.client import LLMClient
from app.services.poller import poll_sources_for_target
from app.services.scoring import strip_html
from app.services.source_discovery import (
    DiscoveryRunStats,
    run_discovery_for_target,
)
from app.services.target_scoring import bulk_title_score_for_target
from app.services.targets import crud, fit_refresh, from_input, votes
from app.services.targets.derive_profile import DEFAULT_PURPOSE, derive_profile_from_jd
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
from app.services.targets.lateral_discovery import (
    DEFAULT_PURPOSE as LATERAL_PURPOSE,
)
from app.services.targets.lateral_discovery import (
    LateralSuggestions,
    suggest_lateral_targets,
)
from app.services.targets.learning_projection import project_profile_impact
from app.services.targets.match import (
    suggest_and_match,
    suggest_and_match_from_query,
)
from app.services.targets.merge import merge_reference_jds
from app.services.targets.profile_writes import (
    apply_profile_merge_rpc_async,
    apply_profile_patch_rpc,
    apply_profile_patch_rpc_async,
)
from app.services.targets.suggest import DEFAULT_PURPOSE as SUGGEST_PURPOSE
from app.services.targets.suggest import (
    QUERY_DEFAULT_PURPOSE as QUERY_SUGGEST_PURPOSE,
)
from app.services.validate import assert_safe_host, validate_job_url

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/targets",
    tags=["targets"],
    dependencies=[Depends(verify_api_key_or_jwt)],
)


# ---- Background pipeline for activation ------------------------------------
#
# Retro-scoring pre-existing jobs against a newly-activated target lives in
# ``target_scoring.bulk_title_score_for_target`` (keyset-paged bulk upsert +
# per-page global-score recompute) — the batch shape ``bulk_score_for_target``
# uses. It replaced a per-row-upsert N+1 over an unordered OFFSET scan here.


def _require_user_owns_target(supabase: Client, *, user_id: str | None, target_id: str) -> None:
    """Raise 404 if a JWT caller is not linked to ``target_id``.

    Targets are a shared resource keyed only by the explicit ``user_targets``
    junction — there is no RLS backstop because the API uses the service-role
    key. Without this guard any authenticated user could mutate any target.

    A ``None`` user_id is the operator/api-key path (``verify_api_key_or_jwt``)
    and bypasses the check — operators are trusted with full access.

    Returns 404 (not 403) so non-owners cannot enumerate target existence.
    """
    if user_id is None:
        return
    if target_id not in crud.get_user_target_ids(supabase, user_id):
        raise HTTPException(status_code=404, detail="Target not found")


def _require_operator(user_id: str | None) -> None:
    """Reject a signed-in end user from a shared-catalog mutation.

    Targets are a shared row (many users co-follow one target via the global
    ``find_matching_target`` label dedup), so a direct field edit here would
    rewrite the scoring rubric / label every co-follower depends on. Those
    edits are operator-only: ``user_id is None`` is the operator/api-key path
    (cron, admin curation) and passes; a real subject is a JWT end user and is
    refused. End users shape only their own view (axis weights, preferences on
    ``user_targets``) and contribute to the shared profile solely through the
    bounded #191 path (reference JDs, learner review). See SEC-2 (#366).
    """
    if user_id is not None:
        raise HTTPException(
            status_code=403,
            detail=(
                "Targets are a shared catalog and can't be edited directly. "
                "Tune your own view via axis weights and preferences, or "
                "contribute a reference JD."
            ),
        )


# ── Async inlines of shared crud reads (#57 slice 3) ─────────────────────────
# The interactive GET handlers below are now `async def` and hold the async
# client. ``crud.*`` stays SYNC for its many not-yet-converted callers — the
# poller, the activation / from_input background pipelines, target_scoring's
# poll twins, the discovery / learner services, and crud's own unit +
# RLS-integration tests. A sync helper can't take the async client, and
# converting crud would ripple into every one of those (the poller is
# explicitly out of scope for this slice). So these thin async inlines run the
# same queries on the async client (the routers/insights.py::_user_target_ids
# pattern); the row→model parse is reused from crud so the shape stays
# identical. No sync+async twin in the shared layer. The write handlers that
# fan out into crud's complex write helpers (link/update/axis-weights/…) stay
# sync `def` — FastAPI threadpools them, so their blocking round-trips never
# touch the loop (#107).


async def _user_target_ids(supabase: AsyncClient, user_id: str) -> set[str]:
    """Async inline of ``crud.get_user_target_ids`` — any-status memberships."""
    resp = (
        await supabase.table(crud.USER_TARGETS_TABLE)
        .select("target_id")
        .eq("user_id", user_id)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    return {r["target_id"] for r in rows}


async def _require_user_owns_target_async(
    supabase: AsyncClient, *, user_id: str | None, target_id: str
) -> None:
    """Async twin of :func:`_require_user_owns_target` (the sync version is kept
    for the sync / threadpooled handlers). 404s a JWT caller not linked to
    ``target_id``; the operator/api-key path (``user_id`` None) bypasses."""
    if user_id is None:
        return
    if target_id not in await _user_target_ids(supabase, user_id):
        raise HTTPException(status_code=404, detail="Target not found")


async def _target_get(supabase: AsyncClient, target_id: str) -> JobTarget | None:
    """Async inline of ``crud.get``. Reuses ``crud._parse_target`` for shape."""
    resp = await supabase.table(crud.TARGETS_TABLE).select("*").eq("id", target_id).execute()
    rows = cast(list[dict[str, Any]], resp.data or [])
    return crud._parse_target(rows[0]) if rows else None


async def _get_user_target(
    supabase: AsyncClient, user_id: str, target_id: str
) -> UserTarget | None:
    """Async inline of ``crud.get_user_target``."""
    resp = (
        await supabase.table(crud.USER_TARGETS_TABLE)
        .select("*")
        .eq("user_id", user_id)
        .eq("target_id", target_id)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    return crud._parse_user_target(rows[0]) if rows else None


async def _search_targets_by_label(
    supabase: AsyncClient, query: str, *, limit: int
) -> list[JobTarget]:
    """Async inline of ``crud.search_by_label`` — case-insensitive ``ILIKE``
    substring over the shared catalog. A blank / too-short query short-circuits
    without a DB hit (never dump the whole catalog)."""
    trimmed = query.strip()
    if len(trimmed) < 2:
        return []
    resp = (
        await supabase.table(crud.TARGETS_TABLE)
        .select("*")
        .ilike("label", f"%{trimmed}%")
        .order("label")
        .limit(limit)
        .execute()
    )
    return [crud._parse_target(cast(dict[str, Any], r)) for r in (resp.data or [])]


async def _active_targets_for_user(supabase: AsyncClient, user_id: str) -> list[JobTarget]:
    """Async inline of ``crud.get_active_for_user`` — the user's active memberships."""
    ut_resp = (
        await supabase.table(crud.USER_TARGETS_TABLE)
        .select("target_id")
        .eq("user_id", user_id)
        .eq("is_active", True)
        .execute()
    )
    ut_rows = cast(list[dict[str, Any]], ut_resp.data or [])
    target_ids = [r["target_id"] for r in ut_rows]
    if not target_ids:
        return []
    resp = await supabase.table(crud.TARGETS_TABLE).select("*").in_("id", target_ids).execute()
    return [crud._parse_target(cast(dict[str, Any], r)) for r in (resp.data or [])]


async def _active_targets(supabase: AsyncClient) -> list[JobTarget]:
    """Async inline of ``crud.get_active`` — the derived ``app_active OR
    EXISTS(active membership)`` pipeline predicate, two indexed reads deduped in
    Python (see the crud docstring for the dropped-trigger history)."""
    floor_resp = (
        await supabase.table(crud.TARGETS_TABLE).select("*").eq("app_active", True).execute()
    )
    member_ids_resp = (
        await supabase.table(crud.USER_TARGETS_TABLE)
        .select("target_id")
        .eq("is_active", True)
        .execute()
    )
    member_ids = {
        cast(str, r["target_id"])
        for r in cast(list[dict[str, Any]], member_ids_resp.data or [])
    }
    rows = cast(list[dict[str, Any]], floor_resp.data or [])
    seen = {cast(str, r["id"]) for r in rows}
    missing = sorted(member_ids - seen)
    if missing:
        member_resp = (
            await supabase.table(crud.TARGETS_TABLE).select("*").in_("id", missing).execute()
        )
        rows.extend(cast(list[dict[str, Any]], member_resp.data or []))
    return [crud._parse_target(r) for r in rows]


async def _list_reference_jds_async(
    supabase: AsyncClient, target_id: str
) -> list[TargetReferenceJD]:
    """Async inline of ``crud.list_reference_jds``."""
    resp = (
        await supabase.table(crud.REF_JDS_TABLE)
        .select("*")
        .eq("target_id", target_id)
        .order("created_at")
        .execute()
    )
    return [crud._parse_ref_jd(cast(dict[str, Any], r)) for r in (resp.data or [])]


async def _add_reference_jd_async(
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


async def _count_user_reference_jds_async(
    supabase: AsyncClient, *, target_id: str, user_id: str
) -> int:
    """Async inline of ``crud.count_user_reference_jds`` (the #47 per-user cap)."""
    resp = (
        await supabase.table(crud.REF_JDS_TABLE)
        .select("id", count=CountMethod.exact, head=True)
        .eq("target_id", target_id)
        .eq("user_id", user_id)
        .execute()
    )
    return resp.count or 0


async def _list_for_user_async(supabase: AsyncClient, user_id: str) -> list[JobTarget]:
    """Async inline of ``crud.list_for_user`` — all targets a user is linked to."""
    ut_resp = (
        await supabase.table(crud.USER_TARGETS_TABLE)
        .select("target_id")
        .eq("user_id", user_id)
        .execute()
    )
    ut_rows = cast(list[dict[str, Any]], ut_resp.data or [])
    target_ids = [r["target_id"] for r in ut_rows]
    if not target_ids:
        return []
    resp = (
        await supabase.table(crud.TARGETS_TABLE)
        .select("*")
        .in_("id", target_ids)
        .order("created_at", desc=True)
        .execute()
    )
    return [crud._parse_target(cast(dict[str, Any], r)) for r in (resp.data or [])]


async def _list_user_targets_with_summary_async(
    supabase: AsyncClient, user_id: str
) -> list[UserTargetWithSummary]:
    """Async inline of ``crud.list_user_targets_with_summary`` (#863 list projection)."""
    ut_resp = (
        await supabase.table(crud.USER_TARGETS_TABLE)
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .execute()
    )
    ut_rows = cast(list[dict[str, Any]], ut_resp.data or [])
    if not ut_rows:
        return []
    target_ids = [r["target_id"] for r in ut_rows]
    t_resp = await supabase.table(crud.TARGETS_TABLE).select("*").in_("id", target_ids).execute()
    summaries_by_id = {
        cast(dict[str, Any], r)["id"]: crud._summarize_target(cast(dict[str, Any], r))
        for r in (t_resp.data or [])
    }
    results: list[UserTargetWithSummary] = []
    for ut_row in ut_rows:
        summary = summaries_by_id.get(ut_row["target_id"])
        if summary is None:
            continue
        results.append(
            UserTargetWithSummary(
                user_target=crud._parse_user_target(ut_row),
                target=summary,
            )
        )
    return results


async def _get_job_posting_row(supabase: AsyncClient, posting_id: str) -> dict[str, Any] | None:
    """Read a job posting's title/description/url for /from-posting target creation."""
    resp = (
        await supabase.table("jobs")
        .select("id, title, description_html, absolute_url")
        .eq("id", posting_id)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    return rows[0] if rows else None


async def _suppress_reference_jd_async(supabase: AsyncClient, ref_jd_id: str) -> None:
    """Quarantine-at-birth a capped reference-JD contribution so every merge
    excludes it until an operator lifts it (#191 staged-merge review rail)."""
    await (
        supabase.table(crud.REF_JDS_TABLE)
        .update({"suppressed": True})
        .eq("id", ref_jd_id)
        .execute()
    )


async def _insert_learning_log_row_async(supabase: AsyncClient, row: dict[str, Any]) -> None:
    """Insert a staged ``target_learning_log`` row (a reviewable merge)."""
    await supabase.table("target_learning_log").insert(row).execute()


async def _get_user_target_preferences(
    supabase: AsyncClient, *, user_id: str, target_id: str
) -> TargetPreferences | None:
    """Async inline of ``crud.get_user_target_preferences`` — the single
    junction read, re-projected into the preference shape."""
    ut = await _get_user_target(supabase, user_id, target_id)
    if ut is None:
        return None
    return crud.preferences_from_user_target(ut)


async def _live_scored_job_count(supabase: AsyncClient, target_id: str) -> int:
    """Count of the target's live, non-excluded ``scores`` rows — the number the
    /targets/{id}/status card shows.

    Gate ``excluded=False`` AND ``job_is_live`` so this reflects what
    /jobs?target_id=... actually lists — the list hides both scorer-excluded
    rows (negative-keyword matches) and jobs that are no longer live
    (archived/purged). Counting only ``excluded=False`` over-reported badly: on
    the heaviest prod target the not-excluded count was 12,853 but only 2,808
    were live.

    ``head=True`` → PostgREST issues a HEAD, returning only the Content-Range
    count and *zero* rows. The ``job_is_live AND NOT excluded`` predicate matches
    the ``idx_scores_live_dedup`` partial index, so this stays an index-only
    count (no rows, no new index).
    """
    count_resp = (
        await supabase.table("scores")
        .select("id", count=CountMethod.exact, head=True)
        .eq("target_id", target_id)
        .eq("excluded", False)
        .eq("job_is_live", True)
        .execute()
    )
    return count_resp.count or 0


# ── Async inlines of shared crud WRITES (#57 slice 4) ────────────────────────
# The write handlers below are now `async def` and hold the async client. As
# with the slice-3 read inlines above, ``crud.*`` stays SYNC for its many
# not-yet-converted callers — the poller, the activation / from_input background
# pipelines, target_scoring's poll twins, the discovery / learner services, and
# crud's own unit + RLS-integration tests. A sync helper can't take the async
# client, and converting crud would ripple into every one of those (all out of
# this slice's scope). So these thin async inlines run the SAME writes on the
# async client; the row→model parse is reused from ``crud`` so the persisted
# shape stays byte-identical. No sync+async twin in the shared layer. Handlers
# that fan out into crud's complex write helpers still reachable only from the
# LLM/poller paths (link / activate / add_reference_jd / …) stay on the sync
# client + ``to_thread`` — their blocking round-trips already never touch the
# loop (#107), and their downstream services (poller, profile_writes, votes,
# match, resolve, source_discovery, cost_log) are out of this slice's scope.


async def _create_target_async(supabase: AsyncClient, payload: TargetCreate) -> JobTarget:
    """Async inline of ``crud.create`` — find-or-create on ``normalized_label``."""
    normalized = crud.normalize_label(payload.label)
    row: dict[str, Any] = {
        "label": payload.label,
        "description": payload.description,
        "normalized_label": normalized,
        "scoring_profile": payload.scoring_profile.model_dump(),
        "search_keywords": payload.search_keywords,
    }
    resp = await (
        supabase.table(crud.TARGETS_TABLE)
        .upsert(row, on_conflict="normalized_label", ignore_duplicates=True)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    if rows:
        return crud._parse_target(rows[0])
    # Conflict → the canonical row already existed; return it rather than 500.
    existing = await (
        supabase.table(crud.TARGETS_TABLE)
        .select("*")
        .eq("normalized_label", normalized)
        .limit(1)
        .execute()
    )
    existing_rows = cast(list[dict[str, Any]], existing.data or [])
    if existing_rows:
        return crud._parse_target(existing_rows[0])
    raise RuntimeError("Failed to insert or locate targets row")


async def _update_target_async(
    supabase: AsyncClient, target_id: str, payload: TargetUpdate
) -> JobTarget | None:
    """Async inline of ``crud.update`` — same partial field mapping."""
    updates: dict[str, Any] = {"updated_at": datetime.now(UTC).isoformat()}
    if payload.label is not None:
        updates["label"] = payload.label
        updates["normalized_label"] = crud.normalize_label(payload.label)
    if payload.description is not None:
        updates["description"] = payload.description
    if payload.scoring_profile is not None:
        updates["scoring_profile"] = payload.scoring_profile.model_dump()
    if payload.search_keywords is not None:
        updates["search_keywords"] = payload.search_keywords
    if payload.activation_status is not None:
        updates["activation_status"] = payload.activation_status
    if payload.app_active is not None:
        updates["app_active"] = payload.app_active
    if payload.profile_version is not None:
        updates["profile_version"] = payload.profile_version
    if payload.example_promising_titles is not None:
        updates["example_promising_titles"] = payload.example_promising_titles
    if payload.example_unpromising_titles is not None:
        updates["example_unpromising_titles"] = payload.example_unpromising_titles
    if payload.seniority_hint is not None:
        updates["seniority_hint"] = payload.seniority_hint
    if payload.domain_hints is not None:
        updates["domain_hints"] = payload.domain_hints
    if payload.role_family is not None:
        updates["role_family"] = payload.role_family
    resp = await supabase.table(crud.TARGETS_TABLE).update(updates).eq("id", target_id).execute()
    rows = cast(list[dict[str, Any]], resp.data or [])
    return crud._parse_target(rows[0]) if rows else None


async def _set_user_target_inactive_async(
    supabase: AsyncClient, user_id: str, target_id: str
) -> UserTarget | None:
    """Async inline of ``crud.set_user_target_inactive``."""
    resp = await (
        supabase.table(crud.USER_TARGETS_TABLE)
        .update({"is_active": False, "updated_at": datetime.now(UTC).isoformat()})
        .eq("user_id", user_id)
        .eq("target_id", target_id)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    return crud._parse_user_target(rows[0]) if rows else None


async def _unlink_user_from_target_async(
    supabase: AsyncClient, user_id: str, target_id: str
) -> bool:
    """Async inline of ``crud.unlink_user_from_target``."""
    resp = await (
        supabase.table(crud.USER_TARGETS_TABLE)
        .delete()
        .eq("user_id", user_id)
        .eq("target_id", target_id)
        .execute()
    )
    return bool(resp.data)


async def _delete_target_async(supabase: AsyncClient, target_id: str) -> bool:
    """Async inline of ``crud.delete`` — operator-only hard delete."""
    resp = await supabase.table(crud.TARGETS_TABLE).delete().eq("id", target_id).execute()
    return bool(resp.data)


async def _set_axis_weights_async(
    supabase: AsyncClient, *, user_id: str, target_id: str, weights: AxisWeights | None
) -> UserTarget | None:
    """Async inline of ``crud.set_user_target_axis_weights`` — snapshot prior into
    ``axis_weights_previous`` then write. ``weights=None`` resets to defaults."""
    current = await _get_user_target(supabase, user_id, target_id)
    if current is None:
        return None
    updates: dict[str, Any] = {
        "axis_weights": weights.model_dump() if weights is not None else None,
        "axis_weights_previous": (
            current.axis_weights.model_dump() if current.axis_weights is not None else None
        ),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    resp = await (
        supabase.table(crud.USER_TARGETS_TABLE)
        .update(updates)
        .eq("user_id", user_id)
        .eq("target_id", target_id)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    return crud._parse_user_target(rows[0]) if rows else None


async def _undo_axis_weights_async(
    supabase: AsyncClient, *, user_id: str, target_id: str
) -> UserTarget | None:
    """Async inline of ``crud.undo_user_target_axis_weights`` — swap current ↔ previous."""
    current = await _get_user_target(supabase, user_id, target_id)
    if current is None:
        return None
    new_current = current.axis_weights_previous
    new_previous = current.axis_weights
    updates: dict[str, Any] = {
        "axis_weights": new_current.model_dump() if new_current is not None else None,
        "axis_weights_previous": (new_previous.model_dump() if new_previous is not None else None),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    resp = await (
        supabase.table(crud.USER_TARGETS_TABLE)
        .update(updates)
        .eq("user_id", user_id)
        .eq("target_id", target_id)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    return crud._parse_user_target(rows[0]) if rows else None


async def _set_notification_thresholds_async(
    supabase: AsyncClient, *, user_id: str, target_id: str, thresholds: dict[str, int | None]
) -> UserTarget | None:
    """Async inline of ``crud.set_user_target_notification_thresholds`` — partial
    write of only the channels present in *thresholds*."""
    current = await _get_user_target(supabase, user_id, target_id)
    if current is None:
        return None
    updates: dict[str, Any] = {
        col: thresholds[col]
        for col in crud._NOTIFICATION_THRESHOLD_COLUMNS
        if col in thresholds
    }
    if not updates:
        return current
    updates["updated_at"] = datetime.now(UTC).isoformat()
    resp = await (
        supabase.table(crud.USER_TARGETS_TABLE)
        .update(updates)
        .eq("user_id", user_id)
        .eq("target_id", target_id)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    return crud._parse_user_target(rows[0]) if rows else None


async def _set_preferences_async(
    supabase: AsyncClient, *, user_id: str, target_id: str, preferences: TargetPreferences
) -> TargetPreferences | None:
    """Async inline of ``crud.set_user_target_preferences`` — full PUT replace of
    every preference column, re-projected off the updated row."""
    current = await _get_user_target(supabase, user_id, target_id)
    if current is None:
        return None
    payload = preferences.model_dump()
    updates: dict[str, Any] = {col: payload[col] for col in crud._PREFERENCE_COLUMNS}
    updates["updated_at"] = datetime.now(UTC).isoformat()
    resp = await (
        supabase.table(crud.USER_TARGETS_TABLE)
        .update(updates)
        .eq("user_id", user_id)
        .eq("target_id", target_id)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    if not rows:
        return None
    return crud.preferences_from_user_target(crud._parse_user_target(rows[0]))


async def _activate_pipeline(
    supabase: Client, llm: LLMClient, target: JobTarget, user_id: str
) -> None:
    """Derive profile (if needed) then poll jobs for a target. Runs as BackgroundTask.

    Genuinely async (awaits the LLM derive + the poller), so it stays
    ``async def`` and runs on the event loop. supabase-py is synchronous, so
    each blocking ``crud``/``optimized`` round-trip is offloaded via
    ``asyncio.to_thread`` to keep it off the loop. See #107.
    """
    target_id = target.id
    needs_derive = not target.search_keywords or not target.scoring_profile.categories

    try:
        if needs_derive:
            # Step 1: derive profile + search keywords from label
            await asyncio.to_thread(
                crud.update,
                supabase,
                target_id,
                TargetUpdate(activation_status="deriving"),
            )
            doc = await asyncio.to_thread(optimized.get_latest, supabase, user_id=user_id)
            if doc is None:
                logger.warning("No OptimizedDoc for target %s — skipping derive", target_id)
                await asyncio.to_thread(
                    crud.update,
                    supabase,
                    target_id,
                    TargetUpdate(activation_status="error"),
                )
                return

            derived, result = await derive_profile_from_label(llm, label=target.label)
            await asyncio.to_thread(
                cost_log.record,
                supabase,
                user_id=user_id,
                purpose=DERIVE_LABEL_PURPOSE,
                result=result,
                metadata={"target_id": target_id, "trigger": "activation"},
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
                    # Slim shape — populated when the LLM emits them (new
                    # prompt); silently dropped when None (legacy /
                    # cached old-prompt responses).
                    description=derived.description,
                    seniority_hint=derived.seniority_hint,
                    domain_hints=derived.domain_hints or None,
                ),
            )
            if updated is None:
                logger.error("Failed to update target %s after derive", target_id)
                return
            target = updated

        # Step 2: poll jobs using the target's search keywords
        await asyncio.to_thread(
            crud.update,
            supabase,
            target_id,
            TargetUpdate(activation_status="polling"),
        )
        poll_result = await poll_sources_for_target(supabase, target)
        logger.info(
            "Activation pipeline for target %s: %d sources, %d new jobs",
            target_id,
            poll_result.sources_polled,
            poll_result.new_jobs,
        )

        # Step 3: retro-score every existing posting against the new target.
        # The poller stage-1-scores jobs at insert time, so anything that
        # existed before this target activated had no scores row for it —
        # the /jobs page filtered by this target would otherwise be empty
        # even when there are plenty of matching titles already in the
        # database. ``score_title_and_upsert`` returns ``None`` (no row
        # written) when no keywords match, so this only creates rows where
        # the title actually scores against the new profile.
        retro_scored = await asyncio.to_thread(bulk_title_score_for_target, supabase, target)
        logger.info(
            "Activation pipeline for target %s: retro-scored %d existing jobs",
            target_id,
            retro_scored,
        )

        await asyncio.to_thread(
            crud.update,
            supabase,
            target_id,
            TargetUpdate(activation_status="ready"),
        )
    except Exception:
        logger.exception("Activation pipeline failed for target %s", target_id)
        await asyncio.to_thread(
            crud.update,
            supabase,
            target_id,
            TargetUpdate(activation_status="error"),
        )


# ---- Target CRUD -----------------------------------------------------------


# Async `def` (#57 slice 4): the write runs natively on the async client via the
# router-inline ``_create_target_async`` (crud stays sync for its bg/poller callers).
@router.post("", response_model=JobTarget, status_code=201)
async def create_target(
    body: TargetCreate,
    supabase: AsyncClient = Depends(get_async_service_supabase),
) -> JobTarget:
    return await _create_target_async(supabase, payload=body)


@router.post(
    "/from-manual",
    response_model=CreateOrLinkResult,
    status_code=201,
    dependencies=[Depends(enforce_llm_budget)],
)
@limiter.limit("5/minute")
async def create_target_from_manual(
    request: Request,
    body: TargetFromManual,
    supabase: AsyncClient = Depends(get_async_service_supabase),
    llm: LLMClient = Depends(get_llm_client),
    user_id: str = Depends(get_current_user_id),
) -> CreateOrLinkResult:
    """Create-or-link a target from user-typed title + description.

    LLM normalizes the input into a canonical ``TargetSuggestion`` and
    matches against existing targets — the only inline LLM call. The user
    is linked immediately and an optimistic ``CreateOrLinkResult`` returns
    right away; the scoring-profile derivation + fit score run in a detached
    task (new targets start in ``deriving`` status). The user always ends up
    with a ``user_targets`` row.
    """
    # ``optimized.get_latest`` is poller-shared + not-yet-async — read it on a
    # locally-obtained sync service client (jobs.py materialize pattern).
    doc = await asyncio.to_thread(optimized.get_latest, get_supabase(), user_id=user_id)
    if doc is None:
        # 422 (Unprocessable Entity): the route exists and the request is
        # well-formed, but a business precondition (an experience profile
        # to derive against) isn't met. 404 was misleading — it implied
        # the endpoint didn't exist, and the UI couldn't distinguish from
        # a genuine misrouted call.
        raise HTTPException(
            status_code=422,
            detail="No experience profile found — complete onboarding first",
        )
    return await from_input.from_manual(
        supabase,
        llm,
        user_id=user_id,
        label=body.label,
        description=body.description,
        payload=doc.payload,
    )


@router.post(
    "/from-suggestion",
    response_model=CreateOrLinkResult,
    status_code=201,
    dependencies=[Depends(enforce_llm_budget)],
)
@limiter.limit("6/minute")
async def create_target_from_suggestion(
    request: Request,
    body: TargetFromSuggestion,
    supabase: AsyncClient = Depends(get_async_service_supabase),
    llm: LLMClient = Depends(get_llm_client),
    user_id: str = Depends(get_current_user_id),
) -> CreateOrLinkResult:
    """Create-or-link a target from an AI search-suggestion the user picked.

    The completion of the catalog-search LLM fallback: the user searched a
    role, got AI suggestions from ``/targets/suggest-from-query``, and picked
    one. Its label is already canonical, so — unlike ``/from-manual`` — no
    inline normalization runs, and matching the shared catalog server-side
    dedups a stale client ``is_new`` (or a race) into a link instead of a
    duplicate row. Links ``is_active=False`` (never trips the active-target
    cap), defers the scoring-profile derivation, and returns immediately.

    PROFILE-INDEPENDENT (unlike ``/from-manual``): a user with no experience
    profile can still create a target from a role search — the query is the
    signal. Only the per-user fit score is deferred-and-skipped when absent.
    """
    doc = await asyncio.to_thread(optimized.get_latest, get_supabase(), user_id=user_id)
    payload = doc.payload if doc is not None else None
    return await from_input.from_suggestion(
        supabase,
        llm,
        user_id=user_id,
        label=body.label,
        description=body.description,
        payload=payload,
    )


@router.post(
    "/from-url",
    response_model=CreateOrLinkResult,
    status_code=201,
    dependencies=[Depends(enforce_llm_budget)],
)
@limiter.limit("5/minute")
async def create_target_from_url(
    request: Request,
    body: TargetFromUrl,
    supabase: AsyncClient = Depends(get_async_service_supabase),
    llm: LLMClient = Depends(get_llm_client),
    user_id: str = Depends(get_current_user_id),
) -> CreateOrLinkResult:
    """Create-or-link a target from a JD URL.

    Validates and fetches the URL, extracts title + JD text, then matches
    against existing targets by label (no LLM call inline). The user is
    linked immediately and an optimistic result returns; profile
    derivation, reference-JD corpus building, profile re-merge, and fit
    score all run in a detached task (new targets start in ``deriving``
    status). The user is always linked.
    """
    doc = await asyncio.to_thread(optimized.get_latest, get_supabase(), user_id=user_id)
    if doc is None:
        # Precondition (profile exists) not met — see /from-manual for rationale.
        raise HTTPException(
            status_code=422,
            detail="No experience profile found — complete onboarding first",
        )

    vr = await validate_job_url(body.jd_url)
    if not vr.is_valid:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid JD URL: {vr.rejection_reason}",
        )
    final_url = vr.final_url

    extraction = await _fetch_jd_from_url(final_url)

    return await from_input.from_url(
        supabase,
        llm,
        user_id=user_id,
        final_url=final_url,
        extracted_title=extraction.title,
        jd_text=extraction.description_html or "",
        company_name=extraction.company_name,
        location=extraction.location,
        salary_text=extraction.salary_text,
        payload=doc.payload,
    )


@router.post(
    "/suggest",
    response_model=MatchedSuggestions,
    dependencies=[Depends(enforce_llm_budget)],
)
@limiter.limit("3/minute")
async def suggest(
    request: Request,
    supabase: AsyncClient = Depends(get_async_service_supabase),
    llm: LLMClient = Depends(get_llm_client),
    user_id: str = Depends(get_current_user_id),
) -> MatchedSuggestions:
    """Suggest 2-3 targets, matched against existing targets.

    Returns each suggestion paired with its matched target (if one exists)
    or flagged as new. Excludes targets the user already has.
    """
    doc = await asyncio.to_thread(optimized.get_latest, get_supabase(), user_id=user_id)
    if doc is None:
        # Precondition (profile exists) not met — see /from-manual for rationale.
        raise HTTPException(status_code=422, detail="No experience profile found")

    matched, result = await suggest_and_match(supabase, llm, payload=doc.payload, user_id=user_id)
    await cost_log.record_async(
        supabase,
        user_id=user_id,
        purpose=SUGGEST_PURPOSE,
        result=result,
        metadata={"user_id": user_id},
    )
    return matched


@router.post(
    "/suggest-lateral",
    response_model=LateralSuggestions,
    dependencies=[Depends(enforce_llm_budget)],
)
@limiter.limit("3/minute")
async def suggest_lateral(
    request: Request,
    supabase: AsyncClient = Depends(get_async_service_supabase),
    llm: LLMClient = Depends(get_llm_client),
    user_id: str = Depends(get_current_user_id),
) -> LateralSuggestions:
    """Mine the master payload for adjacent target roles.

    Distinct from ``POST /targets/suggest`` (the onboarding flow): this
    one returns LATERAL siblings of targets the user is ALREADY
    pursuing. Spans industries, includes at least one career-stretch.
    See ``services.targets.lateral_discovery.suggest_lateral_targets``.

    Each suggestion is a slim-shape-compatible label + reasoning +
    confidence; the activation flow plugs them into
    ``derive_profile_from_label`` to materialise the full target.
    """
    doc = await asyncio.to_thread(optimized.get_latest, get_supabase(), user_id=user_id)
    if doc is None:
        raise HTTPException(status_code=422, detail="No experience profile found")

    # Use the user's CURRENT active targets as the exclusion list so we
    # don't re-suggest what they already have. list_for_user is
    # user-scoped (via user_targets junction), not the global active
    # list — exactly what we want for personalised suggestions.
    current = await _list_for_user_async(supabase, user_id)

    try:
        suggestions, result = await suggest_lateral_targets(
            llm, payload=doc.payload, current_targets=current
        )
    except ValidationError:
        # The prose-cap overflows are truncated in-schema now; anything
        # still failing here is a structurally malformed model response
        # (wrong types, missing fields). That's an upstream hiccup, not a
        # server bug — tell the client to retry instead of 500ing.
        logger.warning("suggest-lateral: model returned malformed suggestions", exc_info=True)
        raise HTTPException(
            status_code=502,
            detail="The model returned malformed suggestions — please retry.",
        ) from None
    await cost_log.record_async(
        supabase,
        user_id=user_id,
        purpose=LATERAL_PURPOSE,
        result=result,
        metadata={
            "user_id": user_id,
            "current_targets_count": len(current),
            "suggestions_count": len(suggestions.suggestions),
        },
    )
    return suggestions


@router.post(
    "/suggest-from-query",
    response_model=MatchedSuggestions,
    dependencies=[Depends(enforce_llm_budget)],
)
@limiter.limit("6/minute")
async def suggest_from_query(
    request: Request,
    body: SuggestFromQueryRequest,
    supabase: AsyncClient = Depends(get_async_service_supabase),
    llm: LLMClient = Depends(get_llm_client),
    user_id: str = Depends(get_current_user_id),
) -> MatchedSuggestions:
    """Turn a free-text role search into selectable target suggestions.

    Catalog search (``GET /targets/search``) only finds targets that already
    exist; when it comes up empty the UI falls back here. The LLM canonicalises
    the query into a target plus a few adjacent roles, each matched against the
    shared catalog so the user can follow an existing one or create a new one.

    Tailored to the user's experience when they have a profile, but works
    without one (the query is the primary signal) — unlike ``/suggest``, which
    requires a profile. Rate-limited a touch higher than ``/suggest`` (6/min)
    since it's an interactive search affordance, and LLM-budget-gated.
    """
    doc = await asyncio.to_thread(optimized.get_latest, get_supabase(), user_id=user_id)
    payload = doc.payload if doc is not None else None
    try:
        matched, result = await suggest_and_match_from_query(
            supabase, llm, query=body.query, user_id=user_id, payload=payload
        )
    except ValidationError:
        # Structurally malformed model response (wrong types/missing fields).
        # An upstream hiccup, not a server bug — tell the client to retry.
        logger.warning("suggest-from-query: model returned malformed suggestions", exc_info=True)
        raise HTTPException(
            status_code=502,
            detail="The model returned malformed suggestions — please retry.",
        ) from None
    await cost_log.record_async(
        supabase,
        user_id=user_id,
        purpose=QUERY_SUGGEST_PURPOSE,
        result=result,
        metadata={"user_id": user_id, "query_len": len(body.query)},
    )
    return matched


@router.get("/active", response_model=TargetsListResponse)
async def get_active_targets(
    supabase: AsyncClient = Depends(get_async_service_supabase),
    user_id: str | None = Depends(get_current_user_id_optional),
) -> TargetsListResponse:
    """Active targets for the caller.

    JWT callers get only their own active targets — the bare
    ``crud.get_active`` returns every PIPELINE-ACTIVE target (the
    instance's ``app_active`` floor OR any user's active membership,
    derived at read time), which would leak every other user's roles +
    scoring profiles to any authenticated caller. api-key /
    operator callers (user_id is None) keep the instance-wide view for
    tooling. Mirrors the scoping the rest of the targets router applies.
    """
    targets = (
        await _active_targets_for_user(supabase, user_id)
        if user_id is not None
        else await _active_targets(supabase)
    )
    return TargetsListResponse(targets=targets)


@router.get("/mine", response_model=MyTargetsSummaryListResponse)
async def get_my_targets(
    background_tasks: BackgroundTasks,
    supabase: AsyncClient = Depends(get_async_service_supabase),
    llm: LLMClient = Depends(get_llm_client),
    user_id: str = Depends(get_current_user_id),
) -> MyTargetsSummaryListResponse:
    """Return the current user's linked targets with fit scores.

    List projection (#863): the heavy ``scoring_profile`` JSONB is omitted;
    the full target is available via ``GET /targets/{id}``.

    Lazy fit-score refresh (E2): any cached score computed against an older
    profile version is recomputed in the background (capped), so a profile edit
    eventually re-scores existing targets too — not just new ones. The response
    returns the current cached scores immediately; fresh ones land by the next
    view.
    """
    items = await _list_user_targets_with_summary_async(supabase, user_id)

    # The staleness scan + refresh run ENTIRELY in the background — /mine adds no
    # queries of its own to the response path. Only schedule when the user has
    # targets (nothing to refresh otherwise); the task itself no-ops immediately
    # if the LLM provider is fatal (credits out), so an outage can't make this
    # churn the DB on every view. ``refresh_stale_for_user`` is not-yet-async and
    # takes the sync service client (obtained locally); it does no async-pool
    # fan-out, so it stays a safe starlette ``BackgroundTask``.
    if items:
        background_tasks.add_task(
            fit_refresh.refresh_stale_for_user, get_supabase(), llm, user_id=user_id
        )
    return MyTargetsSummaryListResponse(targets=items)


# Declared before ``/{target_id}`` so "search" isn't captured as a target id.
@router.get("/search", response_model=TargetSearchResponse)
async def search_targets(
    q: str = Query(..., min_length=1, max_length=200),
    limit: int = Query(20, ge=1, le=50),
    supabase: AsyncClient = Depends(get_async_service_supabase),
    user_id: str = Depends(get_current_user_id),
) -> TargetSearchResponse:
    """Search the shared catalog by label so the caller can follow an existing
    target instead of minting a duplicate.

    JWT-only. Targets are shared, so any user's target is discoverable — but
    each result is marked ``is_linked`` for the ones the caller already follows.
    The heavy ``scoring_profile`` is omitted (see ``TargetSearchResult``); the
    full row is served by ``GET /targets/{id}`` once followed. A blank / 1-char
    query returns no results rather than dumping the catalog.
    """
    matches = await _search_targets_by_label(supabase, q, limit=limit)
    linked_ids = await _user_target_ids(supabase, user_id) if matches else set()
    results = [
        TargetSearchResult(
            id=t.id,
            label=t.label,
            description=t.description,
            is_linked=t.id in linked_ids,
        )
        for t in matches
    ]
    return TargetSearchResponse(results=results)


@router.get("/{target_id}", response_model=JobTarget)
async def get_target(
    target_id: str,
    supabase: AsyncClient = Depends(get_async_service_supabase),
    user_id: str | None = Depends(get_current_user_id_optional),
) -> JobTarget:
    # Targets are shared, service-role-read (no RLS backstop). Without this
    # guard any authenticated user could read any target's full JD text and
    # scoring profile by id (audit #29 round 3 / M3). Operators (api-key,
    # user_id None) bypass; non-owners get 404 so they can't enumerate
    # target existence.
    await _require_user_owns_target_async(supabase, user_id=user_id, target_id=target_id)
    target = await _target_get(supabase, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Target not found")
    return target


@router.get("/{target_id}/user-target", response_model=UserTargetWithTarget)
async def get_my_user_target(
    target_id: str,
    supabase: AsyncClient = Depends(get_async_service_supabase),
    user_id: str = Depends(get_current_user_id),
) -> UserTargetWithTarget:
    """Return the current user's user_targets row for a specific target,
    paired with the shared target data.

    Saves the FE from fetching the entire ``/mine`` list just to find one
    row when rendering a per-target settings page. JWT-only (no api-key
    fallback) because there is no "current user" without a JWT.
    """
    ut = await _get_user_target(supabase, user_id, target_id)
    if ut is None:
        raise HTTPException(
            status_code=404,
            detail="No user_targets row for (user, target).",
        )
    target = await _target_get(supabase, target_id)
    if target is None:
        # user_targets row exists but the shared target was deleted —
        # data integrity issue, surface as a 404 not a 500.
        raise HTTPException(status_code=404, detail="Target not found")
    return UserTargetWithTarget(user_target=ut, target=target)


@router.patch("/{target_id}", response_model=JobTarget)
async def update_target(
    target_id: str,
    body: TargetUpdate,
    supabase: AsyncClient = Depends(get_async_service_supabase),
    user_id: str | None = Depends(get_current_user_id_optional),
) -> JobTarget:
    # Shared-catalog write → operator-only (SEC-2, #366). End users can't edit
    # the shared scoring profile / label; they tune their own view via
    # axis-weights / preferences and contribute via the bounded #191 path.
    _require_operator(user_id)
    target = await _update_target_async(supabase, target_id, body)
    if target is None:
        raise HTTPException(status_code=404, detail="Target not found")
    return target


# Async: the blocking supabase reads/link are offloaded via ``to_thread``
# (#107), and the LLM/poll pipeline is spawned as a DETACHED task on the
# loop — not a starlette BackgroundTask, whose machinery deadlocks the
# pooled async DB client under uvloop once POLLER_ASYNC_DB routes the
# pipeline's poll fan-out through it (see ``app/background.py``).
@router.post(
    "/{target_id}/activate",
    response_model=JobTarget,
    dependencies=[Depends(enforce_llm_budget)],
)
async def activate_target(
    target_id: str,
    supabase: Client = Depends(get_supabase),
    llm: LLMClient = Depends(get_llm_client),
    user_id: str = Depends(get_current_user_id),
) -> JobTarget:
    target = await asyncio.to_thread(crud.get, supabase, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Target not found")

    try:
        await asyncio.to_thread(
            crud.link_user_to_target,
            supabase,
            user_id=user_id,
            target_id=target_id,
            is_active=True,
        )
    except crud.ActiveTargetLimitError as e:
        # 409 Conflict — the request was well-formed but conflicts with
        # current state (the user is already at the active-target cap).
        # Frontend reads ``error`` to switch on this case specifically and
        # offers a deactivate picker rather than a generic toast.
        raise HTTPException(
            status_code=409,
            detail={
                "error": "ACTIVE_LIMIT",
                "limit": e.limit,
                "active_count": e.current_count,
                "message": (
                    f"You already have {e.current_count} active targets "
                    f"(limit {e.limit}) — deactivate one first."
                ),
            },
        ) from e
    refreshed = await asyncio.to_thread(crud.get, supabase, target_id) or target

    spawn_detached(
        _activate_pipeline(supabase, llm, refreshed, user_id),
        name=f"activate-target-{target_id}",
    )
    return refreshed


@router.post(
    "/{target_id}/discover-sources",
    response_model=DiscoveryRunStats,
)
@limiter.limit("2/minute;20/day")
async def discover_sources_for_target_endpoint(
    request: Request,
    target_id: str,
    supabase: Client = Depends(get_supabase),
    user_id: str = Depends(get_current_user_id),
) -> DiscoveryRunStats:
    """Trigger Brave-Search-driven source discovery for one target.

    Synchronous on the request (no BackgroundTask) so the caller gets the
    actual stats back in the response. A full run for a target with 15
    keywords × 6 ATS site filters issues up to 90 Brave queries plus one
    detect_ats call per result URL; in practice it takes 30–90 seconds.
    Acceptable for an operator-triggered endpoint — when we wire this to
    cron, we'll move it to a BackgroundTask path.

    Caller must own the target (JWT path enforces user_id; the read-by-id
    confirms the target exists). Cron-style bulk discovery across all
    active targets isn't exposed here yet — separate follow-up PR.
    """
    # Verify the caller is linked to this target. Discovery is target-driven
    # so it's only meaningful for the user's own targets — and we don't want
    # to let a JWT caller burn the global Brave quota on someone else's
    # search keywords.
    await asyncio.to_thread(
        _require_user_owns_target, supabase, user_id=user_id, target_id=target_id
    )
    target = await asyncio.to_thread(crud.get, supabase, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Target not found")

    return await run_discovery_for_target(supabase, target)


# Async `def` (#57 slice 4): reads/writes run natively on the async client via
# the router-inline helpers (crud stays sync for its bg/poller callers).
@router.post("/{target_id}/deactivate", response_model=JobTarget)
async def deactivate_target(
    target_id: str,
    supabase: AsyncClient = Depends(get_async_service_supabase),
    user_id: str = Depends(get_current_user_id),
) -> JobTarget:
    # Ownership gate (#29 R3 / SEC-4): every sibling route carries this, but
    # deactivate was missing it — without the check any JWT user could POST a
    # target id they don't follow and read back the shared row's full metadata
    # (scoring_profile / search_keywords / example titles) on the service-role
    # client. set_user_target_inactive is already caller-scoped (a harmless
    # no-op for a non-follower), but the response still leaked the row.
    await _require_user_owns_target_async(supabase, user_id=user_id, target_id=target_id)
    target = await _target_get(supabase, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Target not found")

    await _set_user_target_inactive_async(supabase, user_id=user_id, target_id=target_id)
    return await _target_get(supabase, target_id) or target


# ---- Axis weights (PR E follow-up) ----------------------------------------
#
# Per-(user, target) read-time multipliers for the four Phase 2 axes.
# See plan-wyrdfold-streamlined-target.md "User-tunable axis weights".
#
# - PATCH /targets/{id}/axis-weights — set new weights; snapshots prior
#   into axis_weights_previous so /undo can revert in one click.
# - POST  /targets/{id}/axis-weights/undo — swap current ↔ previous.
# - DELETE /targets/{id}/axis-weights — reset to defaults (NULL); also
#   snapshots, so undo recovers the prior custom weights.


# Async `def` (#57 slice 4): the write runs natively on the async user client via
# the router-inline helper (crud stays sync for its bg/poller callers).
@router.patch("/{target_id}/axis-weights", response_model=UserTarget)
async def set_axis_weights(
    target_id: str,
    weights: AxisWeights,
    # #88 Phase 2: RLS client — writes only the caller's user_targets row
    # (full CRUD self-policy), so RLS backstops the helper's user_id filter.
    supabase: AsyncClient = Depends(get_async_user_supabase),
    user_id: str = Depends(get_current_user_id),
) -> UserTarget:
    """Set the user's per-target axis weights.

    Snapshots the prior value into ``axis_weights_previous`` so the
    /undo endpoint can revert. Pure read-time math — does NOT trigger
    any re-grade; existing scores rows are unchanged.
    """
    updated = await _set_axis_weights_async(
        supabase,
        user_id=user_id,
        target_id=target_id,
        weights=weights,
    )
    if updated is None:
        raise HTTPException(
            status_code=404,
            detail="No user_targets row for (user, target). Link the target first.",
        )
    _invalidate_jobs_cache_for_target(target_id)
    return updated


# Async `def` (#57 slice 4): runs natively on the async user client.
@router.delete("/{target_id}/axis-weights", response_model=UserTarget)
async def reset_axis_weights(
    target_id: str,
    supabase: AsyncClient = Depends(get_async_user_supabase),  # #88 Phase 2: user_targets only
    user_id: str = Depends(get_current_user_id),
) -> UserTarget:
    """Reset axis_weights to defaults (NULL — equal quartile).

    Same snapshot behaviour as PATCH: the prior custom weights move to
    ``axis_weights_previous`` so /undo can put them back. "Reset" and
    "undo" cancel each other out in one round-trip.
    """
    updated = await _set_axis_weights_async(
        supabase,
        user_id=user_id,
        target_id=target_id,
        weights=None,
    )
    if updated is None:
        raise HTTPException(
            status_code=404,
            detail="No user_targets row for (user, target). Link the target first.",
        )
    _invalidate_jobs_cache_for_target(target_id)
    return updated


# Async `def` (#57 slice 4): runs natively on the async user client.
@router.post("/{target_id}/axis-weights/undo", response_model=UserTarget)
async def undo_axis_weights(
    target_id: str,
    supabase: AsyncClient = Depends(get_async_user_supabase),  # #88 Phase 2: user_targets only
    user_id: str = Depends(get_current_user_id),
) -> UserTarget:
    """Swap ``axis_weights`` and ``axis_weights_previous``.

    The button-press behind the safety-design's "Undo last change". Two
    consecutive undos toggle back and forth — that's the intended
    contract (undo, then change-my-mind-and-redo).
    """
    updated = await _undo_axis_weights_async(supabase, user_id=user_id, target_id=target_id)
    if updated is None:
        raise HTTPException(
            status_code=404,
            detail="No user_targets row for (user, target).",
        )
    _invalidate_jobs_cache_for_target(target_id)
    return updated


# Async `def` (#57 slice 4): runs natively on the async user client.
@router.patch("/{target_id}/notification-thresholds", response_model=UserTarget)
async def set_notification_thresholds(
    target_id: str,
    body: NotificationThresholdsUpdate,
    supabase: AsyncClient = Depends(get_async_user_supabase),  # #88 Phase 2: user_targets only
    user_id: str = Depends(get_current_user_id),
) -> UserTarget:
    """Set this target's per-channel email/SMS score thresholds (#15).

    Partial PATCH: only the channels included in the request body are
    written, so editing one never clobbers the other. An explicit ``null``
    resets that channel to the user-profile default (``notify.py`` reads
    target → profile fallback); an omitted channel is left untouched.
    Thresholds only gate which *new* matches alert; stored scores and the
    jobs list are unaffected, so no cache invalidation is needed.
    """
    updated = await _set_notification_thresholds_async(
        supabase,
        user_id=user_id,
        target_id=target_id,
        thresholds=cast(dict[str, int | None], body.model_dump(exclude_unset=True)),
    )
    if updated is None:
        raise HTTPException(
            status_code=404,
            detail="No user_targets row for (user, target). Link the target first.",
        )
    return updated


def _invalidate_jobs_cache_for_target(target_id: str) -> None:
    """Bust the jobs list cache for this target plus the untargeted view.

    The untargeted view ("global") merges scores across the user's targets,
    so a weight change on any one target can shift its displayed scores.
    Sibling targets are untouched.
    """
    job_list_cache.invalidate(prefix=jobs_cache_prefix(target_id=target_id))
    job_list_cache.invalidate(prefix=jobs_cache_prefix(target_id=None))


# ---- Per-user target preferences (#60) ------------------------------------
#
# Read-time filter/re-rank over the SHARED, cached fit score. Preferences live
# on the user_targets junction and NEVER trigger a re-grade or per-user
# scoring. JWT-only (no api-key fallback) — there is no "current user" without
# a JWT, and ownership is enforced exactly like the axis-weights /
# notification-thresholds routes: the crud helpers 404 via None when the
# (user, target) link is missing, and (#88 Phase 2) the RLS-bound user client
# means Postgres itself refuses to surface or mutate another user's row.


@router.get("/{target_id}/preferences", response_model=TargetPreferences)
async def get_target_preferences(
    target_id: str,
    # #88 Phase 2: RLS user client — reads only the caller's user_targets row.
    supabase: AsyncClient = Depends(get_async_user_supabase),
    user_id: str = Depends(get_current_user_id),
) -> TargetPreferences:
    """Return the calling user's preferences for a target.

    404s when the user has no link to the target — preferences are
    meaningless without a (user, target) pairing, and a 404 keeps a
    non-owner from confirming the target exists.
    """
    prefs = await _get_user_target_preferences(supabase, user_id=user_id, target_id=target_id)
    if prefs is None:
        raise HTTPException(
            status_code=404,
            detail="No user_targets row for (user, target). Link the target first.",
        )
    return prefs


# Async `def` (#57 slice 4): runs natively on the async user client.
@router.put("/{target_id}/preferences", response_model=TargetPreferences)
async def set_target_preferences(
    target_id: str,
    body: TargetPreferencesUpdate,
    supabase: AsyncClient = Depends(get_async_user_supabase),  # #88 Phase 2: user_targets only
    user_id: str = Depends(get_current_user_id),
) -> TargetPreferences:
    """Replace the calling user's preferences for a target (PUT semantics).

    Every preference column is written from the body (omitted fields fall
    back to their documented defaults), so the stored row is always a
    complete set. Pure read-time config — does NOT re-grade; existing
    ``scores`` rows are untouched. The jobs-list cache is busted so the
    next list read reflects the new filters.
    """
    prefs = await _set_preferences_async(
        supabase,
        user_id=user_id,
        target_id=target_id,
        preferences=TargetPreferences(**body.model_dump()),
    )
    if prefs is None:
        raise HTTPException(
            status_code=404,
            detail="No user_targets row for (user, target). Link the target first.",
        )
    _invalidate_jobs_cache_for_target(target_id)
    return prefs


@router.post(
    "/{target_id}/link",
    response_model=UserTarget,
    status_code=201,
    dependencies=[Depends(enforce_llm_budget)],
)
# #191: linking is the gateway to the shared-target contribution surface
# (a link is what authorizes reference-JD adds, votes, and learner writes
# on that target) — throttle it like add_reference_jd.
@limiter.limit("10/minute")
async def link_target(
    request: Request,
    target_id: str,
    supabase: AsyncClient = Depends(get_async_service_supabase),
    llm: LLMClient = Depends(get_llm_client),
    user_id: str = Depends(get_current_user_id),
) -> UserTarget:
    """Link the current user to a target and derive a fit score."""
    target = await _target_get(supabase, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Target not found")

    # ``resolve_current_payload`` and ``crud.link_user_to_target`` (active-cap
    # read path) are not-yet-async and take the sync service client — obtain one
    # locally (jobs.py materialize pattern); the fit-score cost write rides the
    # async client.
    svc = get_supabase()
    # Derive fit score if we have an experience profile
    fit_score: int | None = None
    fit_reasoning: str | None = None
    # Fresh vs. the current master document, not a possibly-stale persisted
    # optimized doc (BUG 2, the stale-payload seam) — a profile edited just
    # before this link must affect the fit.
    payload, prose_doc_id = await resolve_current_payload(
        svc, llm, cost_supabase=svc, user_id=user_id
    )
    if payload is not None:
        fit_result, result = await derive_fit_score(llm, payload=payload, target=target)
        await cost_log.record_async(
            supabase,
            user_id=user_id,
            purpose=FIT_SCORE_PURPOSE,
            result=result,
            metadata={"target_id": target_id, "user_id": user_id},
        )
        fit_score = fit_result.fit_score
        fit_reasoning = fit_result.reasoning

    try:
        return await asyncio.to_thread(
            crud.link_user_to_target,
            svc,
            user_id=user_id,
            target_id=target_id,
            fit_score=fit_score,
            fit_score_reasoning=fit_reasoning,
            fit_score_prose_doc_id=prose_doc_id,
        )
    except crud.ActiveTargetLimitError as e:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "ACTIVE_LIMIT",
                "limit": e.limit,
                "active_count": e.current_count,
                "message": (
                    f"You already have {e.current_count} active targets "
                    f"(limit {e.limit}) — deactivate one first."
                ),
            },
        ) from e


@router.post(
    "/{target_id}/poll-jobs",
    response_model=PollResult,
    dependencies=[Depends(verify_api_key)],
)
async def poll_jobs_for_target(
    target_id: str,
    supabase: Client = Depends(get_supabase),
) -> PollResult:
    """Poll all job sources, filtering for jobs matching this target's search keywords.

    Admin / operator-only: gated by ``verify_api_key`` so an
    unauthenticated caller can't trigger a fan-out poll across all
    configured ATS sources. Not reachable from the wyrdfold FE.
    """
    target = await asyncio.to_thread(crud.get, supabase, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Target not found")
    if not target.search_keywords:
        raise HTTPException(
            status_code=422,
            detail="Target has no search keywords — derive a profile first",
        )
    return await poll_sources_for_target(supabase, target)


@router.get("/{target_id}/status", response_model=TargetStatusResponse)
async def get_target_status(
    target_id: str,
    supabase: AsyncClient = Depends(get_async_service_supabase),
    user_id: str | None = Depends(get_current_user_id_optional),
) -> TargetStatusResponse:
    """Return activation status and job count for a target."""
    # Without this, any authenticated user could read any target's
    # activation status + scored-job count by id (audit #29 round 3 / M2).
    # Operators (user_id None) bypass; non-owners get 404.
    await _require_user_owns_target_async(supabase, user_id=user_id, target_id=target_id)
    target = await _target_get(supabase, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Target not found")

    jobs_count = await _live_scored_job_count(supabase, target_id)

    return TargetStatusResponse(
        activation_status=target.activation_status,
        jobs_count=jobs_count,
    )


# Sync `def`: the funnel report is blocking supabase work; the threadpool
# keeps it off the event loop (#107).
@router.get(
    "/{target_id}/funnel",
    response_model=TargetFunnelResponse,
    dependencies=[Depends(verify_api_key)],
)
def get_target_funnel(
    target_id: str,
    supabase: Client = Depends(get_supabase),
) -> TargetFunnelResponse:
    """Diagnostic funnel report for one target (#845).

    API-key-only — the response includes per-user list floors and the
    target's scoring profile, which is operator-facing data, not user
    surface. Read-only.
    """
    try:
        return compute_target_funnel(supabase, target_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# Async `def` (#57 slice 4): ownership-check + delete run natively on the async
# client via the router-inline helpers (crud stays sync for its bg/poller callers).
@router.delete("/{target_id}", response_model=DeleteResponse)
async def delete_target(
    target_id: str,
    supabase: AsyncClient = Depends(get_async_service_supabase),
    user_id: str | None = Depends(get_current_user_id_optional),
) -> DeleteResponse:
    await _require_user_owns_target_async(supabase, user_id=user_id, target_id=target_id)
    # Targets are a SHARED catalog — find_matching_target dedups by normalized
    # label across users, so distinct users routinely co-follow one row. A JWT
    # caller "deleting" their target must therefore only drop THEIR link: hard-
    # deleting the shared row cascades away every co-follower's user_targets /
    # scores / feedback / analyses / learning (all those FKs are ON DELETE
    # CASCADE), which is exactly what account-erasure is documented never to do.
    # The user_targets AFTER-DELETE trigger deactivates the target once its last
    # active follower leaves. Operators (api-key path, user_id is None) keep the
    # hard delete. Mirrors the audit-#29 H1 fix that turned the jobs "delete"
    # into a per-user unlink for the same shared-row reason.
    if user_id is not None:
        unlinked = await _unlink_user_from_target_async(supabase, user_id, target_id)
        if not unlinked:
            raise HTTPException(status_code=404, detail="Target not found")
        return DeleteResponse(deleted=True)
    deleted = await _delete_target_async(supabase, target_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Target not found")
    return DeleteResponse(deleted=True)


# ---- Create from job posting -----------------------------------------------


@router.post(
    "/from-posting/{posting_id}",
    response_model=JobTarget,
    status_code=201,
    dependencies=[Depends(enforce_llm_budget)],
)
async def create_target_from_posting(
    posting_id: str,
    supabase: AsyncClient = Depends(get_async_service_supabase),
    llm: LLMClient = Depends(get_llm_client),
    user_id: str | None = Depends(get_current_user_id_optional),
) -> JobTarget:
    """Create a target from an existing job posting.

    Reads the posting's title and description, creates a target, derives a
    scoring profile from the description via LLM, stores the JD as a
    reference, and activates the target.
    """
    # ``derive_profile_from_jd`` (content-hash cache) and the active-cap
    # ``crud.link_user_to_target`` / ``crud.set_app_active`` writes are
    # not-yet-async and take the sync service client — obtain one locally
    # (jobs.py materialize pattern). The catalog reads/writes ride the async
    # client via the router-inline helpers.
    svc = get_supabase()
    posting = await _get_job_posting_row(supabase, posting_id)
    if posting is None:
        raise HTTPException(status_code=404, detail="Job posting not found")

    title = posting.get("title") or "Untitled Role"
    description_html: str = posting.get("description_html") or ""
    absolute_url: str | None = posting.get("absolute_url")

    # Create the target
    target = await _create_target_async(supabase, payload=TargetCreate(label=title))

    # Derive scoring profile from description if substantial
    jd_text = strip_html(description_html)
    if len(jd_text) >= 50:
        try:
            derived, result = await derive_profile_from_jd(llm, jd_text=jd_text, supabase=svc)
            await cost_log.record_async(
                supabase,
                user_id=user_id,
                purpose=DEFAULT_PURPOSE,
                result=result,
                metadata={
                    "target_id": target.id,
                    "posting_id": posting_id,
                    "jd_url": absolute_url or "",
                },
            )

            await _add_reference_jd_async(
                supabase,
                target_id=target.id,
                jd_text=jd_text,
                jd_url=absolute_url,
                extracted_profile=derived.scoring_profile,
            )

            # Update target with the derived profile + search keywords
            await _update_target_async(
                supabase,
                target.id,
                TargetUpdate(
                    scoring_profile=derived.scoring_profile,
                    search_keywords=derived.search_keywords,
                    example_promising_titles=derived.example_promising_titles,
                    example_unpromising_titles=derived.example_unpromising_titles,
                ),
            )
        except Exception:
            logger.exception("Profile derivation failed for posting %s", posting_id)

    # Link the calling user to the new target (multi-user flow) so it
    # actually shows up in ``/targets/mine``. Without this insert, the
    # onboarding "I have a resume and a role in mind" path completes
    # with "All set!" but the user lands on a dashboard with zero
    # targets — the catalog row is created and globally active, but
    # ``user_targets`` was never populated, so every per-user view is
    # empty. For real users the active membership itself makes the
    # target pipeline-active (derived predicate, no catalog write).
    # api-key (cron) callers have no user identity to link — raise the
    # instance floor (``app_active``) instead so the target still
    # enters the pipeline.
    if user_id is not None:
        try:
            await asyncio.to_thread(
                crud.link_user_to_target,
                svc,
                user_id=user_id,
                target_id=target.id,
                is_active=True,
            )
        except crud.ActiveTargetLimitError as e:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "ACTIVE_LIMIT",
                    "limit": e.limit,
                    "active_count": e.current_count,
                    "message": (
                        f"You already have {e.current_count} active targets "
                        f"(limit {e.limit}) — deactivate one first."
                    ),
                },
            ) from e
        # Re-read the target row so the response reflects any writes the
        # linking flow made.
        refreshed = await _target_get(supabase, target.id)
        return refreshed or target
    activated = await asyncio.to_thread(crud.set_app_active, svc, target_id=target.id)
    return activated or target


# ---- Reference JDs ---------------------------------------------------------


async def _fetch_jd_from_url(url: str) -> ExtractionResult:
    """Fetch a JD page and run the extraction cascade (JSON-LD → meta → Firecrawl).

    Returns ``(title, jd_text)``. The title comes from the same extraction
    pipeline and may be ``None`` if no JSON-LD/meta tag was present.

    Raises ``HTTPException`` if the fetch fails or no usable description can
    be extracted. The minimum-length requirement matches ``ReferenceJDAdd``
    so the downstream LLM has enough signal to derive a profile.

    SSRF defense: re-resolve the hostname inside this function as a TOCTOU
    safety net. ``validate_job_url`` already ran upstream, but the upstream
    check resolves DNS once and we connect a few milliseconds later — a
    rebind in that window would slip through without this re-check.
    """
    from urllib.parse import urlparse

    hostname = urlparse(url).hostname or ""
    try:
        assert_safe_host(hostname)
    except ValueError as exc:
        # Generic client message — echoing the resolved host/IP back is an
        # internal-recon oracle (audit #29 R3 / H8). Keep specifics in logs.
        logger.warning("ssrf_reject JD host=%s: %s", hostname, exc)
        raise HTTPException(status_code=422, detail="This URL cannot be fetched") from exc

    # Size-capped streaming fetch — without this, a user-pasted URL
    # pointing to a huge payload could OOM the API (the shared
    # client's 15s timeout doesn't help against fast CDNs).
    try:
        # validate_host gates every redirect hop (not just the first/final
        # URL) before connecting — closes the SSRF redirect gap (#110).
        resp, body_bytes = await get_with_size_cap(url, validate_host=assert_safe_host)
        final_url = str(resp.url)
    except ResponseTooLargeError as exc:
        raise HTTPException(
            status_code=413,
            detail=f"JD page too large to fetch ({exc.size} bytes > {exc.limit}).",
        ) from exc
    except UnsafeURLError as exc:
        # A redirect hop resolved to an internal address — don't reflect it
        # (audit #29 R3 / H8).
        logger.warning("ssrf_reject JD redirect for %s: %s", url, exc)
        raise HTTPException(status_code=422, detail="This URL cannot be fetched") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=400, detail="Failed to fetch JD URL") from exc

    final_hostname = urlparse(final_url).hostname or ""
    if final_hostname and final_hostname != hostname:
        try:
            assert_safe_host(final_hostname)
        except ValueError as exc:
            # Don't reflect the resolved internal host/IP (audit #29 R3 / H8).
            logger.warning("ssrf_reject JD redirect host=%s: %s", final_hostname, exc)
            raise HTTPException(
                status_code=422,
                detail="This URL cannot be fetched",
            ) from exc

    # The stream was consumed by ``get_with_size_cap`` so ``resp.text``
    # is empty here; decode the bytes the helper returned.
    html = body_bytes.decode("utf-8", errors="replace") if resp.status_code == 200 else ""
    extraction: ExtractionResult
    if html:
        extraction = extract_job_from_html(html, final_url)
    else:
        extraction = ExtractionResult(tier="none", warnings=["fetch_non_200"])

    if extraction.tier == "none" or len(extraction.description_html or "") < 50:
        fc = await _extract_from_firecrawl(final_url)
        if fc.tier != "none" and len(fc.description_html or "") >= len(
            extraction.description_html or ""
        ):
            extraction = fc

    jd_text = extraction.description_html or ""
    if len(jd_text) < 50:
        raise HTTPException(
            status_code=422,
            detail=(
                "Could not extract a job description from that URL. "
                "Try pasting the JD text directly."
            ),
        )
    # Return the whole extraction (title + company + location + description +
    # salary) so the from-url flow can materialize a full job, not just derive
    # a profile. jd_text above only validated that description_html is usable.
    return extraction


@router.post(
    "/{target_id}/reference-jds",
    response_model=JobTarget,
    status_code=201,
    dependencies=[Depends(enforce_llm_budget)],
)
@limiter.limit("10/minute")
async def add_reference_jd(
    request: Request,
    target_id: str,
    body: ReferenceJDAdd,
    supabase: AsyncClient = Depends(get_async_service_supabase),
    llm: LLMClient = Depends(get_llm_client),
    user_id: str | None = Depends(get_current_user_id_optional),
) -> JobTarget:
    """Add a reference JD, derive a scoring profile via LLM, and merge.

    Accepts either ``jd_text`` (>=50 chars) or ``jd_url``. When only the URL
    is provided, the server fetches the page and extracts JD text via the
    same cascade used by ``POST /jobs/manual`` (JSON-LD → meta tags →
    Firecrawl).

    Genuinely async (awaits URL validation, the JD fetch, and the LLM
    derive). The shared-catalog reads/writes ride the async service client via
    the router-inline helpers + the #191 merge RPC's async twin; the
    not-yet-async ``derive_profile_from_jd`` content cache + ``project_profile_impact``
    take a locally-obtained sync client, offloaded via ``asyncio.to_thread``. See #107.
    """
    await _require_user_owns_target_async(supabase, user_id=user_id, target_id=target_id)
    # Verify target exists
    target = await _target_get(supabase, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Target not found")

    # ``derive_profile_from_jd`` (content-hash cache) + ``project_profile_impact``
    # are not-yet-async and take the sync service client (jobs.py pattern).
    svc = get_supabase()

    # Cap reference-JD contributions per user per target (#47): bounds a single
    # (possibly rogue) contributor's footprint on the shared profile, on top of
    # the per-contributor merge de-bias + downvote suppression. Checked before
    # the LLM derive so an over-cap add costs nothing. Operator/api-key callers
    # (user_id None) are exempt. Soft cap — see config.
    if user_id is not None:
        contributed = await _count_user_reference_jds_async(
            supabase, target_id=target_id, user_id=user_id
        )
        if contributed >= settings.reference_jd_max_per_user_per_target:
            raise HTTPException(
                status_code=409,
                detail=(
                    "You've reached the limit of "
                    f"{settings.reference_jd_max_per_user_per_target} reference "
                    "JDs for this target. Remove one before adding another."
                ),
            )

    # Validate JD URL if provided (#496)
    if body.jd_url:
        vr = await validate_job_url(body.jd_url)
        if not vr.is_valid:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid JD URL: {vr.rejection_reason}",
            )
        body.jd_url = vr.final_url

    # If no jd_text, fetch + extract from the URL
    if not body.jd_text:
        if not body.jd_url:
            raise HTTPException(status_code=422, detail="Either jd_text or jd_url is required")
        body.jd_text = (await _fetch_jd_from_url(body.jd_url)).description_html

    if not body.jd_text:
        # _fetch_jd_from_url validates a usable description before returning, but
        # the model field is nullable — guard so the type narrows to str and the
        # deferred closure below can't capture a None.
        raise HTTPException(
            status_code=422,
            detail="Could not extract a job description from that URL.",
        )

    # Bind to a non-None local: ``body.jd_text`` is guaranteed set past this
    # point, but the narrowing wouldn't survive into the deferred to_thread
    # closure below.
    jd_text = body.jd_text

    # Derive profile from JD via LLM (cached by content hash + prompt version).
    # An empty/garbage JD (failed fetch, paywall) is rejected before it can
    # poison the shared target's cached profile (#47).
    try:
        derived, result = await derive_profile_from_jd(llm, jd_text=jd_text, supabase=svc)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await cost_log.record_async(
        supabase,
        user_id=user_id,
        purpose=DEFAULT_PURPOSE,
        result=result,
        metadata={"target_id": target_id, "jd_url": body.jd_url or ""},
    )

    # Store the reference JD, attributed to the contributing user so the
    # merge can de-bias by contributor (#5 refinement layer).
    added_jd = await _add_reference_jd_async(
        supabase,
        target_id=target_id,
        jd_text=jd_text,
        jd_url=body.jd_url,
        extracted_profile=derived.scoring_profile,
        user_id=user_id,
    )

    # Operator path (api-key caller): no follower identity to RPC-gate on —
    # trusted direct write, the documented #191 exception (same convention
    # as the operator/cron service-role paths in docs/rls-data-access.md).
    # Example title pools come from the LATEST JD only — these are concrete
    # examples not weighted aggregates, and merging across JDs would dilute
    # the few-shot signal. The latest JD overwrites; pools stay coherent.
    if user_id is None:
        all_ref_jds = await _list_reference_jds_async(supabase, target_id)
        composite = merge_reference_jds(all_ref_jds)
        updated = await _update_target_async(
            supabase,
            target_id,
            TargetUpdate(
                scoring_profile=composite,
                search_keywords=derived.search_keywords,
                example_promising_titles=derived.example_promising_titles,
                example_unpromising_titles=derived.example_unpromising_titles,
                profile_version=target.profile_version + 1,
            ),
        )
        if updated is None:
            raise HTTPException(status_code=500, detail="Failed to update target profile")
        return updated

    # #191 slice 1b — user contributions to the SHARED profile are bounded
    # and RPC-gated. Project the merged profile's impact over the target's
    # recent scored jobs (the same learning-rate cap the learner uses, #5
    # P4): an outlier contribution is QUARANTINED (suppressed-at-birth, so
    # every merge excludes it) and staged to the learning-log review rail
    # instead of applied. Otherwise the write goes through the merge RPC —
    # in-DB follower re-check + version guard — retried once on a
    # concurrent-write conflict.
    current = target
    for _attempt in range(2):
        all_ref_jds = await _list_reference_jds_async(supabase, target_id)
        composite = merge_reference_jds(all_ref_jds)
        prev_profile = current.scoring_profile.model_dump()
        # Project the full "after" state: the merge installs the new JD's
        # derived search_keywords alongside the profile, and keywords feed
        # scoring — evaluating the new profile under the OLD keywords would
        # misjudge outliers (Copilot on #204). ``project_profile_impact`` is
        # not-yet-async → sync client, offloaded via ``to_thread``.
        projection = await asyncio.to_thread(
            project_profile_impact,
            svc,
            target_id,
            prev_profile,
            composite.model_dump(),
            current.search_keywords,
            derived.search_keywords,
        )

        if projection is not None and projection.capped:
            note = (
                f"[staged merge: this reference JD would move "
                f"{projection.jobs_moved}/{projection.jobs_considered} recent "
                f"jobs by ≥{projection.move_threshold} pts, over the "
                f"{projection.max_moved_fraction:.0%} cap]"
            )
            staged_diff = ProfilePatch(confidence=0.0, rationale=note)
            staged_row: dict[str, Any] = {
                "user_id": user_id,
                "target_id": target_id,
                "status": "staged",
                "kind": "merge",
                "prev_profile": prev_profile,
                "next_profile": composite.model_dump(),
                "diff": staged_diff.model_dump(mode="json"),
                "confidence": 0.0,
                "rationale": note,
                "signals_consumed": 0,
                "applied_run_id": None,
                "projection": projection.model_dump(mode="json"),
                "merge_payload": {
                    "ref_jd_id": added_jd.id,
                    "search_keywords": derived.search_keywords,
                    "example_promising_titles": derived.example_promising_titles,
                    "example_unpromising_titles": derived.example_unpromising_titles,
                },
            }
            await _suppress_reference_jd_async(supabase, added_jd.id)
            await _insert_learning_log_row_async(supabase, staged_row)
            logger.info(
                "Reference-JD contribution QUARANTINED for (user=%s, "
                "target=%s): projected to move %d/%d jobs ≥%d pts (cap %.0f%%)",
                user_id,
                target_id,
                projection.jobs_moved,
                projection.jobs_considered,
                projection.move_threshold,
                projection.max_moved_fraction * 100,
            )
            # Profile untouched — the staged row is reviewable in the
            # learning-log panel (apply lifts the quarantine).
            return current

        outcome, _new_version = await apply_profile_merge_rpc_async(
            supabase,
            user_id=user_id,
            target_id=target_id,
            next_profile=composite.model_dump(),
            expected_version=current.profile_version,
            search_keywords=derived.search_keywords,
            example_promising=derived.example_promising_titles,
            example_unpromising=derived.example_unpromising_titles,
        )
        if outcome == "applied":
            updated = await _target_get(supabase, target_id)
            if updated is None:
                raise HTTPException(status_code=500, detail="Failed to update target profile")
            return updated
        if outcome == "version_conflict":
            refreshed = await _target_get(supabase, target_id)
            if refreshed is None:
                raise HTTPException(status_code=404, detail="Target not found")
            current = refreshed
            continue
        # not_a_follower / target_not_found — the link was severed mid-flight.
        raise HTTPException(status_code=404, detail="Target not found for user")

    raise HTTPException(
        status_code=409,
        detail="Target profile is changing concurrently — retry",
    )


@router.get("/{target_id}/reference-jds", response_model=ReferenceJDsListResponse)
async def list_reference_jds(
    target_id: str,
    supabase: AsyncClient = Depends(get_async_service_supabase),
    user_id: str | None = Depends(get_current_user_id_optional),
) -> ReferenceJDsListResponse:
    # Reference JDs are read via the service-role client with no RLS
    # backstop. Without this guard any authenticated user could read any
    # target's full reference-JD text by id (audit #29 round 3 / M3).
    # Operators (user_id None) bypass; non-owners get 404.
    await _require_user_owns_target_async(supabase, user_id=user_id, target_id=target_id)
    ref_jds = await _list_reference_jds_async(supabase, target_id)
    # Strip the contributor ``user_id``: the contribution graph is meant to
    # be anonymous (votes are anonymous, #5 P3), and surfacing the
    # per-row ``user_id`` here deanonymizes who contributed each JD.
    anonymized = [jd.model_copy(update={"user_id": None}) for jd in ref_jds]
    return ReferenceJDsListResponse(reference_jds=anonymized)


# Sync `def` (not `async def`): the whole body is blocking supabase work
# (ownership check, delete, re-merge read, update), so FastAPI runs it in its
# threadpool and keeps it off the event loop. See #107.
@router.delete(
    "/{target_id}/reference-jds/{ref_jd_id}",
    response_model=JobTarget,
)
def delete_reference_jd(
    target_id: str,
    ref_jd_id: str,
    supabase: Client = Depends(get_supabase),
    user_id: str | None = Depends(get_current_user_id_optional),
) -> JobTarget:
    """Delete a reference JD and re-merge the remaining profiles."""
    _require_user_owns_target(supabase, user_id=user_id, target_id=target_id)
    target = crud.get(supabase, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Target not found")

    # A regular caller may only remove their OWN contribution; operators
    # (user_id None) may remove any. A non-owner's delete matches no rows and
    # returns 404 — without enumerating who contributed it.
    deleted = crud.delete_reference_jd(supabase, ref_jd_id, target_id=target_id, user_id=user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Reference JD not found")

    # Operator path (api-key caller): no follower identity to RPC-gate on —
    # trusted direct write, the documented #191 exception.
    if user_id is None:
        remaining = crud.list_reference_jds(supabase, target_id)
        composite = merge_reference_jds(remaining) if remaining else ScoringProfile()
        updated = crud.update(
            supabase,
            target_id,
            TargetUpdate(
                scoring_profile=composite,
                profile_version=target.profile_version + 1,
            ),
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="Target not found")
        return updated

    # #191: removal-direction re-merge — RPC-gated (in-DB follower re-check +
    # version guard), deliberately NOT capped: removing your own contribution
    # is the correction layer and must never be blocked by the learning-rate
    # cap. Retried once on a concurrent-write conflict.
    current = target
    for _attempt in range(2):
        remaining = crud.list_reference_jds(supabase, target_id)
        composite = merge_reference_jds(remaining) if remaining else ScoringProfile()
        outcome, _new_version = apply_profile_patch_rpc(
            supabase,
            user_id=user_id,
            target_id=target_id,
            next_profile=composite.model_dump(),
            expected_version=current.profile_version,
        )
        if outcome == "applied":
            updated = crud.get(supabase, target_id)
            if updated is None:
                raise HTTPException(status_code=404, detail="Target not found")
            return updated
        if outcome == "version_conflict":
            refreshed = crud.get(supabase, target_id)
            if refreshed is None:
                raise HTTPException(status_code=404, detail="Target not found")
            current = refreshed
            continue
        raise HTTPException(status_code=404, detail="Target not found for user")

    raise HTTPException(
        status_code=409,
        detail="Target profile is changing concurrently — retry",
    )


# `async def` (#57 PR-G2b): the caller's vote write rides the ASYNC RLS user
# client and the shared-catalog reads + #191 re-merge ride the ASYNC service
# client (all awaited). Only the service-role vote-tally RPC
# (``recompute_suppression``) is not-yet-async — offloaded via ``asyncio.to_thread``
# on a locally-obtained sync client, so no blocking `.execute()` hits the loop
# (#107). slowapi's @limiter.limit works on async handlers too (it reads the
# `request` arg).
@router.post(
    "/{target_id}/reference-jds/{ref_jd_id}/vote",
    response_model=ContributionVoteResult,
)
@limiter.limit("30/minute")
async def vote_on_reference_jd(
    request: Request,
    target_id: str,
    ref_jd_id: str,
    body: ReferenceJDVote,
    supabase: AsyncClient = Depends(get_async_service_supabase),
    user_supabase: AsyncClient = Depends(get_async_user_supabase),
    user_id: str = Depends(get_current_user_id),
) -> ContributionVoteResult:
    """Up/down-vote (or clear) a reference-JD contribution (#5 P3).

    A target's followers moderate its contributions: once a contribution's net
    down-votes reach the quorum it is suppressed from the shared-profile merge
    and the profile is re-merged without it (and restored if up-votes later
    rescue it). Votes are anonymous — only the caller's own vote and the
    suppression outcome are returned, never the tally or who voted.
    """
    await _require_user_owns_target_async(supabase, user_id=user_id, target_id=target_id)

    # The contribution must belong to this target — no cross-target voting, and
    # a 404 rather than leaking the existence of another target's JD.
    ref_jds = await _list_reference_jds_async(supabase, target_id)
    if not any(j.id == ref_jd_id for j in ref_jds):
        raise HTTPException(status_code=404, detail="Reference JD not found")

    # The caller's vote goes through their async RLS client (DB enforces own-row).
    await votes.set_user_vote(
        user_supabase, reference_jd_id=ref_jd_id, user_id=user_id, value=body.value
    )

    # Tally every vote (service-role RPC, not-yet-async) and reconcile the
    # suppression flag on a locally-obtained sync client, offloaded off the loop.
    suppressed, changed = await asyncio.to_thread(
        votes.recompute_suppression,
        get_supabase(),
        reference_jd_id=ref_jd_id,
        quorum=settings.contribution_downvote_quorum,
    )

    profile_version: int | None = None
    if changed:
        # Suppression flipped — re-merge the shared profile over the now
        # (un)suppressed set and bump the version so lazy re-scoring picks it
        # up. #191: RPC-gated with the voter's identity (a voter is a
        # follower by the ownership check above); NOT capped — the flip is a
        # quorum-approved moderation outcome, and delaying a poisoned
        # contribution's removal behind the learning-rate cap would be
        # backwards. Retried once on a concurrent-write conflict; a second
        # conflict leaves the profile to the next reconciliation (the vote
        # itself and the suppression flag are already durable). Runs on the
        # async service client (reads + #191 patch RPC twin).
        for _attempt in range(2):
            target = await _target_get(supabase, target_id)
            if target is None:
                break
            composite = merge_reference_jds(await _list_reference_jds_async(supabase, target_id))
            outcome, new_version = await apply_profile_patch_rpc_async(
                supabase,
                user_id=user_id,
                target_id=target_id,
                next_profile=composite.model_dump(),
                expected_version=target.profile_version,
            )
            if outcome == "applied":
                profile_version = new_version
                break
            if outcome != "version_conflict":
                logger.warning(
                    "Vote re-merge refused by RPC (%s) for (user=%s, target=%s)",
                    outcome,
                    user_id,
                    target_id,
                )
                break

    return ContributionVoteResult(
        reference_jd_id=ref_jd_id,
        your_vote=body.value,
        suppressed=suppressed,
        profile_version=profile_version,
    )
