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
    _gate_off_family,
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

    @property
    def not_(self) -> "_Chain":
        # `.not_.is_(...)` negation context — the fake ignores filter args, so
        # negation is a no-op that just keeps the chain fluent (#60 gate).
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
    # Unstubbed tables read as empty (e.g. the #278 off-family gate's `targets`
    # lookup) — an empty `targets` read leaves the gate a no-op, so tests that
    # don't care about role_family are unaffected.
    sb.table.side_effect = lambda name: _Chain(table_resps.get(name, _Resp([])))
    return sb


def test_target_two_query_restores_score_desc_order() -> None:
    # Scores returned in score-desc order (highest first) — this is what
    # Supabase produces because the query chains .order("score", desc=True).
    ts_rows = [
        {
            "job_posting_id": "j-high",
            "score": 90,
            "score_breakdown": {},
            "scoring_status": "complete",
            "fit_reasoning": "graded match",
        },
        {
            "job_posting_id": "j-mid",
            "score": 60,
            "score_breakdown": {},
            "scoring_status": "complete",
            "fit_reasoning": "graded match",
        },
        {
            "job_posting_id": "j-low",
            "score": 30,
            "score_breakdown": {},
            "scoring_status": "complete",
            "fit_reasoning": "graded match",
        },
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
        {
            "job_posting_id": "j-high",
            "target_id": "t-1",
            "score": 80,
            "score_breakdown": {},
            "scoring_status": "complete",
            "fit_reasoning": "graded match",
        },
        {
            "job_posting_id": "j-mid",
            "target_id": "t-1",
            "score": 50,
            "score_breakdown": {},
            "scoring_status": "complete",
            "fit_reasoning": "graded match",
        },
        {
            "job_posting_id": "j-low",
            "target_id": "t-2",
            "score": 20,
            "score_breakdown": {},
            "scoring_status": "complete",
            "fit_reasoning": "graded match",
        },
    ]
    postings_in_storage_order = [
        {"id": "j-mid", "title": "mid"},
        {"id": "j-low", "title": "low"},
        {"id": "j-high", "title": "high"},
    ]
    sb = _supabase_with({"scores": _Resp(score_rows), "jobs": _Resp(postings_in_storage_order)})

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


def test_gate_off_family_drops_family_mismatches() -> None:
    """#278: strict off-family gate — keep a match only when the job's family
    equals the target's, the job is untagged (NULL), or the target is
    unclassified. t-1 is engineering, so finance is dropped; eng + untagged
    are kept."""
    by_id = {
        "j-eng": {"job_posting_id": "j-eng", "target_id": "t-1"},
        "j-fin": {"job_posting_id": "j-fin", "target_id": "t-1"},
        "j-null": {"job_posting_id": "j-null", "target_id": "t-1"},
    }
    sb = _supabase_with(
        {
            "targets": _Resp([{"id": "t-1", "role_family": "engineering"}]),
            "jobs": _Resp(
                [
                    {"id": "j-eng", "role_family": "engineering"},
                    {"id": "j-fin", "role_family": "finance"},
                    {"id": "j-null", "role_family": None},
                ]
            ),
        }
    )
    assert set(_gate_off_family(sb, by_id)) == {"j-eng", "j-null"}


def test_gate_off_family_noop_when_target_unclassified() -> None:
    """A target with no role_family never hides anything (the safe default)."""
    by_id = {"j-fin": {"job_posting_id": "j-fin", "target_id": "t-1"}}
    sb = _supabase_with(
        {
            "targets": _Resp([{"id": "t-1", "role_family": None}]),
            "jobs": _Resp([{"id": "j-fin", "role_family": "finance"}]),
        }
    )
    assert set(_gate_off_family(sb, by_id)) == {"j-fin"}


def test_target_two_query_location_filter_paginates_post_filter_set() -> None:
    """Regression: when a location filter is active, ``total`` must reflect
    the post-filter row count — not the pre-filter scores-layer count.
    Previously the API returned ``total=3`` for a query that only matched 1
    row after filtering, which made the UI render 4+ near-empty pages."""
    ts_rows = [
        {
            "job_posting_id": "j-remote",
            "score": 90,
            "score_breakdown": {},
            "scoring_status": "complete",
            "fit_reasoning": "graded match",
        },
        {
            "job_posting_id": "j-india",
            "score": 70,
            "score_breakdown": {},
            "scoring_status": "complete",
            "fit_reasoning": "graded match",
        },
        {
            "job_posting_id": "j-brazil",
            "score": 50,
            "score_breakdown": {},
            "scoring_status": "complete",
            "fit_reasoning": "graded match",
        },
    ]
    postings = [
        {"id": "j-remote", "title": "remote role", "location": "Remote · US"},
        {"id": "j-india", "title": "india role", "location": "Bangalore, India"},
        {"id": "j-brazil", "title": "brazil role", "location": "São Paulo, Brazil"},
    ]
    sb = _supabase_with({"scores": _Resp(ts_rows, count=3), "jobs": _Resp(postings)})

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


