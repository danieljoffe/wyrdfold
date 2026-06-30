"""Regression tests for /jobs response ordering.

The score-sorted fast path computes ``page_ids`` in the correct order at
the scores layer, then re-fetches postings via ``.in_("id", page_ids)``.
Supabase's ``in_()`` does not preserve list order — without an explicit
re-sort, the postings come back in storage order and the API returns the
right rows in the wrong order.
"""

from typing import Any
from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.routers.jobs import (
    _decode_cursor,
    _encode_cursor,
    _list_jobs_across_user_targets,
    _list_jobs_for_target_rpc,
    _list_jobs_for_target_two_query,
)


class _Resp:
    def __init__(self, data: list[dict[str, Any]], count: int | None = None) -> None:
        self.data = data
        self.count = count


class _Chain:
    """Fluent stub — every builder method returns self; execute() returns the preloaded response."""

    def __init__(self, resp: _Resp) -> None:
        self._resp = resp

    def select(self, *_a: Any, **_kw: Any) -> "_Chain":
        return self

    def eq(self, *_a: Any, **_kw: Any) -> "_Chain":
        return self

    def neq(self, *_a: Any, **_kw: Any) -> "_Chain":
        return self

    def in_(self, *_a: Any, **_kw: Any) -> "_Chain":
        return self

    def is_(self, *_a: Any, **_kw: Any) -> "_Chain":
        return self

    def gte(self, *_a: Any, **_kw: Any) -> "_Chain":
        return self

    def ilike(self, *_a: Any, **_kw: Any) -> "_Chain":
        return self

    def order(self, *_a: Any, **_kw: Any) -> "_Chain":
        return self

    def range(self, *_a: Any, **_kw: Any) -> "_Chain":
        return self

    def execute(self) -> _Resp:
        return self._resp


def _supabase_with(table_resps: dict[str, _Resp]) -> MagicMock:
    sb = MagicMock()
    sb.table.side_effect = lambda name: _Chain(table_resps[name])
    return sb


def test_target_two_query_restores_score_desc_order() -> None:
    # Scores returned in score-desc order (highest first) — this is what
    # Supabase produces because the query chains .order("score", desc=True).
    ts_rows = [
        {"job_posting_id": "j-high", "score": 90, "score_breakdown": {}, "scoring_status": "complete"},
        {"job_posting_id": "j-mid", "score": 60, "score_breakdown": {}, "scoring_status": "complete"},
        {"job_posting_id": "j-low", "score": 30, "score_breakdown": {}, "scoring_status": "complete"},
    ]
    # Postings returned by .in_("id", page_ids) in DIFFERENT (storage) order.
    # The bug: without an explicit re-sort, the API returned this order.
    postings_in_storage_order = [
        {"id": "j-low", "title": "low"},
        {"id": "j-high", "title": "high"},
        {"id": "j-mid", "title": "mid"},
    ]
    sb = _supabase_with(
        {"scores": _Resp(ts_rows, count=3), "jobs": _Resp(postings_in_storage_order)}
    )

    result = _list_jobs_for_target_two_query(
        sb,
        target_id="t-1",
        cursor={},
        page_size=10,
        sort="score",
        ascending=False,
        min_score=None,
        status=None,
        company=None,
        search=None,
        exclude_terms=[],
        only_terms=[],
    )

    assert [p["id"] for p in result["postings"]] == ["j-high", "j-mid", "j-low"]
    assert [p["score"] for p in result["postings"]] == [90, 60, 30]
    assert result["total"] == 3


