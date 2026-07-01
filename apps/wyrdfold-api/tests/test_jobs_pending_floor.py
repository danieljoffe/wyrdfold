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
- the status-tab counts stay consistent (floored counts use the Pending-aware
  Python path, not the flat-floor RPC).
"""

from __future__ import annotations

from unittest.mock import MagicMock

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


def test_is_pending_only_complete_is_graded() -> None:
    assert _is_pending({"scoring_status": "stage1"}) is True
    assert _is_pending({"scoring_status": "stage2"}) is True
    assert _is_pending({}) is True  # missing status → Pending
    assert _is_pending({"scoring_status": "complete"}) is False


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
    # Pending rows pass via the status legs; graded rows must clear the floor.
    assert q.or_calls == [
        "scoring_status.is.null,scoring_status.neq.complete,score.gte.70"
    ]
    assert q.gte_calls == []  # never a flat floor that would hide Pending


@pytest.mark.parametrize("floor", [None, 0])
def test_apply_score_floor_is_noop_without_a_floor(floor: int | None) -> None:
    q = _RecordingQuery()
    assert _apply_score_floor(q, floor) is q
    assert q.or_calls == [] and q.gte_calls == []


def test_rank_graded_first_buckets_pending_below_graded() -> None:
    rows = [
        {"id": "g50", "scoring_status": "complete", "v": 50},
        {"id": "p80", "scoring_status": "stage2", "v": 80},
        {"id": "g70", "scoring_status": "complete", "v": 70},
        {"id": "p10", "scoring_status": "stage1", "v": 10},
    ]
    ranked = _rank_graded_first(rows, value=lambda r: r["v"], ascending=False)
    # Graded first (by value desc), then Pending (by value desc) — the keyword
    # 80 never outranks the graded rows.
    assert [r["id"] for r in ranked] == ["g70", "g50", "p80", "p10"]


def test_rank_graded_first_keeps_pending_last_even_ascending() -> None:
    rows = [
        {"id": "g50", "scoring_status": "complete", "v": 50},
        {"id": "p10", "scoring_status": "stage1", "v": 10},
        {"id": "g70", "scoring_status": "complete", "v": 70},
    ]
    ranked = _rank_graded_first(rows, value=lambda r: r["v"], ascending=True)
    # Ascending sorts each bucket low→high, but Pending stays beneath graded.
    assert [r["id"] for r in ranked] == ["g50", "g70", "p10"]


def test_prefer_score_row_graded_beats_pending() -> None:
    graded_low = {"scoring_status": "complete", "score": 40}
    pending_high = {"scoring_status": "stage2", "score": 95}
    # A real grade represents the job even when a placeholder is numerically higher.
    assert _prefer_score_row(graded_low, pending_high) is True
    assert _prefer_score_row(pending_high, graded_low) is False
    # Same gradedness → higher score wins.
    assert _prefer_score_row(
        {"scoring_status": "complete", "score": 80},
        {"scoring_status": "complete", "score": 70},
    )


# The ``scores``/``jobs`` two-query stubs live in tests.support.fake_supabase
# (shared with test_jobs_preferences_filter). ``two_query_supabase`` emulates
# the Pending-aware score floor + the jobs re-fetch.


# --------------------------------------------------------------------------
# Per-target list
# --------------------------------------------------------------------------


def test_floor_drops_low_graded_but_keeps_low_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "recency_decay_enabled", False)
    scores = [
        {"job_posting_id": "g90", "score": 90, "score_breakdown": {}, "scoring_status": "complete"},
        {"job_posting_id": "g30", "score": 30, "score_breakdown": {}, "scoring_status": "complete"},
        {"job_posting_id": "p20", "score": 20, "score_breakdown": {}, "scoring_status": "stage2"},
    ]
    postings = {jid: {"id": jid, "title": jid} for jid in ("g90", "g30", "p20")}
    result = _list_jobs_for_target_two_query(
        two_query_supabase(scores, postings),
        target_id="t-1", page_size=10, sort="score", ascending=False,
        min_score=50, status=None, company=None, search=None,
        exclude_terms=[], only_terms=[], cursor={},
    )
    ids = [p["id"] for p in result["postings"]]
    # g30 (graded, below floor) dropped; p20 (Pending) kept despite being below.
    assert "g30" not in ids
    assert ids == ["g90", "p20"]  # graded first, then Pending


def test_pending_sorts_below_graded_and_is_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "recency_decay_enabled", False)
    scores = [
        {"job_posting_id": "g50", "score": 50, "score_breakdown": {}, "scoring_status": "complete"},
        {"job_posting_id": "p80", "score": 80, "score_breakdown": {}, "scoring_status": "stage2"},
        {"job_posting_id": "g70", "score": 70, "score_breakdown": {}, "scoring_status": "complete"},
    ]
    postings = {jid: {"id": jid, "title": jid} for jid in ("g50", "p80", "g70")}
    result = _list_jobs_for_target_two_query(
        two_query_supabase(scores, postings),
        target_id="t-1", page_size=10, sort="score", ascending=False,
        min_score=None, status=None, company=None, search=None,
        exclude_terms=[], only_terms=[], cursor={},
    )
    # The keyword 80 does NOT outrank the graded rows.
    assert [p["id"] for p in result["postings"]] == ["g70", "g50", "p80"]
    by_id = {p["id"]: p for p in result["postings"]}
    assert by_id["p80"]["pending"] is True
    assert by_id["g70"]["pending"] is False


# --------------------------------------------------------------------------
# Untargeted (cross-target) list
# --------------------------------------------------------------------------


def test_cross_target_dedup_prefers_graded_over_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "recency_decay_enabled", False)
    # Same job scored on two targets: a graded 60 and a Pending 90.
    scores = [
        {"job_posting_id": "j", "target_id": "t-1", "score": 60, "score_breakdown": {}, "scoring_status": "complete"},
        {"job_posting_id": "j", "target_id": "t-2", "score": 90, "score_breakdown": {}, "scoring_status": "stage2"},
    ]
    postings = {"j": {"id": "j", "title": "j"}}
    result = _list_jobs_across_user_targets(
        two_query_supabase(scores, postings),
        user_target_ids={"t-1", "t-2"}, page_size=10, sort="score", ascending=False,
        min_score=None, status=None, company=None, search=None,
        exclude_terms=[], only_terms=[], cursor={},
    )
    row = result["postings"][0]
    # The graded representative wins, so the shown score is the real grade.
    assert row["score"] == 60
    assert row["pending"] is False


# --------------------------------------------------------------------------
# Status-tab counts stay consistent with the list
# --------------------------------------------------------------------------


def test_counts_skip_rpc_when_floored(monkeypatch: pytest.MonkeyPatch) -> None:
    sb = MagicMock()
    sb.rpc.side_effect = AssertionError("flat-floor RPC must not run when a floor is set")
    monkeypatch.setattr(jobs_mod, "_pipeline_counts_python", lambda *a, **k: {"new": 7})
    out = _pipeline_counts_grouped(sb, target_ids={"t-1"}, min_score=70, user_id="u1")
    assert out == {"new": 7}  # Pending-aware Python path
    sb.rpc.assert_not_called()


def test_counts_use_rpc_when_unfloored() -> None:
    sb = MagicMock()
    sb.rpc.return_value.execute.return_value = FakeResponse([{"status": "new", "count": 3}])
    out = _pipeline_counts_grouped(sb, target_ids={"t-1"}, min_score=None, user_id="u1")
    assert out == {"new": 3}
    sb.rpc.assert_called_once()  # no floor → keyset RPC fast path
