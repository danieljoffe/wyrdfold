"""Cosine-gate decision for the pre-scan (#60, Phase 3 — SHADOW MODE).

Phase 1 (``job_embeddings.py``) caches one vector per JOB; Phase 2
(``target_embeddings.py`` + ``targets.prescan_cosine_threshold``) caches one
vector per TARGET plus a per-target cutoff. This module reads BOTH and computes
the would-be gate verdict — admit a job to the expensive per-target LLM grade
iff ``cosine(job_vec, target_vec) >= target.prescan_cosine_threshold``.

In THIS phase the verdict is OBSERVED ONLY: the poller logs it into
``prescan_shadow`` alongside the live keyword decision (the disagreement matrix,
#68) but keeps the keyword gate driving admission. Making cosine actually drive
admission (the "flip") is a LATER phase informed by the shadow data and is NOT
built here.

Fail-soft by construction: a missing job vector, a target with no embedding, or
a NULL ``prescan_cosine_threshold`` all yield ``(None, None)`` — "no opinion".
The shadow writer records that as a NULL cosine side, and a future flip MUST
treat a ``None`` verdict as admit-all (today's behavior), so an un-populated
spine never silently drops jobs. Reuses the ``cosine`` helper from
``prescan_calibration`` (the same math the calibration script uses).
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import logging
from typing import Any, cast

from supabase import Client

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
    supabase: Client, *, job_id: str, model: str
) -> list[float] | None:
    """The cached vector for (job, model) from ``job_embeddings``, or None."""
    resp = await asyncio.to_thread(
        lambda: (
            supabase.table(JOB_EMBEDDINGS_TABLE)
            .select("embedding")
            .eq("job_posting_id", job_id)
            .eq("model", model)
            .limit(1)
            .execute()
        )
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    if not rows:
        return None
    return parse_vector(rows[0].get("embedding"))


async def _fetch_target_gate(
    supabase: Client, *, target_id: str
) -> tuple[list[float] | None, float | None]:
    """The target's ``(embedding, prescan_cosine_threshold)`` from ``targets``.

    Read from the DB rather than the :class:`JobTarget` model because the model
    does not carry these pre-scan columns. Either part may be None (target not
    yet embedded / not yet calibrated).
    """
    resp = await asyncio.to_thread(
        lambda: (
            supabase.table(TARGETS_TABLE)
            .select("embedding, prescan_cosine_threshold")
            .eq("id", target_id)
            .limit(1)
            .execute()
        )
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    if not rows:
        return None, None
    vec = parse_vector(rows[0].get("embedding"))
    raw_thr = rows[0].get("prescan_cosine_threshold")
    threshold = float(raw_thr) if raw_thr is not None else None
    return vec, threshold


async def cosine_gate_decision(
    supabase: Client,
    *,
    job_id: str,
    target: JobTarget,
    model: EmbeddingModelId = DEFAULT_MODEL,
) -> tuple[float | None, bool | None]:
    """Compute the would-be cosine gate decision for one (job, target).

    Fetches the job's cached vector from ``job_embeddings``, the target's vector
    and ``prescan_cosine_threshold`` from ``targets``, and returns
    ``(cosine, admit)`` where ``admit = cosine >= threshold``.

    Fail-soft: returns ``(None, None)`` whenever the job vector, the target
    embedding, or the threshold is missing — i.e. the gate has "no opinion" and
    the shadow row's cosine side is NULL. Any unexpected error is swallowed and
    also yields ``(None, None)`` so the (best-effort) shadow caller can never be
    broken by this computation.

    OBSERVED ONLY in this phase — the returned verdict does not drive admission.
    """
    try:
        job_vec = await _fetch_job_vector(supabase, job_id=job_id, model=model)
        if job_vec is None:
            return None, None

        target_vec, threshold = await _fetch_target_gate(
            supabase, target_id=target.id
        )
        if target_vec is None or threshold is None:
            return None, None

        if len(job_vec) != len(target_vec):
            # Dimension mismatch (e.g. a stale model swap) — no usable verdict.
            logger.warning(
                "Pre-scan cosine gate: vector dim mismatch job=%s (%d) target=%s (%d)",
                job_id,
                len(job_vec),
                target.id,
                len(target_vec),
            )
            return None, None

        sim = cosine(job_vec, target_vec)
        return sim, sim >= threshold
    except Exception:
        logger.exception(
            "Pre-scan cosine gate failed for job %s / target %s", job_id, target.id
        )
        return None, None


async def _fetch_job_vectors_batch(
    supabase: Client, *, job_ids: list[str], model: str
) -> dict[str, list[float]]:
    """Cached vectors for many jobs in ONE query, keyed by id (missing omitted)."""
    if not job_ids:
        return {}
    resp = await asyncio.to_thread(
        lambda: (
            supabase.table(JOB_EMBEDDINGS_TABLE)
            .select("job_posting_id, embedding")
            .in_("job_posting_id", job_ids)
            .eq("model", model)
            .execute()
        )
    )
    out: dict[str, list[float]] = {}
    for r in cast(list[dict[str, Any]], resp.data or []):
        vec = parse_vector(r.get("embedding"))
        if vec is not None:
            out[str(r["job_posting_id"])] = vec
    return out


async def cosine_gate_admits_batch(
    supabase: Client,
    target: JobTarget,
    job_ids: list[str],
    *,
    model: EmbeddingModelId = DEFAULT_MODEL,
) -> dict[str, bool | None]:
    """Batch cosine-gate verdicts for many jobs of ONE target — the #90 flip.

    Returns ``{job_id: admit}`` where ``admit`` is ``True`` (cosine >=
    threshold), ``False`` (cosine < threshold), or ``None`` — "no opinion"
    (fail-open): the target isn't calibrated (no embedding / threshold), the job
    has no vector, or a dim mismatch. **Callers MUST treat ``None`` as admit** —
    an unpopulated spine never silently drops jobs.

    Costs one target read + one job-vectors read (cosines computed in Python),
    so a whole Phase-2 candidate batch is 2 queries, not N.
    """
    ids = list(dict.fromkeys(j for j in job_ids if j))
    if not ids:
        return {}
    try:
        target_vec, threshold = await _fetch_target_gate(supabase, target_id=target.id)
        if target_vec is None or threshold is None:
            # Target not calibrated → no opinion for any job (fail-open admit).
            return dict.fromkeys(ids, None)
        job_vecs = await _fetch_job_vectors_batch(supabase, job_ids=ids, model=model)
        out: dict[str, bool | None] = {}
        for jid in ids:
            jv = job_vecs.get(jid)
            if jv is None or len(jv) != len(target_vec):
                out[jid] = None  # missing / mismatched vector → fail-open admit
            else:
                out[jid] = cosine(jv, target_vec) >= threshold
        return out
    except Exception:
        logger.exception(
            "Pre-scan batch cosine gate failed for target %s", target.id
        )
        return dict.fromkeys(ids, None)  # fail-open on any error


async def cosine_scores_batch(
    supabase: Client,
    target: JobTarget,
    job_ids: list[str],
    *,
    model: EmbeddingModelId = DEFAULT_MODEL,
) -> dict[str, float]:
    """Cosine(job, target) VALUES for many jobs of ONE target — for
    fit-predictive Phase-2 grading priority (#9).

    Same two reads as :func:`cosine_gate_admits_batch` (one target vector, one
    job-vectors batch), but returns the raw similarity per job instead of the
    gate verdict, so the runner can order the daily grade quota by the signal
    that actually predicts fit: live grades show avg fit climbing monotonically
    with cosine, while ``phase1_confidence`` does not correlate. A missing job
    vector, an un-embedded target, or a dim mismatch omits that job — the caller
    treats an absent score as lowest priority. Never raises (best-effort
    ordering; the sort just falls back to its tie-breakers).
    """
    ids = list(dict.fromkeys(j for j in job_ids if j))
    if not ids:
        return {}
    try:
        target_vec, _threshold = await _fetch_target_gate(supabase, target_id=target.id)
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


def in_prescan_holdout(job_id: str, target_id: str, fraction: float) -> bool:
    """Deterministic membership in the #90 exploration holdout.

    A stable ~``fraction`` slice of (job, target) pairs, so a below-threshold
    job the gate would DROP is instead kept for grading — letting us measure the
    gate's false-negative rate (does a dropped job ever grade high?) against the
    shadow log. Deterministic (hash of the pair) so the same pair is always
    in/out — no per-cycle churn that would re-pay grades. ``fraction <= 0`` →
    never; ``>= 1`` → always.
    """
    if fraction <= 0:
        return False
    if fraction >= 1:
        return True
    digest = hashlib.sha256(f"{job_id}:{target_id}".encode()).hexdigest()
    return (int(digest[:8], 16) / 0xFFFFFFFF) < fraction
