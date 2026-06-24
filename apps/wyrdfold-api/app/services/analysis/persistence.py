"""Cache CRUD for analyses table.

The cache key is (job_posting_id, target_id, optimized_doc_id, user_id) so
that re-deriving the master doc or switching targets naturally invalidates
prior rows, and one user's analysis never leaks into another's view.

``user_id`` is load-bearing: the cron poller and the user-facing
``POST /analysis`` flow both write here, and they MUST agree on the tenant
or the same (job, target, optimized) work gets computed twice. The poller
stamps the optimized doc's owning user (``optimized_doc.user_id``) — the
same value the router resolves from the caller's JWT — so each
(job, target, optimized version, owner) combination runs the LLM at most
once across both paths.

``optimized_doc_id`` already encodes the owner (the unique constraint
``experience_optimized_docs_user_id_version_key`` ties a doc to one user),
so ``user_id`` is technically redundant with it for non-NULL owners — but
it's kept in the key as a tenant-isolation backstop and to keep the
upsert conflict target aligned with the row's natural identity.
"""

from __future__ import annotations

from typing import Any, cast

from supabase import Client

from app.models.analysis import JobAnalysis, JobAnalysisRecord
from app.models.llm import LLMResult

TABLE = "analyses"


def get_cached(
    supabase: Client,
    job_posting_id: str,
    *,
    target_id: str,
    optimized_doc_id: str,
    user_id: str | None,
) -> JobAnalysisRecord | None:
    """Return the cached analysis for this (job, target, optimized) combo, or None."""
    query = (
        supabase.table(TABLE)
        .select("*")
        .eq("job_posting_id", job_posting_id)
        .eq("target_id", target_id)
        .eq("optimized_doc_id", optimized_doc_id)
        .order("created_at", desc=True)
        .limit(1)
    )
    query = query.is_("user_id", "null") if user_id is None else query.eq("user_id", user_id)
    resp = query.execute()
    rows = cast(list[dict[str, Any]], resp.data or [])
    if not rows:
        return None
    return JobAnalysisRecord.model_validate(rows[0])


# Conflict target for the idempotent upsert below. Must match the columns
# of the ``analyses_cache_key_unique`` constraint (see migration
# 20260623140000) exactly, in order. The DB constraint is declared
# ``NULLS NOT DISTINCT`` so legacy ``user_id IS NULL`` rows dedup too.
_CACHE_KEY_COLS = "job_posting_id,target_id,optimized_doc_id,user_id"


def persist(
    supabase: Client,
    *,
    job_posting_id: str,
    target_id: str,
    user_id: str | None,
    optimized_doc_id: str | None,
    analysis: JobAnalysis,
    llm_result: LLMResult,
) -> JobAnalysisRecord:
    """Upsert one analyses row, keyed on the cache key.

    Idempotent on ``(job_posting_id, target_id, optimized_doc_id,
    user_id)``: two near-simultaneous computations of the same analysis
    (e.g. a quick navigate-away-and-back before the first POST persisted,
    or a cron pass racing a user view) collapse onto one row instead of
    duplicating the work into multiple rows. Without this, a race produced
    a second ``analyses`` row that wasted an LLM call AND left stale
    duplicates the ``created_at DESC`` cache read had to skip past.

    Relies on the ``analyses_cache_key_unique`` DB constraint as the
    conflict target. On conflict the existing row is overwritten with the
    fresh scorecard/cost — a re-analysis with identical inputs is a no-op
    in content but refreshes the stored cost/latency to the latest run.
    """
    row: dict[str, Any] = {
        "job_posting_id": job_posting_id,
        "target_id": target_id,
        "user_id": user_id,
        "optimized_doc_id": optimized_doc_id,
        "scorecard": analysis.scorecard.model_dump(mode="json"),
        "recommendation": analysis.recommendation,
        "model": llm_result.model,
        "cost_usd": llm_result.cost_usd,
        "latency_ms": llm_result.latency_ms,
    }
    resp = supabase.table(TABLE).upsert(row, on_conflict=_CACHE_KEY_COLS).execute()
    rows = cast(list[dict[str, Any]], resp.data or [])
    if not rows:
        raise RuntimeError("Failed to upsert analyses row")
    return JobAnalysisRecord.model_validate(rows[0])
