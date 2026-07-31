"""Target CRUD operations against Supabase (#495).

All functions follow the same pattern as app/services/experience/prose.py:
thin wrappers over Supabase table operations that validate rows through
Pydantic models on the way out.
"""

import re
from datetime import UTC, datetime
from typing import Any, cast

from supabase import Client

from app.models.targets import (
    AxisWeights,
    JobTarget,
    JobTargetSummary,
    ScoringProfile,
    TargetCreate,
    TargetPreferences,
    TargetReferenceJD,
    TargetUpdate,
    UserTarget,
    UserTargetWithSummary,
    UserTargetWithTarget,
)

TARGETS_TABLE = "targets"
USER_TARGETS_TABLE = "user_targets"
REF_JDS_TABLE = "reference_jds"

_LABEL_WHITESPACE_RE = re.compile(r"\s+")


def normalize_label(label: str) -> str:
    """Canonical dedup-key normalization for a target label.

    Lowercase, trim, and collapse internal whitespace runs to a single space
    — so "Senior Engineer" and "Senior  Engineer" normalize identically. This
    is the **single source of truth** for the value stored in
    ``targets.normalized_label`` and matched against it (``find_matching_target``
    imports this), so the write path and the lookup path can never disagree.
    It is exactly the key the ``targets_normalized_label_key`` UNIQUE
    constraint enforces.
    """
    return _LABEL_WHITESPACE_RE.sub(" ", label.lower().strip())


