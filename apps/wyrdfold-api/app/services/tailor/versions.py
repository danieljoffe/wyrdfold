"""Resume payload version history (F3-H).

Every `update_payload()` writes a snapshot row into `document_versions`
before mutating the live payload. We cap free-tier history at 5 most recent
versions — older snapshots get pruned. The cap is enforced in Python rather
than via a Postgres trigger so it's easy to test, easy to lift per user, and
visible to anyone reading the service module.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, cast

from pydantic import BaseModel
from supabase import AsyncClient

from app.services.db_read import fetch_one

VERSIONS_TABLE = "document_versions"

VersionSource = Literal["initial", "user_edit", "llm_adapt"]

FREE_TIER_VERSION_CAP = 5
"""Versions retained per resume on the free tier. Paid tiers can lift this."""


class ResumeVersion(BaseModel):
    id: str
    resume_id: str
    payload: dict[str, Any]
    source: VersionSource
    created_at: datetime

    model_config = {"extra": "ignore"}


async def record(
    supabase: AsyncClient,
    *,
    resume_id: str,
    payload: dict[str, Any],
    source: VersionSource,
    payload_md: str | None = None,
) -> None:
    """Insert a version snapshot then prune anything beyond the free-tier cap.

    Failures are best-effort — never let a versioning hiccup break the live
    payload write that the caller is doing alongside this. The caller catches
    exceptions; here we only raise if the insert itself fails.
    """
    row: dict[str, Any] = {
        "resume_id": resume_id,
        "payload": payload,
        "source": source,
    }
    if payload_md is not None:
        row["payload_md"] = payload_md
    await supabase.table(VERSIONS_TABLE).insert(row).execute()
    await _prune(supabase, resume_id=resume_id, keep=FREE_TIER_VERSION_CAP)


async def checkpoint(supabase: AsyncClient, resume_id: str) -> bool:
    """Snapshot the resume's current payload_md as a 'user_edit' version,
    deduped against the most recent version's payload_md.

    Returns True if a new version row was written, False if the current
    markdown already matches the latest version (or the row is missing).

    Called by the session-end flush, before approve, and before re-adapt
    so users can roll back meaningful checkpoints. Routine PATCH /save
    calls do NOT snapshot — that would flood the free-tier cap within
    minutes of typing.
    """
    # fetch_one, not .single(): the latter raises on zero rows, so this
    # function's documented "returns False if the row is missing" was a lie.
    row = await fetch_one(
        supabase.table("documents").select("payload, payload_md").eq("id", resume_id)
    )
    if row is None:
        return False
    current_md = row.get("payload_md")
    current_payload = row.get("payload") or {}

    last_resp = await (
        supabase.table(VERSIONS_TABLE)
        .select("payload_md")
        .eq("resume_id", resume_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    last_rows = cast(list[dict[str, Any]], last_resp.data or [])
    if last_rows and last_rows[0].get("payload_md") == current_md:
        return False

    await record(
        supabase,
        resume_id=resume_id,
        payload=current_payload,
        source="user_edit",
        payload_md=current_md,
    )
    return True


async def list_for_resume(supabase: AsyncClient, resume_id: str) -> list[ResumeVersion]:
    """Most recent versions first. Capped at FREE_TIER_VERSION_CAP by storage."""
    resp = await (
        supabase.table(VERSIONS_TABLE)
        .select("*")
        .eq("resume_id", resume_id)
        .order("created_at", desc=True)
        .limit(FREE_TIER_VERSION_CAP)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    return [ResumeVersion.model_validate(r) for r in rows]


async def _prune(supabase: AsyncClient, *, resume_id: str, keep: int) -> None:
    """Delete oldest versions beyond `keep`. Two-step (read ids, delete) keeps
    us in PostgREST without needing a custom RPC.
    """
    resp = await (
        supabase.table(VERSIONS_TABLE)
        .select("id")
        .eq("resume_id", resume_id)
        .order("created_at", desc=True)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    if len(rows) <= keep:
        return
    expired_ids = [r["id"] for r in rows[keep:]]
    await supabase.table(VERSIONS_TABLE).delete().in_("id", expired_ids).execute()
