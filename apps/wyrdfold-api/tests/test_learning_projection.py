"""Re-score projection / learning-rate cap math (#5 P4).

``project_rescore`` re-scores a target's recent jobs under the current vs
patched profile and decides whether the patch is an outlier the learner should
stage rather than auto-apply. These pin that decision deterministically.
"""

from __future__ import annotations

from typing import Any

from app.models.targets import CategoryProfile, NegativeProfile, ScoringProfile
from app.services.targets.learning_projection import project_rescore


def _profile(core: dict[str, int], negative: list[str] | None = None) -> ScoringProfile:
    return ScoringProfile(
        categories={"core_skills": CategoryProfile(keywords=core, weight=2.0)},
        negative=NegativeProfile(keywords=negative or [], weight=-10.0),
    )


# A job whose title carries "python" — a "python" negative hard-excludes it.
_PY_JOB = ("Python Engineer", "<p>We use python and react every day.</p>")


def _kwargs(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "search_keywords": None,
        "move_threshold": 20,
        "max_moved_fraction": 0.30,
        "min_jobs": 10,
    }
    base.update(over)
    return base


def test_capped_when_a_new_negative_excludes_many_jobs() -> None:
    prev = _profile({"python": 3})
    # The patch adds "python" as a negative -> title match is a hard exclude.
    nxt = _profile({"python": 3}, negative=["python"])
    proj = project_rescore(prev, nxt, [_PY_JOB] * 12, **_kwargs())

    assert proj.jobs_considered == 12
    assert proj.jobs_moved == 12
    assert proj.moved_fraction == 1.0
    assert proj.max_abs_delta >= 20
    assert proj.capped is True


def test_not_capped_when_a_new_negative_is_irrelevant_to_the_list() -> None:
    prev = _profile({"python": 3})
    # "blockchain" appears in none of the jobs -> no score movement.
    nxt = _profile({"python": 3}, negative=["blockchain"])
    proj = project_rescore(prev, nxt, [_PY_JOB] * 12, **_kwargs())

    assert proj.jobs_moved == 0
    assert proj.max_abs_delta == 0
    assert proj.capped is False


def test_min_jobs_floor_prevents_capping_on_a_thin_history() -> None:
    prev = _profile({"python": 3})
    nxt = _profile({"python": 3}, negative=["python"])
    # Same total churn, but only 5 jobs of history — too thin to call outlier.
    proj = project_rescore(prev, nxt, [_PY_JOB] * 5, **_kwargs())

    assert proj.jobs_moved == 5
    assert proj.moved_fraction == 1.0
    assert proj.capped is False


def test_empty_job_list_is_not_capped() -> None:
    prev = _profile({"python": 3})
    nxt = _profile({"python": 3}, negative=["python"])
    proj = project_rescore(prev, nxt, [], **_kwargs())

    assert proj.jobs_considered == 0
    assert proj.moved_fraction == 0.0
    assert proj.capped is False
