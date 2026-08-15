"""#47 finding #4: the /jobs list separates the keyword placeholder from the
real Sonnet fit score.

``scores.score`` holds a cheap keyword placeholder while a row is
``stage1``/``stage2`` and the real fit score once it's ``complete``. Treating
them as one number let the ``min_score`` floor admit/exclude a job based only on
whether the daily grading cap happened to reach it. These tests pin the fix:

- the fit-score floor only judges graded rows; not-yet-graded ("Pending") rows
  are exempt and always shown,
- Pending rows sort below graded ones (a keyword 80 must not outrank a graded
  75), regardless of sort direction,
- each row carries a ``pending`` flag so the UI can badge it instead of showing
  the placeholder as a grade,
- the untargeted view's per-job dedup prefers a graded row over a Pending one,
- the status-tab counts stay consistent (the ``pipeline_counts`` RPC applies
  the same Pending-exempt floor as of migration 20260716050000, so floored
  counts ride it too).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.routers import jobs as jobs_mod
from app.routers.jobs import (
    _apply_score_floor,
    _is_pending,
    _list_jobs_across_user_targets,
    _list_jobs_for_target_two_query,
    _pipeline_counts_grouped,
    _prefer_score_row,
    _rank_graded_first,
)
from tests.support.fake_supabase import FakeResponse, two_query_supabase

# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


def test_is_pending_uses_grade_written_fields_not_status() -> None:
    # A genuine grade is signalled by the fields ONLY Phase 2's persist writes
    # (axis_scores / fit_reasoning, written atomically — prod-verified zero
    # rows carry one without the other). scoring_status is NOT trusted:
    # 'complete' is set on rows that were never actually graded (deferred /
    # reset), which leaked the keyword placeholder as a fit score (~2,300
    # such rows in prod).
    assert _is_pending({"fit_reasoning": "Title: matches. Gap: seniority."}) is False
    assert _is_pending({"axis_scores": {"title_fit": 80}}) is False
    assert _is_pending({"scoring_status": "stage1"}) is True
    assert _is_pending({"scoring_status": "stage2"}) is True
    assert _is_pending({}) is True  # nothing → Pending
    # THE regression: 'complete' with no real grade → Pending, never graded.
    assert _is_pending({"scoring_status": "complete"}) is True
    assert _is_pending({"scoring_status": "complete", "fit_reasoning": ""}) is True
    assert _is_pending({"scoring_status": "complete", "fit_reasoning": "  "}) is True
    assert _is_pending({"scoring_status": "complete", "axis_scores": {}}) is True
    assert _is_pending({"scoring_status": "complete", "axis_scores": None}) is True
    # A real grade → graded, regardless of the status text.
    assert _is_pending({"scoring_status": "complete", "fit_reasoning": "graded"}) is False
    assert _is_pending({"scoring_status": "complete", "axis_scores": {"title_fit": 9}}) is False


def test_is_pending_honors_precomputed_posting_flag() -> None:
    """Posting rows carry the overlay's precomputed ``pending`` bool (derived
    from the scores row's signal columns) — the filtered list branch ranks
    POSTING rows, which have neither axis_scores nor fit_reasoning, so the
    flag must win when present."""
    assert _is_pending({"pending": False, "title": "x"}) is False
    assert _is_pending({"pending": True, "title": "x"}) is True
    # A truthy-but-not-bool value must NOT be trusted as the flag.
    assert _is_pending({"pending": "yes"}) is True  # falls through, no signal


def test_pending_signal_columns_are_fetched_by_the_list_selects() -> None:
    """THE 2026-07-16 regression pin: ``_is_pending``'s PRIMARY signal column
    must be in the list paths' score select — the classifier once keyed only
    on ``fit_reasoning``, which the selects deliberately don't fetch (per-row
    prose × ~2k candidate rows), so EVERY list row classified Pending and the
    graded/Pending tiers collapsed into one recency-ordered lump (unsorted
    lists; empty ascending pages)."""
    from app.routers.jobs import _SCORE_ROW_COLS

    assert "axis_scores" in _SCORE_ROW_COLS
    # And the classifier prefers exactly that column: a row shaped like the
    # list select (axis_scores, NO fit_reasoning) must classify graded.
    assert _is_pending({"axis_scores": {"title_fit": 70}, "score": 70}) is False


class _RecordingQuery:
    """Captures the PostgREST filter calls ``_apply_score_floor`` makes."""

    def __init__(self) -> None:
        self.or_calls: list[str] = []
        self.gte_calls: list[tuple[str, int]] = []

    def or_(self, expr: str) -> _RecordingQuery:
        self.or_calls.append(expr)
        return self

    def gte(self, col: str, val: int) -> _RecordingQuery:
        self.gte_calls.append((col, val))
        return self


def test_apply_score_floor_exempts_pending_when_floored() -> None:
    q = _RecordingQuery()
    assert _apply_score_floor(q, 70) is q
    # Pending rows pass via the axis_scores leg; graded rows must clear the floor.
    # #665: the floor judges the AGED score — the same number the list shows.
    assert q.or_calls == ["axis_scores.is.null,recency_score.gte.70"]
    assert q.gte_calls == []  # never a flat floor that would hide Pending


def test_apply_score_floor_uses_the_same_signal_as_is_pending() -> None:
    """The floor's exemption and the Pending badge must not disagree.

    Regression for the 2026-08-08 prod defect: the predicate keyed on
    ``scoring_status != 'complete'``, which exempted 5,130 live rows that carry
    real ``axis_scores`` — the list rendered them as ordinary graded results
    with a numeric badge, so "Score 85+" returned rows scored 62.
    ``_is_pending`` is the single source of truth for "not yet graded"; the SQL
    predicate has to key on the same column it does.
    """
    q = _RecordingQuery()
    _apply_score_floor(q, 70)
    (expr,) = q.or_calls
    assert "scoring_status" not in expr, (
        "the floor is keyed on scoring_status again — _is_pending's own "
        "docstring says that column is unreliable, and the two disagreed on "
        "5,130 prod rows"
    )
    assert "axis_scores.is.null" in expr

    # The exempted population per this predicate must be exactly the population
    # _is_pending calls Pending. A stage1/stage2 row WITH axis_scores is graded.
    graded_but_not_complete = {
        "scoring_status": "stage1",
        "axis_scores": {"title_fit": 62},
        "score": 62,
    }
    assert _is_pending(graded_but_not_complete) is False, (
        "a stage1 row carrying axis_scores is graded, so the floor must apply "
        "to it — this is the row class the old predicate let through"
    )
    assert _is_pending({"scoring_status": "stage1", "score": 62}) is True


@pytest.mark.parametrize("floor", [None, 0])
def test_apply_score_floor_is_noop_without_a_floor(floor: int | None) -> None:
    q = _RecordingQuery()
    assert _apply_score_floor(q, floor) is q
    assert q.or_calls == [] and q.gte_calls == []


def test_rank_graded_first_buckets_pending_below_graded() -> None:
    rows = [
        {"id": "g50", "scoring_status": "complete", "fit_reasoning": "graded", "v": 50},
        {"id": "p80", "scoring_status": "stage2", "v": 80},
        {"id": "g70", "scoring_status": "complete", "fit_reasoning": "graded", "v": 70},
        {"id": "p10", "scoring_status": "stage1", "v": 10},
    ]
    ranked = _rank_graded_first(rows, value=lambda r: r["v"], ascending=False)
    # Graded first (by value desc), then Pending (by value desc) — the keyword
    # 80 never outranks the graded rows.
    assert [r["id"] for r in ranked] == ["g70", "g50", "p80", "p10"]


def test_rank_graded_first_keeps_pending_last_even_ascending() -> None:
    rows = [
        {"id": "g50", "scoring_status": "complete", "fit_reasoning": "graded", "v": 50},
        {"id": "p10", "scoring_status": "stage1", "v": 10},
        {"id": "g70", "scoring_status": "complete", "fit_reasoning": "graded", "v": 70},
    ]
    ranked = _rank_graded_first(rows, value=lambda r: r["v"], ascending=True)
    # Ascending sorts each bucket low→high, but Pending stays beneath graded.
    assert [r["id"] for r in ranked] == ["g50", "g70", "p10"]


def test_prefer_score_row_graded_beats_pending() -> None:
    graded_low = {"scoring_status": "complete", "fit_reasoning": "graded", "score": 40}
    pending_high = {"scoring_status": "stage2", "score": 95}
    # A real grade represents the job even when a placeholder is numerically higher.
    assert _prefer_score_row(graded_low, pending_high) is True
    assert _prefer_score_row(pending_high, graded_low) is False
    # Same gradedness → higher score wins.
    assert _prefer_score_row(
        {"scoring_status": "complete", "fit_reasoning": "graded", "score": 80},
        {"scoring_status": "complete", "fit_reasoning": "graded", "score": 70},
    )


# The ``scores``/``jobs`` two-query stubs live in tests.support.fake_supabase
# (shared with test_jobs_preferences_filter). ``two_query_supabase`` emulates
# the Pending-aware score floor + the jobs re-fetch.


# --------------------------------------------------------------------------
# Per-target list
# --------------------------------------------------------------------------


async def test_floor_drops_low_graded_but_keeps_low_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "recency_decay_enabled", False)
    scores = [
        {
            "job_posting_id": "g90",
            "score": 90,
            "score_breakdown": {},
            "scoring_status": "complete",
            "fit_reasoning": "graded",
        },
        {
            "job_posting_id": "g30",
            "score": 30,
            "score_breakdown": {},
            "scoring_status": "complete",
            "fit_reasoning": "graded",
        },
        {"job_posting_id": "p20", "score": 20, "score_breakdown": {}, "scoring_status": "stage2"},
    ]
    postings = {jid: {"id": jid, "title": jid} for jid in ("g90", "g30", "p20")}
    result = await _list_jobs_for_target_two_query(
        two_query_supabase(scores, postings),
        target_id="t-1",
        page_size=10,
        sort="score",
        ascending=False,
        min_score=50,
        status=None,
        company=None,
        search=None,
        exclude_terms=[],
        only_terms=[],
        cursor={},
    )
    ids = [p["id"] for p in result["postings"]]
    # g30 (graded, below floor) dropped; p20 (Pending) kept despite being below.
    assert "g30" not in ids
    assert ids == ["g90", "p20"]  # graded first, then Pending


async def test_pending_sorts_below_graded_and_is_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "recency_decay_enabled", False)
    scores = [
        {
            "job_posting_id": "g50",
            "score": 50,
            "score_breakdown": {},
            "scoring_status": "complete",
            "fit_reasoning": "graded",
        },
        {"job_posting_id": "p80", "score": 80, "score_breakdown": {}, "scoring_status": "stage2"},
        {
            "job_posting_id": "g70",
            "score": 70,
            "score_breakdown": {},
            "scoring_status": "complete",
            "fit_reasoning": "graded",
        },
    ]
    postings = {jid: {"id": jid, "title": jid} for jid in ("g50", "p80", "g70")}
    result = await _list_jobs_for_target_two_query(
        two_query_supabase(scores, postings),
        target_id="t-1",
        page_size=10,
        sort="score",
        ascending=False,
        min_score=None,
        status=None,
        company=None,
        search=None,
        exclude_terms=[],
        only_terms=[],
        cursor={},
    )
    # The keyword 80 does NOT outrank the graded rows.
    assert [p["id"] for p in result["postings"]] == ["g70", "g50", "p80"]
    by_id = {p["id"]: p for p in result["postings"]}
    assert by_id["p80"]["pending"] is True
    assert by_id["g70"]["pending"] is False


# --------------------------------------------------------------------------
# Untargeted (cross-target) list
# --------------------------------------------------------------------------


async def test_cross_target_dedup_prefers_graded_over_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "recency_decay_enabled", False)
    # Same job scored on two targets: a graded 60 and a Pending 90.
    scores = [
        {
            "job_posting_id": "j",
            "target_id": "t-1",
            "score": 60,
            "score_breakdown": {},
            "scoring_status": "complete",
            "fit_reasoning": "graded",
        },
        {
            "job_posting_id": "j",
            "target_id": "t-2",
            "score": 90,
            "score_breakdown": {},
            "scoring_status": "stage2",
        },
    ]
    postings = {"j": {"id": "j", "title": "j"}}
    result = await _list_jobs_across_user_targets(
        two_query_supabase(scores, postings),
        user_target_ids={"t-1", "t-2"},
        page_size=10,
        sort="score",
        ascending=False,
        min_score=None,
        status=None,
        company=None,
        search=None,
        exclude_terms=[],
        only_terms=[],
        cursor={},
    )
    row = result["postings"][0]
    # The graded representative wins, so the shown score is the real grade.
    assert row["score"] == 60
    assert row["pending"] is False


# --------------------------------------------------------------------------
# Status-tab counts stay consistent with the list
# --------------------------------------------------------------------------


async def test_counts_use_rpc_when_floored(monkeypatch: pytest.MonkeyPatch) -> None:
    # Floored counts ride the RPC since 20260716050000 taught it the Pending
    # exemption — the Python path is the mid-deploy fallback only. Forcing
    # Python here cost floored users ~2s of chunked round-trips per dashboard
    # load (2026-07-16 audit).
    sb = MagicMock()
    sb.rpc.return_value.execute = AsyncMock(
        return_value=FakeResponse([{"status": "new", "count": 7}])
    )
    monkeypatch.setattr(
        jobs_mod,
        "_pipeline_counts_python",
        lambda *a, **k: pytest.fail("floored counts must use the RPC, not the Python fallback"),
    )
    out = await _pipeline_counts_grouped(sb, target_ids={"t-1"}, min_score=70, user_id="u1")
    assert out == {"new": 7}
    sb.rpc.assert_called_once_with(
        "pipeline_counts",
        {"p_target_ids": ["t-1"], "p_min_score": 70, "p_user_id": "u1"},
    )


async def test_counts_use_rpc_when_unfloored() -> None:
    sb = MagicMock()
    sb.rpc.return_value.execute = AsyncMock(
        return_value=FakeResponse([{"status": "new", "count": 3}])
    )
    out = await _pipeline_counts_grouped(sb, target_ids={"t-1"}, min_score=None, user_id="u1")
    assert out == {"new": 3}
    sb.rpc.assert_called_once()  # no floor → keyset RPC fast path


# --------------------------------------------------------------------------
# 2026-07-16 regression: select-shaped rows must tier + sort correctly
# --------------------------------------------------------------------------


async def test_select_shaped_graded_rows_sort_by_score_not_recency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rows shaped EXACTLY like the list select fetches them (axis_scores
    present, no fit_reasoning) must rank as graded, by score — the regression
    had them all classified Pending and recency-ordered (newest ingest first,
    a 45 above a 78)."""
    monkeypatch.setattr(settings, "recency_decay_enabled", False)
    scores = [
        {
            "job_posting_id": "new45",
            "score": 45,
            "score_breakdown": {},
            "scoring_status": "complete",
            "axis_scores": {"title_fit": 45},
        },
        {
            "job_posting_id": "old78",
            "score": 78,
            "score_breakdown": {},
            "scoring_status": "complete",
            "axis_scores": {"title_fit": 78},
        },
        {
            "job_posting_id": "new68",
            "score": 68,
            "score_breakdown": {},
            "scoring_status": "complete",
            "axis_scores": {"title_fit": 68},
        },
    ]
    postings = {jid: {"id": jid, "title": jid} for jid in ("new45", "old78", "new68")}
    result = await _list_jobs_for_target_two_query(
        two_query_supabase(scores, postings),
        target_id="t-1",
        page_size=10,
        sort="score",
        ascending=False,
        min_score=None,
        status=None,
        company=None,
        search=None,
        exclude_terms=[],
        only_terms=[],
        cursor={},
    )
    assert [p["id"] for p in result["postings"]] == ["old78", "new68", "new45"]
    assert all(p["pending"] is False for p in result["postings"])


