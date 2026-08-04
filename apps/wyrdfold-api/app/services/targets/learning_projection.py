"""Project a ProfilePatch's impact before auto-applying it (#5 P4).

The learner's high-confidence patches auto-apply, but a single patch — a new
negative keyword, a demotion — can silently reshuffle a target's whole job
list. This re-scores the target's recent scored jobs under the current vs
patched profile (deterministic keyword scoring, no LLM) and reports how many
move materially. The learner uses ``capped`` to stage outlier patches for
review instead of applying them: a learning-rate cap.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

from supabase import AsyncClient, Client

from app.config import settings
from app.models.learning import RescoreProjection
from app.models.targets import ScoringProfile

# The keyword half of the blended score is the only part a profile patch can
# change (the LLM half needs a re-grade), so the projected displayed-score
# delta is the keyword-score delta scaled by the blend's keyword weight.
from app.services.analysis.scoring import _KEYWORD_WEIGHT
from app.services.jd_parser import parse_jd
from app.services.scoring import score_job_with_profile

# (title, description_html) for one scored job.
ScoredJobText = tuple[str, str]


def fetch_recent_scored_jobs(supabase: Client, target_id: str, limit: int) -> list[ScoredJobText]:
    """The (title, description_html) of a target's most recently scored jobs.

    Bounded by ``limit`` to keep the deterministic re-score projection cheap.
    Returns [] when the target has no scores yet (a brand-new target), which
    the caller treats as "nothing to project against".
    """
    score_resp = (
        supabase.table("scores")
        .select("job_posting_id")
        .eq("target_id", target_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    score_rows = cast(list[dict[str, Any]], score_resp.data or [])
    job_ids = [r["job_posting_id"] for r in score_rows if r.get("job_posting_id")]
    if not job_ids:
        return []
    jobs_resp = (
        supabase.table("jobs").select("id, title, description_html").in_("id", job_ids).execute()
    )
    job_rows = cast(list[dict[str, Any]], jobs_resp.data or [])
    return [(r.get("title") or "", r.get("description_html") or "") for r in job_rows]


async def fetch_recent_scored_jobs_async(
    supabase: AsyncClient, target_id: str, limit: int
) -> list[ScoredJobText]:
    """Async twin of :func:`fetch_recent_scored_jobs` (#57 PR-G2e-3): the same
    two bounded reads (recent scores → their jobs), awaited on the pooled async
    service client."""
    score_resp = await (
        supabase.table("scores")
        .select("job_posting_id")
        .eq("target_id", target_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    score_rows = cast(list[dict[str, Any]], score_resp.data or [])
    job_ids = [r["job_posting_id"] for r in score_rows if r.get("job_posting_id")]
    if not job_ids:
        return []
    jobs_resp = await (
        supabase.table("jobs").select("id, title, description_html").in_("id", job_ids).execute()
    )
    job_rows = cast(list[dict[str, Any]], jobs_resp.data or [])
    return [(r.get("title") or "", r.get("description_html") or "") for r in job_rows]


def project_profile_impact(
    supabase: Client,
    target_id: str,
    prev_profile: dict[str, Any],
    next_profile: dict[str, Any],
    search_keywords: list[str] | None,
    next_search_keywords: list[str] | None = None,
) -> RescoreProjection | None:
    """Project how much a profile change would move the target's scores.

    Works for any prev→next profile transition — a learner ProfilePatch or
    a reference-JD merge (#191 slice 1b) — since it just re-scores recent
    jobs under both profiles. Returns None when there are no scored jobs to
    project against — the caller then applies without a learning-rate check
    (nothing to over-churn yet).
    """
    jobs = fetch_recent_scored_jobs(supabase, target_id, settings.learning_rescore_sample_size)
    if not jobs:
        return None
    return project_rescore(
        ScoringProfile.model_validate(prev_profile or {}),
        ScoringProfile.model_validate(next_profile or {}),
        jobs,
        search_keywords=search_keywords,
        next_search_keywords=next_search_keywords,
        move_threshold=settings.learning_rescore_move_threshold,
        max_moved_fraction=settings.learning_rescore_max_moved_fraction,
        min_jobs=settings.learning_rescore_min_jobs,
    )


async def project_profile_impact_async(
    supabase: AsyncClient,
    target_id: str,
    prev_profile: dict[str, Any],
    next_profile: dict[str, Any],
    search_keywords: list[str] | None,
    next_search_keywords: list[str] | None = None,
) -> RescoreProjection | None:
    """Async twin of :func:`project_profile_impact` (#57 PR-G2e-3) for the LLM
    learner on the pooled async service client — the sync version stays for the
    reference-JD merge path in ``routers/targets.py`` (threaded).

    Fetches the recent scored jobs asynchronously, then runs the identical
    deterministic :func:`project_rescore` — the scoring is pure CPU over up to N
    JDs, so it is off-loaded to a thread (#107) rather than run on the loop the
    detached learner chain shares with interactive requests."""
    jobs = await fetch_recent_scored_jobs_async(
        supabase, target_id, settings.learning_rescore_sample_size
    )
    if not jobs:
        return None
    return await asyncio.to_thread(
        project_rescore,
        ScoringProfile.model_validate(prev_profile or {}),
        ScoringProfile.model_validate(next_profile or {}),
        jobs,
        search_keywords=search_keywords,
        next_search_keywords=next_search_keywords,
        move_threshold=settings.learning_rescore_move_threshold,
        max_moved_fraction=settings.learning_rescore_max_moved_fraction,
        min_jobs=settings.learning_rescore_min_jobs,
    )


def project_rescore(
    prev_profile: ScoringProfile,
    next_profile: ScoringProfile,
    jobs: list[ScoredJobText],
    *,
    search_keywords: list[str] | None,
    next_search_keywords: list[str] | None = None,
    move_threshold: int,
    max_moved_fraction: float,
    min_jobs: int,
) -> RescoreProjection:
    """Re-score ``jobs`` under both profiles and summarise the movement.

    A job "moves" when its projected blended score changes by at least
    ``move_threshold`` points. The patch is ``capped`` (an outlier the caller
    should stage rather than auto-apply) when at least ``min_jobs`` were
    considered AND more than ``max_moved_fraction`` of them moved — the
    ``min_jobs`` floor stops a brand-new target with little history from
    having its first patches blocked on noise.
    """
    # A profile change can travel with a keyword change (a reference-JD
    # merge replaces search_keywords too, #191 slice 1b) — score the
    # "after" side with the keywords that would actually be installed, or
    # the projection under/over-estimates movement (Copilot on #204). The
    # learner never changes keywords, so the default keeps both sides equal.
    after_keywords = next_search_keywords if next_search_keywords is not None else search_keywords
    moved = 0
    max_abs_delta = 0
    for title, description_html in jobs:
        # Parse the JD once, score both profiles against the same parse.
        parsed = parse_jd(description_html)
        before = score_job_with_profile(
            title,
            description_html,
            prev_profile,
            parsed_jd=parsed,
            search_keywords=search_keywords,
        ).score
        after = score_job_with_profile(
            title,
            description_html,
            next_profile,
            parsed_jd=parsed,
            search_keywords=after_keywords,
        ).score
        delta = abs(round(_KEYWORD_WEIGHT * (after - before)))
        max_abs_delta = max(max_abs_delta, delta)
        if delta >= move_threshold:
            moved += 1

    considered = len(jobs)
    moved_fraction = (moved / considered) if considered else 0.0
    capped = considered >= min_jobs and moved_fraction > max_moved_fraction

    return RescoreProjection(
        jobs_considered=considered,
        jobs_moved=moved,
        moved_fraction=round(moved_fraction, 4),
        max_abs_delta=max_abs_delta,
        move_threshold=move_threshold,
        max_moved_fraction=max_moved_fraction,
        capped=capped,
    )