# The RPC keyset path now serves NON-score sorts only: score sort routes to
# the two-query path for Pending-below-graded bucketing, and a min_score floor
# routes there for Pending exemption (#47). These pin the keyset emit/trim/
# consume mechanics on a sort the RPC still handles (created_at).
def test_rpc_keyset_emits_cursor_and_trims_extra_row() -> None:
    # page_size+1 rows come back → there's a next page; the extra row is
    # dropped and the cursor is the last KEPT row's (sort_value, id).
    rows = [
        {"id": f"j{i}", "score": 100 - i, "created_at": f"2026-06-{30 - i:02d}"}
        for i in range(3)  # 3 = 2 + 1
    ]
    captured: dict[str, Any] = {}
    sb = _rpc_supabase(rows, captured)
    result = _list_jobs_for_target_rpc(
        sb,
        target_id="t-1",
        page_size=2,
        sort="created_at",
        ascending=False,
        min_score=None,
        status=None,
        company=None,
        search=None,
        exclude_terms=[],
        only_terms=[],
        cursor={},
    )
    assert captured["params"]["p_limit"] == 3  # page_size + 1
    assert captured["params"]["p_after_value"] is None  # first page
    assert captured["params"]["p_after_id"] is None
    assert [p["id"] for p in result["postings"]] == ["j0", "j1"]  # extra trimmed
    assert _decode_cursor(result["next_cursor"]) == {"v": "2026-06-29", "id": "j1"}
    assert result["total"] is None  # no COUNT on the keyset path


def test_rpc_keyset_last_page_has_no_cursor() -> None:
    rows = [
        {"id": "j0", "score": 100, "created_at": "2026-06-30"},
        {"id": "j1", "score": 99, "created_at": "2026-06-29"},  # exactly page_size
    ]
    sb = _rpc_supabase(rows, {})
    result = _list_jobs_for_target_rpc(
        sb,
        target_id="t-1",
        page_size=2,
        sort="created_at",
        ascending=False,
        min_score=None,
        status=None,
        company=None,
        search=None,
        exclude_terms=[],
        only_terms=[],
        cursor={},
    )
    assert result["next_cursor"] is None


def test_rpc_keyset_consumes_incoming_cursor() -> None:
    captured: dict[str, Any] = {}
    sb = _rpc_supabase([], captured)
    _list_jobs_for_target_rpc(
        sb,
        target_id="t-1",
        page_size=2,
        sort="created_at",
        ascending=False,
        min_score=None,
        status=None,
        company=None,
        search=None,
        exclude_terms=[],
        only_terms=[],
        cursor={"v": "2026-06-20", "id": "j-x"},
    )
    assert captured["params"]["p_after_value"] == "2026-06-20"  # passed through
    assert captured["params"]["p_after_id"] == "j-x"


def test_rpc_skips_score_sort_and_floored_queries() -> None:
    # Score sort (Pending bucketing) and any min_score floor (Pending exemption)
    # are handled in the two-query path; the RPC raises so the dispatcher falls
    # back. (#47)
    sb = _rpc_supabase([], {})
    for extra in ({"sort": "score", "min_score": None}, {"sort": "created_at", "min_score": 70}):
        with pytest.raises(RuntimeError):
            _list_jobs_for_target_rpc(
                sb,
                target_id="t-1",
                page_size=2,
                ascending=False,
                status=None,
                company=None,
                search=None,
                exclude_terms=[],
                only_terms=[],
                cursor={},
                **extra,
            )


def test_two_query_offset_cursor_advances_when_more_rows() -> None:
    # 3 rows, page_size 2 → first page carries a next-cursor at offset 2.
    # Use a title sort so pagination happens in the Python-slice branch (the
    # score fast-path slices page_ids at the scores layer, which the fluent
    # mock — ignoring .in_() — can't represent).
    ts_rows = [
        {
            "job_posting_id": f"j{i}",
            "score": 90 - i,
            "score_breakdown": {},
            "scoring_status": "complete",
        }
        for i in range(3)
    ]
    postings = [{"id": f"j{i}", "title": f"t{i}"} for i in range(3)]
    sb = _supabase_with({"scores": _Resp(ts_rows, count=3), "jobs": _Resp(postings)})
    result = _list_jobs_for_target_two_query(
        sb,
        target_id="t-1",
        page_size=2,
        sort="title",
        ascending=True,
        min_score=None,
        status=None,
        company=None,
        search=None,
        exclude_terms=[],
        only_terms=[],
        cursor={},
    )
    assert [p["id"] for p in result["postings"]] == ["j0", "j1"]
    assert _decode_cursor(result["next_cursor"]) == {"o": 2}
    # Following that cursor yields the last row and no further cursor.
    result2 = _list_jobs_for_target_two_query(
        sb,
        target_id="t-1",
        page_size=2,
        sort="title",
        ascending=True,
        min_score=None,
        status=None,
        company=None,
        search=None,
        exclude_terms=[],
        only_terms=[],
        cursor={"o": 2},
    )
    assert [p["id"] for p in result2["postings"]] == ["j2"]
    assert result2["next_cursor"] is None


