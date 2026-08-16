"""The exempt Pending tail must not become the whole answer (#795).

`_apply_score_floor` exempts not-yet-graded rows so a grading backlog can't hide
promising jobs. The exemption was UNBOUNDED, and in prod that made "Score 85+"
indefensible: the target's best GRADED row scored 80, so zero rows cleared the
bar, and the page filled with 100 exempt rows scoring 4-58. Every row on screen
contradicted the filter the user had just set.
"""

from __future__ import annotations

from typing import Any

from app.routers.jobs import _PENDING_TAIL_CAP, _cap_pending_tail


def _graded(i: int, score: int) -> dict[str, Any]:
    return {"job_posting_id": f"g{i}", "recency_score": score, "axis_scores": {"title": score}}


def _pending(i: int, score: int) -> dict[str, Any]:
    """Ungraded: no axis_scores — the signal `_is_pending` keys on."""
    return {"job_posting_id": f"p{i}", "recency_score": score, "axis_scores": None}


def test_the_prod_case_a_floor_nothing_graded_clears() -> None:
    """100 exempt rows, no qualifying graded row: the page was ALL non-matching."""
    ranked = [_pending(i, 8) for i in range(100)]
    out = _cap_pending_tail(ranked)
    assert len(out) == _PENDING_TAIL_CAP
    # Still present — the tail is a signal that ungraded work exists, not zero.
    assert out, "capping to nothing would hide the grading backlog entirely"


def test_graded_matches_are_never_trimmed() -> None:
    """The cap only ever touches the exempt tail."""
    ranked = [_graded(i, 90) for i in range(40)] + [_pending(i, 8) for i in range(100)]
    out = _cap_pending_tail(ranked)
    assert sum(1 for r in out if r["axis_scores"]) == 40
    assert sum(1 for r in out if not r["axis_scores"]) == _PENDING_TAIL_CAP


def test_a_short_tail_is_left_alone() -> None:
    ranked = [_graded(i, 90) for i in range(5)] + [_pending(i, 8) for i in range(3)]
    assert _cap_pending_tail(ranked) == ranked


def test_order_is_preserved() -> None:
    """Ranking already put graded first; capping must not reshuffle."""
    ranked = [_graded(1, 90), _graded(2, 85)] + [_pending(i, 8) for i in range(20)]
    out = _cap_pending_tail(ranked)
    assert [r["job_posting_id"] for r in out[:2]] == ["g1", "g2"]
    assert [r["job_posting_id"] for r in out[2:]] == [f"p{i}" for i in range(_PENDING_TAIL_CAP)]


def test_no_pending_is_a_noop() -> None:
    ranked = [_graded(i, 90) for i in range(3)]
    assert _cap_pending_tail(ranked) == ranked