def test_across_user_targets_restores_score_desc_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pin recency decay OFF: this tests raw-score-desc ordering, which the
    # recency feature replaces with recency_score ordering when on. The flag
    # defaults off in CI but RECENCY_DECAY_ENABLED=true in .env.local (loaded
    # by nx) would otherwise sort by an absent recency_score → storage order.
    monkeypatch.setattr(settings, "recency_decay_enabled", False)
    # Same shape, different aggregator (max score across the user's targets).
    score_rows = [
        {"job_posting_id": "j-high", "target_id": "t-1", "score": 80, "score_breakdown": {}, "scoring_status": "complete"},
        {"job_posting_id": "j-mid", "target_id": "t-1", "score": 50, "score_breakdown": {}, "scoring_status": "complete"},
        {"job_posting_id": "j-low", "target_id": "t-2", "score": 20, "score_breakdown": {}, "scoring_status": "complete"},
    ]
    postings_in_storage_order = [
        {"id": "j-mid", "title": "mid"},
        {"id": "j-low", "title": "low"},
        {"id": "j-high", "title": "high"},
    ]
    sb = _supabase_with(
        {"scores": _Resp(score_rows), "jobs": _Resp(postings_in_storage_order)}
    )

    result = _list_jobs_across_user_targets(
        sb,
        user_target_ids={"t-1", "t-2"},
        cursor={},
        page_size=10,
        sort="score",
        ascending=False,
        min_score=None,
        status=None,
        company=None,
        search=None,
        exclude_terms=[],
        only_terms=[],
    )

    assert [p["id"] for p in result["postings"]] == ["j-high", "j-mid", "j-low"]
    assert [p["score"] for p in result["postings"]] == [80, 50, 20]
    assert result["total"] == 3


def test_target_two_query_location_filter_paginates_post_filter_set() -> None:
    """Regression: when a location filter is active, ``total`` must reflect
    the post-filter row count — not the pre-filter scores-layer count.
    Previously the API returned ``total=3`` for a query that only matched 1
    row after filtering, which made the UI render 4+ near-empty pages."""
    ts_rows = [
        {"job_posting_id": "j-remote", "score": 90, "score_breakdown": {}, "scoring_status": "complete"},
        {"job_posting_id": "j-india", "score": 70, "score_breakdown": {}, "scoring_status": "complete"},
        {"job_posting_id": "j-brazil", "score": 50, "score_breakdown": {}, "scoring_status": "complete"},
    ]
    postings = [
        {"id": "j-remote", "title": "remote role", "location": "Remote · US"},
        {"id": "j-india", "title": "india role", "location": "Bangalore, India"},
        {"id": "j-brazil", "title": "brazil role", "location": "São Paulo, Brazil"},
    ]
    sb = _supabase_with(
        {"scores": _Resp(ts_rows, count=3), "jobs": _Resp(postings)}
    )

    result = _list_jobs_for_target_two_query(
        sb,
        target_id="t-1",
        cursor={},
        page_size=10,
        sort="score",
        ascending=False,
        min_score=None,
        status=None,
        company=None,
        search=None,
        exclude_terms=["india", "brazil"],
        only_terms=[],
    )

    assert [p["id"] for p in result["postings"]] == ["j-remote"]
    # The crux of the regression: total should be the post-filter count,
    # not the 3 pre-filter scores rows.
    assert result["total"] == 1


# ── cursor pagination (#113) ────────────────────────────────────────────────


def test_cursor_codec_round_trips() -> None:
    for payload in ({"v": 90, "id": "j-1"}, {"o": 40}):
        assert _decode_cursor(_encode_cursor(payload)) == payload
    # Empty / None / malformed → first page (empty dict).
    assert _decode_cursor(None) == {}
    assert _encode_cursor(None) is None
    assert _encode_cursor({}) is None
    assert _decode_cursor("not-base64!!") == {}


def _rpc_supabase(rows: list[dict[str, Any]], captured: dict[str, Any]) -> MagicMock:
    sb = MagicMock()

    def _rpc(name: str, params: dict[str, Any]) -> MagicMock:
        captured["name"] = name
        captured["params"] = params
        call = MagicMock()
        call.execute.return_value = _Resp(rows)
        return call

    sb.rpc.side_effect = _rpc
    return sb


