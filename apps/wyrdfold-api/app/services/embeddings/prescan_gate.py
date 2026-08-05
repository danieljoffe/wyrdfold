"""Cosine(job, target) scores for Phase-2 grade-queue ORDERING.

What remains of the #60/#89/#90 pre-scan apparatus after the gate was
retired (R2, 2026-07-31 — see docs/decisions.md "retired by its own shadow
data"): live grades showed cosine is the strongest cheap predictor of the
eventual fit score (avg fit ~7→67 monotone), so the per-candidate cosine
still orders the daily grade cap; the ADMISSION gate, its per-target
thresholds, holdout, and shadow recorder are gone.

Reads are best-effort and fail open: a missing vector or read error only
degrades priority order — it can never change what gets admitted or spent.
"""

from __future__ import annotations

import ast
import logging
from typing import Any, cast

from supabase import AsyncClient

from app.models.embeddings import EmbeddingModelId
from app.models.targets import JobTarget
from app.services.embeddings.job_embeddings import DEFAULT_MODEL
from app.services.embeddings.prescan_calibration import cosine

logger = logging.getLogger(__name__)

JOB_EMBEDDINGS_TABLE = "job_embeddings"
TARGETS_TABLE = "targets"


def parse_vector(raw: Any) -> list[float] | None:
    """Coerce a pgvector cell into a ``list[float]`` (or None).

    PostgREST returns a ``vector`` column either as a JSON array (list) or as its
    text form ``"[0.1,0.2,...]"`` depending on client/version — handle both.
    Mirrors ``scripts/calibrate_prescan_threshold._parse_vector``.
    """
    if raw is None:
        return None
    if isinstance(raw, list):
        try:
            return [float(x) for x in raw]
        except (TypeError, ValueError):
            return None
    if isinstance(raw, str):
        try:
            parsed = ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            return None
        if isinstance(parsed, (list, tuple)):
            try:
                return [float(x) for x in parsed]
            except (TypeError, ValueError):
                return None
    return None


async def _fetch_job_vector(
    supabase: AsyncClient, *, job_id: str, model: str
) -> list[float] | None:
    """The cached vector for (job, model) from ``job_embeddings``, or None."""
    resp = await (
        supabase.table(JOB_EMBEDDINGS_TABLE)
        .select("embedding")
        .eq("job_posting_id", job_id)
        .eq("model", model)
        .limit(1)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    if not rows:
        return None
    return parse_vector(rows[0].get("embedding"))


async def _fetch_target_vector(supabase: AsyncClient, *, target_id: str) -> list[float] | None:
    """The target's query ``embedding`` from ``targets`` (None = not embedded).

    Read from the DB rather than the :class:`JobTarget` model because the
    model does not carry the embedding column.
    """
    resp = await (
        supabase.table(TARGETS_TABLE).select("embedding").eq("id", target_id).limit(1).execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    if not rows:
        return None
    return parse_vector(rows[0].get("embedding"))


# ``.in_()`` id lists ride the request URL, so the vectors read must chunk:
# ~150 UUIDs ~= 5.7KB stays under proxy URL limits AND httpx's own 64KB
# refusal (``InvalidURL: query too long`` — hit at backfill scale on
# 2026-07-15, where the gate's fail-closed contract silently deferred the
# whole batch). Matches the 100-200 sizing of the other in_-chunked reads.
_VECTOR_READ_CHUNK_SIZE = 150


async def _fetch_job_vectors_batch(
    supabase: AsyncClient, *, job_ids: list[str], model: str
) -> dict[str, list[float]]:
    """Cached vectors for many jobs, keyed by id (missing omitted).

    Chunked ``.in_()`` reads (URL-safe); one query per
    ``_VECTOR_READ_CHUNK_SIZE`` ids. Any read error propagates — the gate's
    callers are fail-CLOSED on infra errors by contract."""
    if not job_ids:
        return {}
    out: dict[str, list[float]] = {}
    for i in range(0, len(job_ids), _VECTOR_READ_CHUNK_SIZE):
        chunk = job_ids[i : i + _VECTOR_READ_CHUNK_SIZE]
        resp = await (
            supabase.table(JOB_EMBEDDINGS_TABLE)
            .select("job_posting_id, embedding")
            .in_("job_posting_id", chunk)
            .eq("model", model)
            .execute()
        )
        for r in cast(list[dict[str, Any]], resp.data or []):
            vec = parse_vector(r.get("embedding"))
            if vec is not None:
                out[str(r["job_posting_id"])] = vec
    return out


async def cosine_scores_batch(
    supabase: AsyncClient,
    target: JobTarget,
    job_ids: list[str],
    *,
    model: EmbeddingModelId = DEFAULT_MODEL,
) -> dict[str, float]:
    """Cosine(job, target) VALUES for many jobs of ONE target — for
    fit-predictive Phase-2 grading priority (#9).

    One target-vector read + one job-vectors batch read, returning the raw
    similarity per job so the runner can order the daily grade quota by the
    signal that actually predicts fit: live grades show avg fit climbing monotonically
    with cosine, while ``phase1_confidence`` does not correlate. A missing job
    vector, an un-embedded target, or a dim mismatch omits that job — the caller
    treats an absent score as lowest priority. Never raises (best-effort
    ordering; the sort just falls back to its tie-breakers).
    """
    ids = list(dict.fromkeys(j for j in job_ids if j))
    if not ids:
        return {}
    try:
        target_vec = await _fetch_target_vector(supabase, target_id=target.id)
        if target_vec is None:
            return {}
        job_vecs = await _fetch_job_vectors_batch(supabase, job_ids=ids, model=model)
        out: dict[str, float] = {}
        for jid in ids:
            jv = job_vecs.get(jid)
            if jv is not None and len(jv) == len(target_vec):
                out[jid] = cosine(jv, target_vec)
        return out
    except Exception:
        logger.exception("Pre-scan cosine scores failed for target %s", target.id)
        return {}