async def test_ascending_score_sort_returns_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """The observed symptom's second half: sort=score&order=asc returned an
    EMPTY page (all-Pending mis-tiering surfaced the oldest, dead-job rows
    into the first window). With correct tiering, ascending returns the
    graded rows lowest-first."""
    monkeypatch.setattr(settings, "recency_decay_enabled", False)
    scores = [
        {
            "job_posting_id": "a78",
            "score": 78,
            "score_breakdown": {},
            "scoring_status": "complete",
            "axis_scores": {"title_fit": 78},
        },
        {
            "job_posting_id": "b45",
            "score": 45,
            "score_breakdown": {},
            "scoring_status": "complete",
            "axis_scores": {"title_fit": 45},
        },
    ]
    postings = {jid: {"id": jid, "title": jid} for jid in ("a78", "b45")}
    result = await _list_jobs_for_target_two_query(
        two_query_supabase(scores, postings),
        target_id="t-1",
        page_size=10,
        sort="score",
        ascending=True,
        min_score=None,
        status=None,
        company=None,
        search=None,
        exclude_terms=[],
        only_terms=[],
        cursor={},
    )
    assert [p["id"] for p in result["postings"]] == ["b45", "a78"]


# --------------------------------------------------------------------------
# Scores-layer liveness join (dead rows must not eat page slots)
# --------------------------------------------------------------------------