def test_gate_live_us_applies_archived_and_non_us_filters() -> None:
    """#60: the display/match gate drops globally-archived jobs (archived_at IS
    NULL) AND CONFIRMED non-US ones (is_us IS NOT false — keeps US + not-yet-
    tagged). A spy records the PostgREST filter calls so a dropped gate fails."""
    from app.routers.jobs import _gate_live_us

    calls: list[tuple[str, ...]] = []

    class _Spy:
        @property
        def not_(self) -> "_Spy":
            calls.append(("not_",))
            return self

        def is_(self, col: str, val: str) -> "_Spy":
            calls.append(("is_", col, val))
            return self

    _gate_live_us(_Spy())

    assert ("is_", "archived_at", "null") in calls  # globally-live gate
    assert ("is_", "is_us", "false") in calls  # non-US gate
    # the is_us filter is NEGATED (is_us IS NOT false), so `not_` precedes it
    assert calls.index(("not_",)) < calls.index(("is_", "is_us", "false"))


# ── Pending-tier recency sort (#47 f/u) ─────────────────────────────────────


def test_rank_graded_first_orders_pending_by_recency_key() -> None:
    """The Pending bucket sorts by ``pending_value`` (recency), graded by
    ``value`` (fit score): a stale Pending row with a HIGH keyword score sinks
    below a fresh Pending row with a LOW one. Without ``pending_value`` it falls
    back to ``value`` (the old keyword-score ordering)."""
    from app.routers.jobs import _rank_graded_first

    rows = [
        {"id": "g-hi", "fit_reasoning": "x", "disp": 80, "fs": "2026-07-05"},
        {"id": "g-lo", "fit_reasoning": "x", "disp": 60, "fs": "2026-07-04"},
        {"id": "p-old", "fit_reasoning": None, "disp": 90, "fs": "2026-07-01"},
        {"id": "p-new", "fit_reasoning": None, "disp": 40, "fs": "2026-07-09"},
    ]
    ranked = _rank_graded_first(
        rows,
        value=lambda r: (r["disp"], r["id"]),
        ascending=False,
        pending_value=lambda r: (r["fs"], r["id"]),
    )
    # graded by score, then Pending NEWEST-first (07-09 before 07-01) even though
    # the fresh one's keyword score (40) is lower than the stale one's (90).
    assert [r["id"] for r in ranked] == ["g-hi", "g-lo", "p-new", "p-old"]

    # Fallback (no pending_value) keeps the old behavior: Pending by `value`, so
    # the high-keyword stale row leads its bucket.
    ranked_fallback = _rank_graded_first(
        rows, value=lambda r: (r["disp"], r["id"]), ascending=False
    )
    assert [r["id"] for r in ranked_fallback] == ["g-hi", "g-lo", "p-old", "p-new"]


def test_two_query_pending_sorts_by_recency_not_keyword_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end through the score-sorted two-query path: within the Pending
    tier, fresh ungraded jobs outrank stale ones by first_seen_at — NOT by the
    hidden keyword placeholder. Decay is OFF, so this also exercises the forced
    first-seen fetch (the case the ``force`` flag exists for)."""
    monkeypatch.setattr(settings, "recency_decay_enabled", False)
    ts_rows = [
        {
            "job_posting_id": "g-hi",
            "score": 80,
            "score_breakdown": {},
            "scoring_status": "complete",
            "fit_reasoning": "strong match",
        },
        {
            "job_posting_id": "g-lo",
            "score": 60,
            "score_breakdown": {},
            "scoring_status": "complete",
            "fit_reasoning": "ok match",
        },
        # Pending (no fit_reasoning) with keyword placeholders: the stale one
        # carries the HIGHER keyword score.
        {
            "job_posting_id": "p-old-hi",
            "score": 90,
            "score_breakdown": {},
            "scoring_status": "stage2",
            "fit_reasoning": None,
        },
        {
            "job_posting_id": "p-new-lo",
            "score": 40,
            "score_breakdown": {},
            "scoring_status": "stage2",
            "fit_reasoning": None,
        },
    ]
    jobs_rows = [
        {"id": "g-hi", "title": "graded hi", "first_seen_at": "2026-07-05"},
        {"id": "g-lo", "title": "graded lo", "first_seen_at": "2026-07-04"},
        {"id": "p-old-hi", "title": "pending old high-keyword", "first_seen_at": "2026-07-01"},
        {"id": "p-new-lo", "title": "pending new low-keyword", "first_seen_at": "2026-07-09"},
    ]
    sb = _supabase_with({"scores": _Resp(ts_rows, count=4), "jobs": _Resp(jobs_rows)})

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

    # graded first (fit score), then Pending newest-first: p-new-lo (07-09) ABOVE
    # p-old-hi (07-01) despite 40 < 90 keyword scores.
    assert [p["id"] for p in result["postings"]] == ["g-hi", "g-lo", "p-new-lo", "p-old-hi"]
