"""Re-score projection / learning-rate cap math (#5 P4).

``project_rescore`` re-scores a target's recent jobs under the current vs
patched profile and decides whether the patch is an outlier the learner should
stage rather than auto-apply. These pin that decision deterministically.
"""

from __future__ import annotations

from typing import Any

import pytest

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


def test_after_side_scores_with_next_search_keywords() -> None:
    """Copilot on #204: a reference-JD merge replaces search_keywords along
    with the profile, and keywords gate the role-title intent (a mismatching
    title is slashed). The projection must score the "after" side with the
    keywords that would actually be installed — an identical profile whose
    NEW keywords disown the target's whole job list is a massive movement,
    invisible if both sides were scored under the old keywords."""
    profile = _profile({"python": 3})

    # Control: keywords unchanged -> no movement at all.
    same = project_rescore(
        profile,
        profile,
        [_PY_JOB] * 12,
        **_kwargs(search_keywords=["python engineer"]),
    )
    assert same.jobs_moved == 0
    assert same.capped is False

    # The merge would install keywords that mismatch every job title ->
    # every job's title-intent gate flips, the whole list moves, capped.
    swapped = project_rescore(
        profile,
        profile,
        [_PY_JOB] * 12,
        **_kwargs(
            search_keywords=["python engineer"],
            next_search_keywords=["sales manager"],
        ),
    )
    assert swapped.jobs_moved == 12
    assert swapped.capped is True


# ---------------------------------------------------------------------------
# project_profile_impact_async (#57 PR-G2e-3) — the async twin: fetch the recent
# scored jobs on the pooled async client, then run the identical (pure)
# ``project_rescore``. Used by the LLM learner.
# ---------------------------------------------------------------------------


class _ProjResp:
    def __init__(self, data: Any) -> None:
        self.data = data


class _ProjQuery:
    def __init__(self, fake: _ProjSupabase, table: str) -> None:
        self._fake = fake
        self._table = table

    def select(self, *_a: Any, **_k: Any) -> _ProjQuery:
        return self

    def eq(self, *_a: Any, **_k: Any) -> _ProjQuery:
        return self

    def order(self, *_a: Any, **_k: Any) -> _ProjQuery:
        return self

    def limit(self, *_a: Any, **_k: Any) -> _ProjQuery:
        return self

    def in_(self, *_a: Any, **_k: Any) -> _ProjQuery:
        return self

    async def execute(self) -> _ProjResp:
        return _ProjResp(self._fake.rows.get(self._table, []))


class _ProjSupabase:
    """Awaitable-``execute`` fake standing in for the pooled ``AsyncClient``."""

    def __init__(self, *, scores: list[dict[str, Any]], jobs: list[dict[str, Any]]) -> None:
        self.rows = {"scores": scores, "jobs": jobs}

    def table(self, name: str) -> _ProjQuery:
        return _ProjQuery(self, name)


@pytest.mark.asyncio
async def test_project_profile_impact_async_fetches_and_projects() -> None:
    """End to end: the async twin awaits the recent-scored-jobs reads, then runs
    the deterministic ``project_rescore``. Adding a ``python`` negative that
    hard-excludes both scored jobs must register real score movement."""
    from app.services.targets.learning_projection import project_profile_impact_async

    sb = _ProjSupabase(
        scores=[{"job_posting_id": "j1"}, {"job_posting_id": "j2"}],
        jobs=[
            {"id": "j1", "title": "Python Engineer", "description_html": "<p>python</p>"},
            {"id": "j2", "title": "Python Engineer", "description_html": "<p>python</p>"},
        ],
    )
    prev = _profile({"python": 3}).model_dump()
    nxt = _profile({"python": 3}, negative=["python"]).model_dump()

    proj = await project_profile_impact_async(sb, "t", prev, nxt, ["python"])  # type: ignore[arg-type]

    assert proj is not None
    assert proj.jobs_considered == 2
    assert proj.max_abs_delta > 0  # the new negative moved both jobs' scores


@pytest.mark.asyncio
async def test_project_profile_impact_async_returns_none_without_scored_jobs() -> None:
    """No scored jobs to project against → None (the caller then applies the
    patch without a learning-rate check — nothing to over-churn yet)."""
    from app.services.targets.learning_projection import project_profile_impact_async

    sb = _ProjSupabase(scores=[], jobs=[])
    proj = await project_profile_impact_async(sb, "t", {}, {}, None)  # type: ignore[arg-type]
    assert proj is None
