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
