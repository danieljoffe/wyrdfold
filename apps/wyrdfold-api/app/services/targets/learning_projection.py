"""Project a ProfilePatch's impact before auto-applying it (#5 P4).

The learner's high-confidence patches auto-apply, but a single patch — a new
negative keyword, a demotion — can silently reshuffle a target's whole job
list. This re-scores the target's recent scored jobs under the current vs
patched profile (deterministic keyword scoring, no LLM) and reports how many
move materially. The learner uses ``capped`` to stage outlier patches for
review instead of applying them: a learning-rate cap.
"""

from __future__ import annotations

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


def project_rescore(
    prev_profile: ScoringProfile,
    next_profile: ScoringProfile,
    jobs: list[ScoredJobText],
    *,
    search_keywords: list[str] | None,
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
            search_keywords=search_keywords,
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