def _parse_target(row: dict[str, Any]) -> JobTarget:
    """Parse a raw Supabase row into a JobTarget, handling JSONB fields."""
    return JobTarget(
        id=row["id"],
        label=row["label"],
        description=row.get("description"),
        normalized_label=row.get("normalized_label"),
        scoring_profile=ScoringProfile.model_validate(row.get("scoring_profile") or {}),
        search_keywords=row.get("search_keywords") or [],
        activation_status=row.get("activation_status") or "idle",
        profile_version=row.get("profile_version", 1),
        app_active=row["app_active"],
        example_promising_titles=row.get("example_promising_titles") or [],
        example_unpromising_titles=row.get("example_unpromising_titles") or [],
        # Slim shape (NULL on legacy rows until PR B backfill).
        seniority_hint=row.get("seniority_hint"),
        domain_hints=row.get("domain_hints") or [],
        role_family=row.get("role_family"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _summarize_target(row: dict[str, Any]) -> JobTargetSummary:
    """Project a raw Supabase row into the list-view summary (#863).

    ``keyword_count`` / ``category_count`` are derived here from the
    ``scoring_profile`` JSONB so list responses can omit the column itself.
    Legacy rows with NULL/missing profile collapse to 0/0.
    """
    cats = ((row.get("scoring_profile") or {}).get("categories")) or {}
    keyword_count = sum(len((c or {}).get("keywords") or {}) for c in cats.values())
    return JobTargetSummary(
        id=row["id"],
        label=row["label"],
        description=row.get("description"),
        normalized_label=row.get("normalized_label"),
        activation_status=row.get("activation_status") or "idle",
        profile_version=row.get("profile_version", 1),
        app_active=row["app_active"],
        seniority_hint=row.get("seniority_hint"),
        keyword_count=keyword_count,
        category_count=len(cats),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _parse_user_target(row: dict[str, Any]) -> UserTarget:
    """Parse a raw Supabase row into a UserTarget."""
    aw_raw = row.get("axis_weights")
    awp_raw = row.get("axis_weights_previous")
    return UserTarget(
        id=row["id"],
        user_id=row["user_id"],
        target_id=row["target_id"],
        is_active=row["is_active"],
        fit_score=row.get("fit_score"),
        fit_score_reasoning=row.get("fit_score_reasoning"),
        axis_weights=AxisWeights.model_validate(aw_raw) if aw_raw else None,
        axis_weights_previous=(AxisWeights.model_validate(awp_raw) if awp_raw else None),
        job_score_threshold=row.get("job_score_threshold"),
        sms_score_threshold=row.get("sms_score_threshold"),
        # Per-user preferences (#60). Columns may be absent on a row read back
        # from an older shape; fall back to the model defaults so the parse
        # never KeyErrors. ``pref_score_cutoff`` NULL (column default applies
        # at write time) collapses to the 40-point default here too.
        pref_score_cutoff=(
            row["pref_score_cutoff"] if row.get("pref_score_cutoff") is not None else 40
        ),
        pref_locations=row.get("pref_locations"),
        pref_remote_ok=(row["pref_remote_ok"] if row.get("pref_remote_ok") is not None else True),
        pref_seniority_min=row.get("pref_seniority_min"),
        pref_seniority_max=row.get("pref_seniority_max"),
        pref_employment_types=row.get("pref_employment_types"),
        pref_include_unknown_salary=(
            row["pref_include_unknown_salary"]
            if row.get("pref_include_unknown_salary") is not None
            else True
        ),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _parse_ref_jd(row: dict[str, Any]) -> TargetReferenceJD:
    return TargetReferenceJD(
        id=row["id"],
        target_id=row["target_id"],
        user_id=row.get("user_id"),
        jd_url=row.get("jd_url"),
        jd_text=row["jd_text"],
        extracted_profile=ScoringProfile.model_validate(row.get("extracted_profile") or {}),
        suppressed=bool(row.get("suppressed", False)),
        created_at=row["created_at"],
    )


# ---- Target CRUD -----------------------------------------------------------


def get_by_normalized_label(supabase: Client, normalized_label: str) -> JobTarget | None:
    """Return the shared-catalog target for a normalized label, or None.

    Exact match on ``normalized_label`` — the dedup key the
    ``targets_normalized_label_key`` UNIQUE constraint enforces.
    """
    resp = (
        supabase.table(TARGETS_TABLE)
        .select("*")
        .eq("normalized_label", normalized_label)
        .limit(1)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    return _parse_target(rows[0]) if rows else None


def create(supabase: Client, payload: TargetCreate) -> JobTarget:
    """Find-or-create a shared-catalog target, keyed on ``normalized_label``.

    The catalog is shared — one canonical row per role, ownership via
    ``user_targets`` — so create is **idempotent** on the normalized label: an
    existing row is returned unchanged rather than duplicated. This is the
    low-level backstop that ``UNIQUE(normalized_label)`` (migration
    20260717060000) made necessary — without it a raw ``POST /targets``, or a
    race between the ``find_matching_target`` check and the insert in the
    ``from_manual`` / ``from_suggestion`` create-or-link paths, would surface a
    23505 as a 500. Routing the insert through an on-conflict upsert makes it
    race-safe: concurrent creators converge on the one committed row. The
    richer exact+fuzzy dedup still lives in the higher-level paths; here we
    align exactly with the DB constraint (exact ``normalized_label``).
    """
    normalized = normalize_label(payload.label)
    row: dict[str, Any] = {
        "label": payload.label,
        "description": payload.description,
        "normalized_label": normalized,
        "scoring_profile": payload.scoring_profile.model_dump(),
        "search_keywords": payload.search_keywords,
    }
    # Insert unless the normalized label already exists; ignore_duplicates
    # skips (never overwrites) the existing canonical row.
    resp = (
        supabase.table(TARGETS_TABLE)
        .upsert(row, on_conflict="normalized_label", ignore_duplicates=True)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    if rows:
        return _parse_target(rows[0])  # freshly inserted
    # Conflict → the row already existed (a prior create, or a concurrent
    # writer that has committed by the time our ignore-duplicates upsert
    # returned). Return the canonical row rather than a 500.
    existing = get_by_normalized_label(supabase, normalized)
    if existing is not None:
        return existing
    raise RuntimeError("Failed to insert or locate targets row")


def get(supabase: Client, target_id: str) -> JobTarget | None:
    resp = supabase.table(TARGETS_TABLE).select("*").eq("id", target_id).execute()
    rows = cast(list[dict[str, Any]], resp.data or [])
    return _parse_target(rows[0]) if rows else None


def search_by_label(supabase: Client, query: str, *, limit: int = 20) -> list[JobTarget]:
    """Substring search over the shared catalog by display label.

    Discovery for the "search for a target" flow: a role one user created is
    visible to all, so a new user can follow an existing target instead of
    minting a duplicate. Case-insensitive ``ILIKE`` on ``label`` (a small
    column that scans fine at the current catalog size), ordered by label for
    stable results. Returns ``[]`` for a blank / too-short query so the UI
    never dumps the whole catalog on an empty box.
    """
    trimmed = query.strip()
    if len(trimmed) < 2:
        return []
    resp = (
        supabase.table(TARGETS_TABLE)
        .select("*")
        .ilike("label", f"%{trimmed}%")
        .order("label")
        .limit(limit)
        .execute()
    )
    return [_parse_target(cast(dict[str, Any], r)) for r in (resp.data or [])]


def list_all(supabase: Client) -> list[JobTarget]:
    """Return all targets, ordered by creation date."""
    resp = supabase.table(TARGETS_TABLE).select("*").order("created_at", desc=True).execute()
    return [_parse_target(cast(dict[str, Any], r)) for r in (resp.data or [])]


def get_active(supabase: Client) -> list[JobTarget]:
    """Return every PIPELINE-ACTIVE target: ``app_active OR EXISTS(active
    membership)``.

    ``app_active`` is the standing instance-sponsorship floor (app-owned
    catalog / operator; zero-membership targets the instance ingests anyway,
    #543). Membership activity lives in ``user_targets.is_active``. The old
    single-flag read relied on ``trg_sync_target_active`` keeping a cache in
    ``targets.is_active`` — that trigger clobbered the catalog floor on any
    membership event, so it was dropped (schema audit P0, 2026-07-31) and the
    predicate is now derived here at read time: two indexed reads over tiny
    tables, deduped in Python.
    """
    floor_resp = supabase.table(TARGETS_TABLE).select("*").eq("app_active", True).execute()
    member_ids_resp = (
        supabase.table(USER_TARGETS_TABLE)
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
        member_resp = supabase.table(TARGETS_TABLE).select("*").in_("id", missing).execute()
        rows.extend(cast(list[dict[str, Any]], member_resp.data or []))
    return [_parse_target(r) for r in rows]


def is_pipeline_active(supabase: Client, target_id: str) -> bool:
    """Single-target form of :func:`get_active`'s predicate.

    Used by mid-cycle re-checks (poller / bulk scorer) that guard against a
    target being deactivated while a batch is in flight. Same two-arm rule:
    the instance floor OR any active membership.
    """
    t_resp = (
        supabase.table(TARGETS_TABLE)
        .select("app_active")
        .eq("id", target_id)
        .limit(1)
        .execute()
    )
    t_rows = cast(list[dict[str, Any]], t_resp.data or [])
    if not t_rows:
        return False
    if bool(t_rows[0].get("app_active")):
        return True
    m_resp = (
        supabase.table(USER_TARGETS_TABLE)
        .select("id", count="exact", head=True)  # type: ignore[arg-type]
        .eq("target_id", target_id)
        .eq("is_active", True)
        .execute()
    )
    return bool(m_resp.count or 0)


def get_all(supabase: Client) -> list[JobTarget]:
    """Return EVERY target, active or not, as parsed ``JobTarget`` models.

    The active-only counterpart is :func:`get_active`. Scheduled/bulk source
    discovery uses this one so a target nobody currently has active still has
    its ATS boards refreshed — an inactive target the user re-activates later
    should already have fresh sources rather than starting cold. Returns the
    full target (unlike the ``*_summary`` list projections) because discovery needs
    ``search_keywords``.
    """
    resp = supabase.table(TARGETS_TABLE).select("*").execute()
    return [_parse_target(cast(dict[str, Any], r)) for r in (resp.data or [])]


def update(supabase: Client, target_id: str, payload: TargetUpdate) -> JobTarget | None:
    updates: dict[str, Any] = {"updated_at": datetime.now(UTC).isoformat()}
    if payload.label is not None:
        updates["label"] = payload.label
        updates["normalized_label"] = normalize_label(payload.label)
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
    # Slim shape (PR A of plan-wyrdfold-streamlined-target.md). None on
    # the partial = "don't touch the column"; pass an empty list / "" to
    # explicitly clear.
    if payload.seniority_hint is not None:
        updates["seniority_hint"] = payload.seniority_hint
    if payload.domain_hints is not None:
        updates["domain_hints"] = payload.domain_hints
    if payload.role_family is not None:
        updates["role_family"] = payload.role_family

    resp = supabase.table(TARGETS_TABLE).update(updates).eq("id", target_id).execute()
    rows = cast(list[dict[str, Any]], resp.data or [])
    return _parse_target(rows[0]) if rows else None


def delete(supabase: Client, target_id: str) -> bool:
    resp = supabase.table(TARGETS_TABLE).delete().eq("id", target_id).execute()
    return bool(resp.data)


def set_app_active(supabase: Client, target_id: str) -> JobTarget | None:
    """Raise the instance-sponsorship floor: ``app_active = True``.

    Operator/system use only (seed script, api-key cron paths without a user
    identity). User flows activate via ``user_targets`` memberships — the
    pipeline predicate (:func:`get_active`) ORs the two arms.
    """
    resp = (
        supabase.table(TARGETS_TABLE)
        .update({"app_active": True, "updated_at": datetime.now(UTC).isoformat()})
        .eq("id", target_id)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    return _parse_target(rows[0]) if rows else None


def clear_app_active(supabase: Client, target_id: str) -> JobTarget | None:
    """Drop the instance-sponsorship floor: ``app_active = False``.

    Operator/system use only. Does not touch memberships — a target with an
    active member stays pipeline-active regardless.
    """
    resp = (
        supabase.table(TARGETS_TABLE)
        .update({"app_active": False, "updated_at": datetime.now(UTC).isoformat()})
        .eq("id", target_id)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    return _parse_target(rows[0]) if rows else None


# ---- User-scoped target queries ----------------------------------------------


def list_for_user(supabase: Client, user_id: str) -> list[JobTarget]:
    """Return all targets a user is linked to, ordered by creation date."""
    ut_resp = (
        supabase.table(USER_TARGETS_TABLE).select("target_id").eq("user_id", user_id).execute()
    )
    ut_rows = cast(list[dict[str, Any]], ut_resp.data or [])
    target_ids = [r["target_id"] for r in ut_rows]
    if not target_ids:
        return []

    resp = (
        supabase.table(TARGETS_TABLE)
        .select("*")
        .in_("id", target_ids)
        .order("created_at", desc=True)
        .execute()
    )
    return [_parse_target(cast(dict[str, Any], r)) for r in (resp.data or [])]


def get_active_for_user(supabase: Client, user_id: str) -> list[JobTarget]:
    """Return targets a user has active (is_active=True in user_targets)."""
    ut_resp = (
        supabase.table(USER_TARGETS_TABLE)
        .select("target_id")
        .eq("user_id", user_id)
        .eq("is_active", True)
        .execute()
    )
    ut_rows = cast(list[dict[str, Any]], ut_resp.data or [])
    target_ids = [r["target_id"] for r in ut_rows]
    if not target_ids:
        return []

    resp = supabase.table(TARGETS_TABLE).select("*").in_("id", target_ids).execute()
    return [_parse_target(cast(dict[str, Any], r)) for r in (resp.data or [])]


def get_user_target_ids(supabase: Client, user_id: str) -> set[str]:
    """Return the set of target IDs a user is linked to (any status)."""
    resp = supabase.table(USER_TARGETS_TABLE).select("target_id").eq("user_id", user_id).execute()
    rows = cast(list[dict[str, Any]], resp.data or [])
    return {r["target_id"] for r in rows}


def list_user_targets_with_targets(supabase: Client, user_id: str) -> list[UserTargetWithTarget]:
    """Return a user's targets paired with their junction data (fit score)."""
    ut_resp = (
        supabase.table(USER_TARGETS_TABLE)
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .execute()
    )
    ut_rows = cast(list[dict[str, Any]], ut_resp.data or [])
    if not ut_rows:
        return []

    target_ids = [r["target_id"] for r in ut_rows]
    t_resp = supabase.table(TARGETS_TABLE).select("*").in_("id", target_ids).execute()
    targets_by_id = {
        cast(dict[str, Any], r)["id"]: _parse_target(cast(dict[str, Any], r))
        for r in (t_resp.data or [])
    }

    results: list[UserTargetWithTarget] = []
    for ut_row in ut_rows:
        target = targets_by_id.get(ut_row["target_id"])
        if target is None:
            continue
        results.append(
            UserTargetWithTarget(
                user_target=_parse_user_target(ut_row),
                target=target,
            )
        )
    return results


def list_user_targets_with_summary(supabase: Client, user_id: str) -> list[UserTargetWithSummary]:
    """List-view projection of :func:`list_user_targets_with_targets` (#863).

    Same junction + targets fetch, but pairs each link with the light
    :class:`JobTargetSummary` instead of the full target.
    """
    ut_resp = (
        supabase.table(USER_TARGETS_TABLE)
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .execute()
    )
    ut_rows = cast(list[dict[str, Any]], ut_resp.data or [])
    if not ut_rows:
        return []

    target_ids = [r["target_id"] for r in ut_rows]
    t_resp = supabase.table(TARGETS_TABLE).select("*").in_("id", target_ids).execute()
    summaries_by_id = {
        cast(dict[str, Any], r)["id"]: _summarize_target(cast(dict[str, Any], r))
        for r in (t_resp.data or [])
    }

    results: list[UserTargetWithSummary] = []
    for ut_row in ut_rows:
        summary = summaries_by_id.get(ut_row["target_id"])
        if summary is None:
            continue
        results.append(
            UserTargetWithSummary(
                user_target=_parse_user_target(ut_row),
                target=summary,
            )
        )
    return results


# ---- User-Target junction CRUD ----------------------------------------------


MAX_ACTIVE_TARGETS_PER_USER = 1
"""Per-user cap on simultaneously active targets.

Caps fan-out of the LLM scoring pipeline (Phase 1/2 spend scales with
active targets — each one costs ~$1/month in background grading against
a $5/month allowance). Inactive targets are not counted — a user can
keep arbitrarily many as "saved searches" they cycle between. Per-user
override via ``user_profiles.max_active_targets`` (the operator's "add
credits" lever).
"""


def effective_active_target_cap(supabase: Client, user_id: str) -> int:
    """The user's active-target cap.

    Resolution: per-user ``max_active_targets`` override → the plan
    tier's cap (saas mode, Phase 3 — the structural bound on background
    grading cost) → the global default (self_host).
    """
    resp = (
        supabase.table("user_profiles")
        .select("max_active_targets,plan")
        .eq("user_id", user_id)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    override = rows[0].get("max_active_targets") if rows else None
    if override is not None:
        return int(override)

    from app.config import settings

    if settings.deployment_mode == "saas":
        from app.services.entitlements import entitlements_for

        plan = cast("str | None", rows[0].get("plan")) if rows else None
        return entitlements_for(plan).max_active_targets

    return MAX_ACTIVE_TARGETS_PER_USER


class ActiveTargetLimitError(Exception):
    """Raised when activating a target would exceed the per-user cap."""

    def __init__(self, current_count: int, limit: int) -> None:
        self.current_count = current_count
        self.limit = limit
        super().__init__(f"Active target limit ({limit}) reached; currently {current_count} active")


def count_active_for_user(supabase: Client, user_id: str) -> int:
    """Return the number of user_targets rows with ``is_active=True`` for this user."""
    resp = (
        supabase.table(USER_TARGETS_TABLE)
        .select("id", count="exact")  # type: ignore[arg-type]
        .eq("user_id", user_id)
        .eq("is_active", True)
        .limit(1)
        .execute()
    )
    return resp.count or 0


def link_user_to_target(
    supabase: Client,
    *,
    user_id: str,
    target_id: str,
    is_active: bool = True,
    fit_score: int | None = None,
    fit_score_reasoning: str | None = None,
    fit_score_prose_doc_id: str | None = None,
    enforce_active_limit: bool = True,
) -> UserTarget:
    """Link a user to a target (upsert). Membership activity feeds the derived
    pipeline predicate (see :func:`get_active`); nothing on ``targets`` is written.

    Raises ``ActiveTargetLimitError`` when ``is_active=True`` would push
    the user above ``MAX_ACTIVE_TARGETS_PER_USER`` — but ONLY when this
    call introduces a new active link. Re-upserting an already-active
    link (e.g., refreshing fit_score on a row the user already has
    active) is exempt because no net change happens.

    Pass ``enforce_active_limit=False`` for internal callers that need
    to bypass the cap — e.g., a future migration backfilling
    user_targets rows from a different source.
    """
    if is_active and enforce_active_limit:
        # Determine whether this upsert will INCREASE the active count
        # or just refresh an already-active row. Skip the count check
        # for the latter to keep idempotent updates free.
        existing_resp = (
            supabase.table(USER_TARGETS_TABLE)
            .select("is_active")
            .eq("user_id", user_id)
            .eq("target_id", target_id)
            .limit(1)
            .execute()
        )
        existing = cast(list[dict[str, Any]], existing_resp.data or [])
        already_active = bool(existing and existing[0].get("is_active"))
        if not already_active:
            current = count_active_for_user(supabase, user_id)
            cap = effective_active_target_cap(supabase, user_id)
            if current >= cap:
                raise ActiveTargetLimitError(current, cap)

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
    # Stamp the profile version this score was computed against (E2), so a later
    # profile edit makes it detectably stale. Written alongside the score.
    if fit_score_prose_doc_id is not None:
        row["fit_score_prose_doc_id"] = fit_score_prose_doc_id

    resp = supabase.table(USER_TARGETS_TABLE).upsert(row, on_conflict="user_id,target_id").execute()
    rows = cast(list[dict[str, Any]], resp.data or [])
    if not rows:
        raise RuntimeError("Failed to upsert user_targets row")
    return _parse_user_target(rows[0])


def get_fit_score_prose_doc_id(
    supabase: Client, *, user_id: str, target_id: str
) -> str | None:
    """The prose-doc version marker on the user's link (E2), or None if the link
    is gone or was never scored. Read right before a lazy refresh recomputes, so
    a concurrent refresh (two quick views) doesn't double-spend the LLM."""
    resp = (
        supabase.table(USER_TARGETS_TABLE)
        .select("fit_score_prose_doc_id")
        .eq("user_id", user_id)
        .eq("target_id", target_id)
        .limit(1)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    return rows[0].get("fit_score_prose_doc_id") if rows else None


def update_fit_score(
    supabase: Client,
    *,
    user_id: str,
    target_id: str,
    fit_score: int,
    fit_score_reasoning: str | None,
    fit_score_prose_doc_id: str | None,
) -> None:
    """Update ONLY the fit-score columns on an existing link (E2 lazy refresh).

    A targeted UPDATE, not an upsert through ``link_user_to_target`` — that path
    always writes ``is_active`` and would flip the active flag (and trip the
    active-target cap) as a side effect of a background rescore. This touches
    nothing but the score, its reasoning, and the version marker.
    """
    (
        supabase.table(USER_TARGETS_TABLE)
        .update(
            {
                "fit_score": fit_score,
                "fit_score_reasoning": fit_score_reasoning,
                "fit_score_prose_doc_id": fit_score_prose_doc_id,
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )
        .eq("user_id", user_id)
        .eq("target_id", target_id)
        .execute()
    )


def unlink_user_from_target(supabase: Client, user_id: str, target_id: str) -> bool:
    """Remove a user–target link. The DB trigger will deactivate the target
    if no other users have it active."""
    resp = (
        supabase.table(USER_TARGETS_TABLE)
        .delete()
        .eq("user_id", user_id)
        .eq("target_id", target_id)
        .execute()
    )
    return bool(resp.data)


def get_user_target(supabase: Client, user_id: str, target_id: str) -> UserTarget | None:
    """Get a specific user–target link."""
    resp = (
        supabase.table(USER_TARGETS_TABLE)
        .select("*")
        .eq("user_id", user_id)
        .eq("target_id", target_id)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    return _parse_user_target(rows[0]) if rows else None


def list_user_targets(supabase: Client, user_id: str) -> list[UserTarget]:
    """Return all targets linked to a user."""
    resp = (
        supabase.table(USER_TARGETS_TABLE)
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .execute()
    )
    return [_parse_user_target(cast(dict[str, Any], r)) for r in (resp.data or [])]


def set_user_target_active(supabase: Client, user_id: str, target_id: str) -> UserTarget | None:
    """Activate a user's link to a target (feeds the derived pipeline predicate)."""
    resp = (
        supabase.table(USER_TARGETS_TABLE)
        .update({"is_active": True, "updated_at": datetime.now(UTC).isoformat()})
        .eq("user_id", user_id)
        .eq("target_id", target_id)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    return _parse_user_target(rows[0]) if rows else None


def set_user_target_inactive(supabase: Client, user_id: str, target_id: str) -> UserTarget | None:
    """Deactivate a user's link to a target (feeds the derived pipeline predicate)."""
    resp = (
        supabase.table(USER_TARGETS_TABLE)
        .update({"is_active": False, "updated_at": datetime.now(UTC).isoformat()})
        .eq("user_id", user_id)
        .eq("target_id", target_id)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    return _parse_user_target(rows[0]) if rows else None


def set_user_target_axis_weights(
    supabase: Client,
    *,
    user_id: str,
    target_id: str,
    weights: AxisWeights | None,
) -> UserTarget | None:
    """Set (or clear) the user-tunable axis weights for this user-target pair.

    Snapshots the prior ``axis_weights`` value into ``axis_weights_previous``
    so the UI's "undo last change" button can revert in one click. Only
    the most recent prior state is kept — full history is YAGNI for v1
    (see plan-wyrdfold-streamlined-target.md "User-tunable axis weights").

    Passing ``weights=None`` resets to defaults (DB column becomes NULL).
    The previous value is still snapshotted so the user can undo "reset
    to default" too.

    Returns the updated ``UserTarget`` or ``None`` if no row exists for
    this (user, target) pairing.
    """
    current = get_user_target(supabase, user_id, target_id)
    if current is None:
        return None
    updates: dict[str, Any] = {
        "axis_weights": weights.model_dump() if weights is not None else None,
        "axis_weights_previous": (
            current.axis_weights.model_dump() if current.axis_weights is not None else None
        ),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    resp = (
        supabase.table(USER_TARGETS_TABLE)
        .update(updates)
        .eq("user_id", user_id)
        .eq("target_id", target_id)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    return _parse_user_target(rows[0]) if rows else None


_NOTIFICATION_THRESHOLD_COLUMNS = ("job_score_threshold", "sms_score_threshold")


def set_user_target_notification_thresholds(
    supabase: Client,
    *,
    user_id: str,
    target_id: str,
    thresholds: dict[str, int | None],
) -> UserTarget | None:
    """Set this user-target pair's per-channel notification thresholds (#15).

    Partial update: only the channels **present** in ``thresholds`` are
    written, so editing one channel never clobbers the other. A key mapped
    to ``None`` is an explicit reset of that channel to the user-profile
    default (``notify.py`` reads target → profile fallback); an *omitted*
    key leaves the stored value untouched. An empty ``thresholds`` is a
    no-op that returns the current row unchanged. Does not re-grade —
    thresholds only gate which *new* matches alert, not the stored scores.

    Returns the updated (or unchanged) ``UserTarget`` or ``None`` if no row
    exists for this (user, target) pairing (the router 404s on None).
    """
    current = get_user_target(supabase, user_id, target_id)
    if current is None:
        return None
    updates: dict[str, Any] = {
        col: thresholds[col] for col in _NOTIFICATION_THRESHOLD_COLUMNS if col in thresholds
    }
    if not updates:
        return current
    updates["updated_at"] = datetime.now(UTC).isoformat()
    resp = (
        supabase.table(USER_TARGETS_TABLE)
        .update(updates)
        .eq("user_id", user_id)
        .eq("target_id", target_id)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    return _parse_user_target(rows[0]) if rows else None


_PREFERENCE_COLUMNS = (
    "pref_score_cutoff",
    "pref_locations",
    "pref_remote_ok",
    "pref_seniority_min",
    "pref_seniority_max",
    "pref_employment_types",
    "pref_include_unknown_salary",
)


def preferences_from_user_target(ut: UserTarget) -> TargetPreferences:
    """Project the per-user preference columns off a ``UserTarget`` row.

    Pure read-shape transform — the columns already live on ``user_targets``,
    so the GET endpoint reuses the single junction read instead of a second
    round-trip.
    """
    return TargetPreferences(
        pref_score_cutoff=ut.pref_score_cutoff,
        pref_locations=ut.pref_locations,
        pref_remote_ok=ut.pref_remote_ok,
        pref_seniority_min=cast(Any, ut.pref_seniority_min),
        pref_seniority_max=cast(Any, ut.pref_seniority_max),
        pref_employment_types=ut.pref_employment_types,
        pref_include_unknown_salary=ut.pref_include_unknown_salary,
    )


def get_user_target_preferences(
    supabase: Client, *, user_id: str, target_id: str
) -> TargetPreferences | None:
    """Return the calling user's preferences for a target, or ``None`` when no
    (user, target) link exists (the router 404s on None)."""
    ut = get_user_target(supabase, user_id, target_id)
    if ut is None:
        return None
    return preferences_from_user_target(ut)


def set_user_target_preferences(
    supabase: Client,
    *,
    user_id: str,
    target_id: str,
    preferences: TargetPreferences,
) -> TargetPreferences | None:
    """Replace the calling user's preferences for a target (#60).

    Full PUT replace: every preference column is written from ``preferences``
    (omitted fields already carry their model defaults), so the stored row is
    always a complete, self-describing set. Does NOT re-grade — preferences are
    a read-time filter over the shared, cached fit score.

    Returns the persisted ``TargetPreferences`` (re-projected off the updated
    row) or ``None`` when no (user, target) link exists.
    """
    current = get_user_target(supabase, user_id, target_id)
    if current is None:
        return None
    payload = preferences.model_dump()
    updates: dict[str, Any] = {col: payload[col] for col in _PREFERENCE_COLUMNS}
    updates["updated_at"] = datetime.now(UTC).isoformat()
    resp = (
        supabase.table(USER_TARGETS_TABLE)
        .update(updates)
        .eq("user_id", user_id)
        .eq("target_id", target_id)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    if not rows:
        return None
    return preferences_from_user_target(_parse_user_target(rows[0]))


def undo_user_target_axis_weights(
    supabase: Client, *, user_id: str, target_id: str
) -> UserTarget | None:
    """Revert ``axis_weights`` to ``axis_weights_previous``.

    Swaps the two columns (previous → current, current → previous).
    Idempotent in the sense that calling twice toggles back and forth —
    that's actually the intended behaviour: "Undo" then "Undo" returns
    to where you started.

    Returns ``None`` if the (user, target) row doesn't exist OR if
    there's no previous state to revert to (caller should 422).
    """
    current = get_user_target(supabase, user_id, target_id)
    if current is None:
        return None
    new_current = current.axis_weights_previous
    new_previous = current.axis_weights
    updates: dict[str, Any] = {
        "axis_weights": (new_current.model_dump() if new_current is not None else None),
        "axis_weights_previous": (new_previous.model_dump() if new_previous is not None else None),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    resp = (
        supabase.table(USER_TARGETS_TABLE)
        .update(updates)
        .eq("user_id", user_id)
        .eq("target_id", target_id)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    return _parse_user_target(rows[0]) if rows else None


# ---- Reference JD CRUD -----------------------------------------------------


def add_reference_jd(
    supabase: Client,
    target_id: str,
    jd_text: str,
    jd_url: str | None,
    extracted_profile: ScoringProfile,
    user_id: str | None = None,
) -> TargetReferenceJD:
    row = {
        "target_id": target_id,
        "user_id": user_id,
        "jd_text": jd_text,
        "jd_url": jd_url,
        "extracted_profile": extracted_profile.model_dump(),
    }
    resp = supabase.table(REF_JDS_TABLE).insert(row).execute()
    rows = cast(list[dict[str, Any]], resp.data or [])
    if not rows:
        raise RuntimeError("Failed to insert reference_jds row")
    return _parse_ref_jd(rows[0])


def list_reference_jds(supabase: Client, target_id: str) -> list[TargetReferenceJD]:
    resp = (
        supabase.table(REF_JDS_TABLE)
        .select("*")
        .eq("target_id", target_id)
        .order("created_at")
        .execute()
    )
    return [_parse_ref_jd(cast(dict[str, Any], r)) for r in (resp.data or [])]


def count_user_reference_jds(supabase: Client, *, target_id: str, user_id: str) -> int:
    """How many reference JDs this user has contributed to this target.
    Drives the per-user contribution cap (#47)."""
    resp = (
        supabase.table(REF_JDS_TABLE)
        # head=True → count only, no rows shipped (HEAD request).
        .select("id", count="exact", head=True)  # type: ignore[arg-type]
        .eq("target_id", target_id)
        .eq("user_id", user_id)
        .execute()
    )
    return resp.count or 0


def delete_reference_jd(
    supabase: Client, ref_jd_id: str, *, target_id: str, user_id: str | None = None
) -> bool:
    # target_id constrains the delete to the target the route already
    # ownership-checked — without it, any ref_jd_id across any target
    # would be deletable (IDOR, audit #24 F1).
    query = supabase.table(REF_JDS_TABLE).delete().eq("id", ref_jd_id).eq("target_id", target_id)
    # A regular JWT caller may only remove their OWN contribution (#5
    # refinement: "remove-your-own + re-merge", never hard-delete others').
    # user_id is None only on the operator/api-key path, which the route's
    # ownership guard also lets bypass — operators may remove any ref JD.
    if user_id is not None:
        query = query.eq("user_id", user_id)
    resp = query.execute()
    return bool(resp.data)