class _LiveJoinRecorder:
    def __init__(self) -> None:
        self.selects: list[str] = []
        self.is_calls: list[tuple[str, str]] = []
        self._neg = False

    def table(self, _name: str) -> _LiveJoinRecorder:
        return self

    def select(self, cols: str) -> _LiveJoinRecorder:
        self.selects.append(cols)
        return self

    def eq(self, *_a: object, **_k: object) -> _LiveJoinRecorder:
        return self

    def in_(self, *_a: object, **_k: object) -> _LiveJoinRecorder:
        return self

    def or_(self, *_a: object, **_k: object) -> _LiveJoinRecorder:
        return self

    @property
    def not_(self) -> _LiveJoinRecorder:
        self._neg = True
        return self

    def is_(self, col: str, val: str) -> _LiveJoinRecorder:
        self.is_calls.append((("not." if self._neg else "") + col, val))
        self._neg = False
        return self

    async def execute(self) -> FakeResponse:
        return FakeResponse([])


async def test_live_view_applies_jobs_inner_join() -> None:
    sb = MagicMock()
    rec = _LiveJoinRecorder()
    sb.table.return_value = rec
    await _list_jobs_for_target_two_query(
        sb,
        target_id="t-1",
        page_size=10,
        sort="score",
        ascending=False,
        min_score=None,
        status=None,
        company=None,
        search=None,
        exclude_terms=[],
        only_terms=[],
        cursor={},
    )
    # The embed also rides the gate + recency columns (perf: kills the
    # per-request chunked role_family/posted-date reads), and since #654 the
    # deterministic salary columns too, so the logistics filter can run at
    # the scores layer instead of forcing a full hydration.
    #
    # Asserted per-column rather than as one literal: the column ORDER is not
    # the invariant — the join type and the ride-along set are.
    embed_selects = [s for s in rec.selects if "jobs!inner(" in s]
    assert embed_selects, "live view must inner-join jobs at the scores layer"
    for col in (
        "id",
        "role_family",
        "source_posted_at",
        "cataloged_at",
        "salary_min",
        "salary_max",
        "salary_currency",
        "salary_period",
    ):
        assert any(col in s for s in embed_selects), "embed lost " + col
    assert ("jobs.archived_at", "null") in rec.is_calls
    assert ("jobs.purged_at", "null") in rec.is_calls
    assert ("not.jobs.is_us", "false") in rec.is_calls


