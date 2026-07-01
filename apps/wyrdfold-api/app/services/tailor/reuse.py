"""Resume reuse within targets (#504).

When a target already has a generated resume for one job, check whether
a new job's JD is similar enough to reuse that resume instead of
generating from scratch.

Similarity is measured by keyword overlap from the target's scoring
profile — no embedding calls needed. If two JDs hit >= 70% of the same
profile keywords (Jaccard), the existing resume is cloned with zero LLM
cost.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from supabase import Client

from app.constants import resolve_owner
from app.models.tailor import TailoredResumeRecord
from app.models.targets import ScoringProfile
from app.services.experience.optimized import get_latest as get_latest_optimized
from app.services.tailor.persistence import insert_row, jd_hash

SIMILARITY_THRESHOLD = 0.70


def extract_profile_keywords(profile: ScoringProfile) -> set[str]:
    """Extract all keywords from all categories in a scoring profile.

    Returns lowercased keyword set for case-insensitive matching.
    """
    keywords: set[str] = set()
    for cat in profile.categories.values():
        keywords.update(k.lower() for k in cat.keywords)
    return keywords


def _keyword_hits(jd_text: str, keywords: set[str]) -> set[str]:
    """Which profile keywords appear in a JD (case-insensitive substring match)."""
    jd_lower = jd_text.lower()
    return {kw for kw in keywords if kw in jd_lower}


def jd_similarity(
    jd_a: str,
    jd_b: str,
    profile_keywords: set[str],
) -> float:
    """Jaccard similarity of keyword hits between two JDs.

    Returns 0.0–1.0. Higher means more similar.
    Returns 0.0 when no keywords hit either JD.
    """
    hits_a = _keyword_hits(jd_a, profile_keywords)
    hits_b = _keyword_hits(jd_b, profile_keywords)
    union = hits_a | hits_b
    if not union:
        return 0.0
    return len(hits_a & hits_b) / len(union)


def _current_master_created_at(supabase: Client, user_id: str | None) -> datetime | None:
    """``created_at`` of the user's CURRENT master optimized doc, or None.

    Best-effort: a lookup failure must never block reuse, so any error returns
    None (which disables the staleness check, falling back to the old behavior)."""
    try:
        master = get_latest_optimized(supabase, user_id)
    except Exception:
        return None
    return master.created_at if master else None


def _resume_predates_master(row: dict[str, Any], master_created_at: datetime | None) -> bool:
    """True if a candidate resume was generated BEFORE the current master doc
    version was created — i.e. it was built from an older master (#47).

    Reuse copies the source's payload/markdown/docx verbatim, so cloning a
    resume built from a since-edited master would silently ship pre-edit content
    (e.g. a fabrication the user has since corrected, a fixed date, removed
    content). We refuse those; the caller regenerates fresh. None master / an
    unparseable timestamp never refuses (don't drop a reuse on a guess)."""
    if master_created_at is None:
        return False
    raw = row.get("created_at")
    if not raw:
        return False
    try:
        created = (
            raw
            if isinstance(raw, datetime)
            else datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        )
        return created < master_created_at
    except (ValueError, TypeError):
        return False


# How many of the caller's most recent resumes we consider for reuse.
# Pulled first (cheap, user-scoped) and then filtered to the target via
# ``scores`` membership — keeps the ``.in_()`` list bounded regardless of
# how many postings the target has accumulated.
_RECENT_RESUMES_WINDOW = 50
_MAX_CANDIDATES = 10


def find_reusable_resume(
    supabase: Client,
    *,
    target_id: str,
    job_description: str,
    profile_keywords: set[str],
    user_id: str | None = None,
) -> TailoredResumeRecord | None:
    """Find an existing resume in the same target similar enough to reuse.

    The job ↔ target link lives in the ``scores`` table — ``jobs.target_id``
    is a vestigial column the poller never populates, so the previous
    implementation (``jobs.eq("target_id", …)``) always returned an empty
    id list and reuse silently never fired (same root cause as the #676
    ownership fixes). We now pull the caller's recent resumes and keep the
    ones whose posting carries a ``scores`` row for this target.

    Scoped to ``user_id`` so one user's resume is never cloned into
    another user's documents (``user_id=None`` matches operator-created
    rows, mirroring the batch-service convention).

    Returns the best match above SIMILARITY_THRESHOLD, or None.
    """
    docs_query = (
        supabase.table("documents")
        .select("*")
        .eq("document_type", "resume")
        .order("created_at", desc=True)
        .limit(_RECENT_RESUMES_WINDOW)
    )
    docs_query = docs_query.eq("user_id", resolve_owner(user_id))
    rows = cast(list[dict[str, Any]], docs_query.execute().data or [])
    posting_ids = list(
        {
            cast(str, r.get("job_posting_id"))
            for r in rows
            if r.get("job_posting_id")
        }
    )
    if not posting_ids:
        return None

    scores_resp = (
        supabase.table("scores")
        .select("job_posting_id")
        .eq("target_id", target_id)
        .in_("job_posting_id", posting_ids)
        .execute()
    )
    in_target = {
        cast(dict[str, Any], r)["job_posting_id"]
        for r in (scores_resp.data or [])
    }
    if not in_target:
        return None

    candidates = [
        r for r in rows if r.get("job_posting_id") in in_target
    ][:_MAX_CANDIDATES]

    # Resumes built from an older master doc version would clone stale,
    # pre-edit content — refuse them so the caller regenerates fresh (#47).
    master_created_at = _current_master_created_at(supabase, user_id)

    best_record: TailoredResumeRecord | None = None
    best_sim = 0.0

    for row in candidates:
        if _resume_predates_master(row, master_created_at):
            continue
        jd_snapshot = row.get("jd_snapshot", "")
        sim = jd_similarity(job_description, jd_snapshot, profile_keywords)
        if sim >= SIMILARITY_THRESHOLD and sim > best_sim:
            best_sim = sim
            best_record = TailoredResumeRecord.model_validate(row)

    return best_record


def clone_resume_for_job(
    supabase: Client,
    *,
    source: TailoredResumeRecord,
    job_posting_id: str,
    job_description: str,
    user_id: str | None,
) -> TailoredResumeRecord:
    """Create a new documents row that clones an existing resume.

    Same payload, same storage_path (docx), linked to a different
    job_posting. source_resume_id tracks the lineage. Zero LLM cost.
    """
    row: dict[str, Any] = {
        "user_id": user_id,
        "job_posting_id": job_posting_id,
        "document_type": source.document_type,
        "resume_type": source.resume_type,
        "jd_snapshot": job_description,
        "jd_snapshot_hash": jd_hash(job_description),
        "payload": source.payload,
        "payload_md": source.payload_md,
        "docx_payload_md_hash": source.docx_payload_md_hash,
        "storage_path": source.storage_path,
        "warnings": [*source.warnings, "reused_from_similar_job"],
        "model": source.model,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
        "latency_ms": 0,
        "source_resume_id": source.id,
    }
    return insert_row(supabase, row, payload_md=source.payload_md)