def test_rpc_keyset_emits_cursor_and_trims_extra_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pin recency decay OFF: the RPC keyset path is intentionally skipped (it
    # raises) when RECENCY_DECAY_ENABLED is on, since recency sort is handled
    # in the two-query path. .env.local sets it on, so pin it for determinism.
    monkeypatch.setattr(settings, "recency_decay_enabled", False)
    # page_size+1 rows come back → there's a next page; the extra row is
    # dropped and the cursor is the last KEPT row's (score, id).
    rows = [{"id": f"j{i}", "score": 100 - i} for i in range(3)]  # 3 = 2 + 1
    captured: dict[str, Any] = {}
    sb = _rpc_supabase(rows, captured)
    result = _list_jobs_for_target_rpc(
        sb, target_id="t-1", page_size=2, sort="score", ascending=False,
        min_score=None, status=None, company=None, search=None,
        exclude_terms=[], only_terms=[], cursor={},
    )
    assert captured["params"]["p_limit"] == 3  # page_size + 1
    assert captured["params"]["p_after_value"] is None  # first page
    assert captured["params"]["p_after_id"] is None
    assert [p["id"] for p in result["postings"]] == ["j0", "j1"]  # extra trimmed
    assert _decode_cursor(result["next_cursor"]) == {"v": 99, "id": "j1"}
    assert result["total"] is None  # no COUNT on the keyset path


def test_rpc_keyset_last_page_has_no_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # See note above — the RPC keyset path requires recency decay off.
    monkeypatch.setattr(settings, "recency_decay_enabled", False)
    rows = [{"id": "j0", "score": 100}, {"id": "j1", "score": 99}]  # exactly page_size
    sb = _rpc_supabase(rows, {})
    result = _list_jobs_for_target_rpc(
        sb, target_id="t-1", page_size=2, sort="score", ascending=False,
        min_score=None, status=None, company=None, search=None,
        exclude_terms=[], only_terms=[], cursor={},
    )
    assert result["next_cursor"] is None


def test_rpc_keyset_consumes_incoming_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # See note above — the RPC keyset path requires recency decay off.
    monkeypatch.setattr(settings, "recency_decay_enabled", False)
    captured: dict[str, Any] = {}
    sb = _rpc_supabase([], captured)
    _list_jobs_for_target_rpc(
        sb, target_id="t-1", page_size=2, sort="score", ascending=False,
        min_score=None, status=None, company=None, search=None,
        exclude_terms=[], only_terms=[], cursor={"v": 90, "id": "j-x"},
    )
    assert captured["params"]["p_after_value"] == "90"  # stringified for the RPC
    assert captured["params"]["p_after_id"] == "j-x"


def test_two_query_offset_cursor_advances_when_more_rows() -> None:
    # 3 rows, page_size 2 → first page carries a next-cursor at offset 2.
    # Use a title sort so pagination happens in the Python-slice branch (the
    # score fast-path slices page_ids at the scores layer, which the fluent
    # mock — ignoring .in_() — can't represent).
    ts_rows = [
        {"job_posting_id": f"j{i}", "score": 90 - i, "score_breakdown": {}, "scoring_status": "complete"}
        for i in range(3)
    ]
    postings = [{"id": f"j{i}", "title": f"t{i}"} for i in range(3)]
    sb = _supabase_with({"scores": _Resp(ts_rows, count=3), "jobs": _Resp(postings)})
    result = _list_jobs_for_target_two_query(
        sb, target_id="t-1", page_size=2, sort="title", ascending=True,
        min_score=None, status=None, company=None, search=None,
        exclude_terms=[], only_terms=[], cursor={},
    )
    assert [p["id"] for p in result["postings"]] == ["j0", "j1"]
    assert _decode_cursor(result["next_cursor"]) == {"o": 2}
    # Following that cursor yields the last row and no further cursor.
    result2 = _list_jobs_for_target_two_query(
        sb, target_id="t-1", page_size=2, sort="title", ascending=True,
        min_score=None, status=None, company=None, search=None,
        exclude_terms=[], only_terms=[], cursor={"o": 2},
    )
    assert [p["id"] for p in result2["postings"]] == ["j2"]
    assert result2["next_cursor"] is None