async def test_archived_view_keeps_archived_jobs() -> None:
    """status=archived must NOT liveness-join at the scores layer — the
    archived view exists to show archived jobs."""
    sb = MagicMock()
    rec = _LiveJoinRecorder()
    sb.table.return_value = rec
    await _list_jobs_for_target_two_query(
        sb,
        target_id="t-1",
        page_size=10,
        sort="score",
        ascending=False,
        min_score=None,
        status="archived",
        company=None,
        search=None,
        exclude_terms=[],
        only_terms=[],
        cursor={},
    )
    assert not any("jobs!inner" in s for s in rec.selects)
    assert rec.is_calls == []


async def test_filtered_path_keeps_graded_first_and_score_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE 2026-07-16 follow-up regression: with a location/preference filter
    active, ranking runs over POSTING rows (post-fetch branch) — the graded
    tier collapsed into Pending and the list went recency-ordered. Rows shaped
    like production (scores carry axis_scores; postings carry the overlay
    flag) must stay graded-first, score-descending, Pending below."""
    monkeypatch.setattr(settings, "recency_decay_enabled", False)
    scores = [
        {
            "job_posting_id": "g65",
            "score": 65,
            "score_breakdown": {},
            "scoring_status": "complete",
            "axis_scores": {"title_fit": 65},
        },
        {"job_posting_id": "p1", "score": 30, "score_breakdown": {}, "scoring_status": "stage2"},
        {
            "job_posting_id": "g78",
            "score": 78,
            "score_breakdown": {},
            "scoring_status": "complete",
            "axis_scores": {"title_fit": 78},
        },
        {
            "job_posting_id": "g72",
            "score": 72,
            "score_breakdown": {},
            "scoring_status": "complete",
            "axis_scores": {"title_fit": 72},
        },
    ]
    postings = {
        jid: {"id": jid, "title": jid, "location": "Remote, US"}
        for jid in ("g65", "p1", "g78", "g72")
    }
    result = await _list_jobs_for_target_two_query(
        two_query_supabase(scores, postings),
        target_id="t-1",
        page_size=10,
        sort="score",
        ascending=False,
        min_score=None,
        status=None,
        company=None,
        search=None,
        exclude_terms=[],
        only_terms=["remote"],
        cursor={},
    )
    ids = [p["id"] for p in result["postings"]]
    # Graded by score desc, Pending strictly last — never interleaved.
    assert ids == ["g78", "g72", "g65", "p1"]
    assert [p["pending"] for p in result["postings"]] == [False, False, False, True]


async def test_embedded_columns_eliminate_gate_and_recency_roundtrips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Perf regression pin (2026-07-16, the 4-8s /jobs + dashboard renders):
    when the scores fetch embeds jobs(role_family, first_seen_at), the
    off-family gate and the Pending-recency sort must harvest those fields
    in-memory — ZERO follow-up jobs-table reads beyond the single page
    fetch. On a ~2k-row candidate set the old chunked re-reads were ~20+
    sequential round-trips per request."""
    monkeypatch.setattr(settings, "recency_decay_enabled", False)

    jobs_selects: list[str] = []

    class _JobsRec:
        def select(self, cols: str) -> _JobsRec:
            jobs_selects.append(cols)
            return self

        def __getattr__(self, name: str):
            if name == "not_":
                return self

            def _f(*_a: object, **_k: object) -> _JobsRec:
                return self

            return _f

        async def execute(self) -> FakeResponse:
            return FakeResponse([{"id": "j1", "title": "j1"}, {"id": "j2", "title": "j2"}])

    class _ScoresRec:
        def __getattr__(self, name: str):
            if name == "not_":
                return self

            def _f(*_a: object, **_k: object) -> _ScoresRec:
                return self

            return _f

        async def execute(self) -> FakeResponse:
            return FakeResponse(
                [
                    {
                        "job_posting_id": "j1",
                        "target_id": "t-1",
                        "score": 70,
                        "score_breakdown": {},
                        "scoring_status": "complete",
                        "axis_scores": {"title_fit": 70},
                        "jobs": {
                            "id": "j1",
                            "role_family": "engineering",
                            "cataloged_at": "2026-07-10T00:00:00Z",
                        },
                    },
                    {
                        "job_posting_id": "j2",
                        "target_id": "t-1",
                        "score": 50,
                        "score_breakdown": {},
                        "scoring_status": "complete",
                        "axis_scores": {"title_fit": 50},
                        "jobs": {
                            "id": "j2",
                            "role_family": "engineering",
                            "cataloged_at": "2026-07-11T00:00:00Z",
                        },
                    },
                ]
            )

    class _TargetsRec:
        def __getattr__(self, name: str):
            def _f(*_a: object, **_k: object) -> _TargetsRec:
                return self

            return _f

        async def execute(self) -> FakeResponse:
            return FakeResponse([{"id": "t-1", "role_family": "engineering"}])

    sb = MagicMock()
    sb.table.side_effect = lambda name: {
        "scores": _ScoresRec(),
        "jobs": _JobsRec(),
        "targets": _TargetsRec(),
    }[name]

    result = await _list_jobs_across_user_targets(
        sb,
        user_target_ids={"t-1"},
        page_size=10,
        sort="score",
        ascending=False,
        min_score=None,
        status=None,
        company=None,
        search=None,
        exclude_terms=[],
        only_terms=[],
        cursor={},
    )

    assert [p["id"] for p in result["postings"]] == ["j1", "j2"]
    # Exactly ONE jobs read: the page fetch. No role_family / first_seen
    # chunk reads.
    assert len(jobs_selects) == 1
    assert "role_family" not in jobs_selects[0].replace("jobs!inner", "")
    assert all("cataloged_at" not in s or "title" in s for s in jobs_selects)
