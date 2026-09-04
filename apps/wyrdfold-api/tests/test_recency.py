"""Tests for recency decay (#5).

Covers the pure decay math (``compute_recency_multiplier`` /
``compute_recency_score``), the poller-side refresh pass
(``refresh_recency_scores_poll`` — flag-off sync-in-thread AND flag-on
async-client routing through the #57 seam), and the /jobs two-query
ordering when the ``RECENCY_DECAY_ENABLED`` flag is on.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

import app.services.recency as recency_mod
from app.config import settings
from app.models.targets import AxisWeights
from app.routers.jobs import (
    _display_sort_value,
    _list_jobs_across_user_targets,
    _list_jobs_for_target_two_query,
)
from app.services import db_write
from app.services.recency import (
    RECENCY_FLOOR,
    compute_recency_multiplier,
    compute_recency_score,
    display_recency_score,
    refresh_all_recency_scores,
    refresh_recency_scores_poll,
)

# ---- Pure decay math -------------------------------------------------------


def test_multiplier_full_inside_grace_window() -> None:
    assert compute_recency_multiplier(0) == 1.0
    assert compute_recency_multiplier(7) == 1.0


def test_multiplier_decays_after_grace() -> None:
    # Day 8: one day past the 7-day grace → lose 1.5%.
    assert compute_recency_multiplier(8) == pytest.approx(0.985)
    # Day 27: 20 days past grace → lose 30%.
    assert compute_recency_multiplier(27) == pytest.approx(0.70)


def test_multiplier_floors_at_30_percent() -> None:
    # 1 - (age-7)*0.015 = 0.3 → age ≈ 53.7; anything older clamps to the floor.
    assert compute_recency_multiplier(54) == RECENCY_FLOOR
    assert compute_recency_multiplier(365) == RECENCY_FLOOR


def test_multiplier_clamps_negative_age() -> None:
    """Clock skew on a just-ingested row must not exceed full score."""
    assert compute_recency_multiplier(-3) == 1.0


def test_compute_recency_score_disabled_is_identity() -> None:
    # Even a very old posting keeps its raw score when the flag is off.
    assert compute_recency_score(90, age_days=200, enabled=False) == 90


def test_compute_recency_score_enabled_applies_decay() -> None:
    assert compute_recency_score(90, age_days=0, enabled=True) == 90
    # 20 days past grace → 0.70 → round(90 * 0.70) = 63.
    assert compute_recency_score(90, age_days=27, enabled=True) == 63
    # Past the floor → round(90 * 0.30) = 27.
    assert compute_recency_score(90, age_days=400, enabled=True) == 27


# ---- refresh_recency_scores_poll -------------------------------------------


class _Resp:
    def __init__(self, data: list[dict[str, Any]]) -> None:
        self.data = data


class _TableChain:
    def __init__(self, resp: _Resp) -> None:
        self._resp = resp

    def select(self, *_a: Any, **_kw: Any) -> _TableChain:
        return self

    def in_(self, *_a: Any, **_kw: Any) -> _TableChain:
        return self

    def execute(self) -> _Resp:
        return self._resp


class _RpcChain:
    def __init__(self, sink: list[dict[str, Any]]) -> None:
        self._sink = sink

    def execute(self) -> _Resp:
        return _Resp([])


def _refresh_supabase(
    jobs: list[dict[str, Any]],
    scores: list[dict[str, Any]],
    rpc_calls: list[tuple[str, dict[str, Any]]],
) -> MagicMock:
    sb = MagicMock()
    sb.table.side_effect = lambda name: _TableChain(_Resp(jobs if name == "jobs" else scores))

    def _rpc(name: str, params: dict[str, Any]) -> _RpcChain:
        rpc_calls.append((name, params))
        return _RpcChain([])

    sb.rpc.side_effect = _rpc
    return sb


@pytest.mark.asyncio
async def test_refresh_applies_decay_per_row_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "recency_decay_enabled", True)
    fresh = datetime.now(UTC).isoformat()
    old = (datetime.now(UTC) - timedelta(days=27)).isoformat()
    jobs = [
        {"id": "j-fresh", "cataloged_at": fresh},
        {"id": "j-old", "cataloged_at": old},
    ]
    # Two targets for the old job → two rows, different scores, same age.
    scores = [
        {"id": "s1", "job_posting_id": "j-fresh", "score": 80},
        {"id": "s2", "job_posting_id": "j-old", "score": 90},
        {"id": "s3", "job_posting_id": "j-old", "score": 40},
    ]
    rpc_calls: list[tuple[str, dict[str, Any]]] = []
    sb = _refresh_supabase(jobs, scores, rpc_calls)

    written = await refresh_recency_scores_poll(sb, ["j-fresh", "j-old"])

    assert written == 3
    assert len(rpc_calls) == 1
    name, params = rpc_calls[0]
    assert name == "bulk_update_recency_scores"
    by_id = {u["id"]: u["recency_score"] for u in params["p_updates"]}
    assert by_id["s1"] == 80  # fresh → no decay
    assert by_id["s2"] == 63  # round(90 * 0.70)
    assert by_id["s3"] == 28  # round(40 * 0.70)


@pytest.mark.asyncio
async def test_refresh_mirrors_score_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "recency_decay_enabled", False)
    old = (datetime.now(UTC) - timedelta(days=100)).isoformat()
    jobs = [{"id": "j-old", "cataloged_at": old}]
    scores = [{"id": "s1", "job_posting_id": "j-old", "score": 90}]
    rpc_calls: list[tuple[str, dict[str, Any]]] = []
    sb = _refresh_supabase(jobs, scores, rpc_calls)

    await refresh_recency_scores_poll(sb, ["j-old"])

    by_id = {u["id"]: u["recency_score"] for u in rpc_calls[0][1]["p_updates"]}
    assert by_id["s1"] == 90  # flag off → recency mirrors raw score


@pytest.mark.asyncio
async def test_refresh_noop_on_empty_input() -> None:
    sb = MagicMock()
    assert await refresh_recency_scores_poll(sb, []) == 0
    sb.rpc.assert_not_called()


# ---- refresh_recency_scores_poll on the #57 async seam ----------------------


class _AsyncReadChain:
    """Async twin of ``_TableChain``: what the seam's ``build`` closure chains
    on the pooled ``AsyncClient`` when ``POLLER_ASYNC_DB`` is on."""

    def __init__(self, client: _AsyncSeamClient, resp: _Resp, fail: bool) -> None:
        self._client = client
        self._resp = resp
        self._fail = fail

    def select(self, *_a: Any, **_kw: Any) -> _AsyncReadChain:
        return self

    def in_(self, *_a: Any, **_kw: Any) -> _AsyncReadChain:
        return self

    async def execute(self) -> _Resp:
        self._client.executed += 1
        if self._fail:
            raise Exception("db down")
        return self._resp


class _AsyncRpcHandle:
    """Async rpc handle; raises on execute when the chunk is marked to fail."""

    def __init__(self, client: _AsyncSeamClient, fail: bool) -> None:
        self._client = client
        self._fail = fail

    async def execute(self) -> _Resp:
        self._client.executed += 1
        if self._fail:
            raise Exception("bulk chunk failed")
        return _Resp([])


class _AsyncSeamClient:
    """Async-client stand-in handed to ``build`` by the #57 seam: answers the
    jobs/scores reads, records rpc calls, and counts every ``execute``.
    ``fail_rpc`` marks which rpc calls (1-based) blow up; ``fail_jobs_read``
    makes the jobs fetch raise."""

    def __init__(
        self,
        jobs: list[dict[str, Any]],
        scores: list[dict[str, Any]],
        *,
        fail_rpc: set[int] | None = None,
        fail_jobs_read: bool = False,
    ) -> None:
        self._jobs = jobs
        self._scores = scores
        self._fail_rpc = fail_rpc or set()
        self._fail_jobs_read = fail_jobs_read
        self.executed = 0
        self.rpc_calls: list[tuple[str, dict[str, Any]]] = []

    def table(self, name: str) -> _AsyncReadChain:
        if name == "jobs":
            return _AsyncReadChain(self, _Resp(self._jobs), fail=self._fail_jobs_read)
        return _AsyncReadChain(self, _Resp(self._scores), fail=False)

    def rpc(self, name: str, params: dict[str, Any]) -> _AsyncRpcHandle:
        self.rpc_calls.append((name, params))
        return _AsyncRpcHandle(self, fail=len(self.rpc_calls) in self._fail_rpc)


def _flag_on(monkeypatch: pytest.MonkeyPatch, async_client: _AsyncSeamClient) -> None:
    monkeypatch.setattr(db_write, "get_async_supabase", lambda: async_client)


@pytest.mark.asyncio
async def test_refresh_poll_flag_on_reads_and_writes_on_async_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POLLER_ASYNC_DB on: both reads AND the bulk-update rpc execute on the
    pooled async client, the sync client is never touched, and the decay math
    is byte-identical to the flag-off path."""
    monkeypatch.setattr(settings, "recency_decay_enabled", True)
    fresh = datetime.now(UTC).isoformat()
    old = (datetime.now(UTC) - timedelta(days=27)).isoformat()
    jobs = [
        {"id": "j-fresh", "cataloged_at": fresh},
        {"id": "j-old", "cataloged_at": old},
    ]
    scores = [
        {"id": "s1", "job_posting_id": "j-fresh", "score": 80},
        {"id": "s2", "job_posting_id": "j-old", "score": 90},
        {"id": "s3", "job_posting_id": "j-old", "score": 40},
    ]
    async_client = _AsyncSeamClient(jobs, scores)
    _flag_on(monkeypatch, async_client)
    sync_sb = MagicMock()

    written = await refresh_recency_scores_poll(sync_sb, ["j-fresh", "j-old"])

    assert written == 3
    # jobs read + scores read + rpc write, all on the async client.
    assert async_client.executed == 3
    sync_sb.table.assert_not_called()
    sync_sb.rpc.assert_not_called()
    assert len(async_client.rpc_calls) == 1
    name, params = async_client.rpc_calls[0]
    assert name == "bulk_update_recency_scores"
    by_id = {u["id"]: u["recency_score"] for u in params["p_updates"]}
    assert by_id["s1"] == 80  # fresh → no decay
    assert by_id["s2"] == 63  # round(90 * 0.70)
    assert by_id["s3"] == 28  # round(40 * 0.70)


@pytest.mark.asyncio
async def test_refresh_poll_flag_on_failed_chunk_counts_zero_others_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed bulk-update chunk is logged and counts 0 while the remaining
    chunks still land — one bad batch must never abort the pass."""
    monkeypatch.setattr(settings, "recency_decay_enabled", False)
    monkeypatch.setattr(recency_mod, "_RECENCY_CHUNK_SIZE", 2)
    now_iso = datetime.now(UTC).isoformat()
    jobs = [
        {"id": "j1", "cataloged_at": now_iso},
        {"id": "j2", "cataloged_at": now_iso},
    ]
    # 3 updates with chunk size 2 → chunks [s1, s2] and [s3].
    scores = [
        {"id": "s1", "job_posting_id": "j1", "score": 80},
        {"id": "s2", "job_posting_id": "j1", "score": 90},
        {"id": "s3", "job_posting_id": "j2", "score": 40},
    ]
    async_client = _AsyncSeamClient(jobs, scores, fail_rpc={1})
    _flag_on(monkeypatch, async_client)

    written = await refresh_recency_scores_poll(MagicMock(), ["j1", "j2"])

    # First chunk (2 rows) failed → 0; second chunk (1 row) landed.
    assert written == 1
    assert len(async_client.rpc_calls) == 2  # both chunks attempted
    assert [u["id"] for u in async_client.rpc_calls[0][1]["p_updates"]] == ["s1", "s2"]
    assert [u["id"] for u in async_client.rpc_calls[1][1]["p_updates"]] == ["s3"]


@pytest.mark.asyncio
async def test_refresh_poll_flag_on_jobs_read_failure_fail_soft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A jobs-fetch error is swallowed: return 0, never raise into the poll
    cycle, and never reach the rpc."""
    async_client = _AsyncSeamClient([], [], fail_jobs_read=True)
    _flag_on(monkeypatch, async_client)

    assert await refresh_recency_scores_poll(MagicMock(), ["j1"]) == 0
    assert async_client.rpc_calls == []


# ---- /jobs ordering by recency_score ---------------------------------------


class _ListResp:
    def __init__(self, data: list[dict[str, Any]], count: int | None = None) -> None:
        self.data = data
        self.count = count


class _ListChain:
    def __init__(self, resp: _ListResp) -> None:
        self._resp = resp

    def select(self, *_a: Any, **_kw: Any) -> _ListChain:
        return self

    def eq(self, *_a: Any, **_kw: Any) -> _ListChain:
        return self

    def neq(self, *_a: Any, **_kw: Any) -> _ListChain:
        return self

    def in_(self, *_a: Any, **_kw: Any) -> _ListChain:
        return self

    def is_(self, *_a: Any, **_kw: Any) -> _ListChain:
        return self

    @property
    def not_(self) -> _ListChain:
        return self

    def gte(self, *_a: Any, **_kw: Any) -> _ListChain:
        return self

    def ilike(self, *_a: Any, **_kw: Any) -> _ListChain:
        return self

    def order(self, *_a: Any, **_kw: Any) -> _ListChain:
        return self

    def range(self, *_a: Any, **_kw: Any) -> _ListChain:
        return self

    async def execute(self) -> _ListResp:
        return self._resp


def _list_supabase(table_resps: dict[str, _ListResp]) -> MagicMock:
    sb = MagicMock()
    # Unstubbed tables (e.g. the #278 off-family gate's ``targets`` lookup) read
    # as empty, leaving the gate a no-op for tests that don't set role_family.
    sb.table.side_effect = lambda name: _ListChain(table_resps.get(name, _ListResp([])))
    return sb


async def test_target_two_query_orders_by_recency_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A high-fit but stale job sorts BELOW a fresher, lower-fit job once
    decay is on — the visible (raw) score still rides along."""
    monkeypatch.setattr(settings, "recency_decay_enabled", True)
    # The sort now keys on the score each row DISPLAYS — read-time decay from
    # the posted date (#47), not the stored ``recency_score``: fresh-70 (no
    # decay) outranks stale-95 (decays to ~66 at 27 days).
    fresh = datetime.now(UTC).isoformat()
    old = (datetime.now(UTC) - timedelta(days=27)).isoformat()
    ts_rows = [
        {
            "job_posting_id": "j-fresh",
            "score": 70,
            "recency_score": 70,
            "score_breakdown": {},
            "scoring_status": "complete",
        },
        {
            "job_posting_id": "j-stale",
            "score": 95,
            "recency_score": 48,
            "score_breakdown": {},
            "scoring_status": "complete",
        },
    ]
    postings_storage_order = [
        {"id": "j-stale", "title": "stale high-fit", "cataloged_at": old},
        {"id": "j-fresh", "title": "fresh", "cataloged_at": fresh},
    ]
    sb = _list_supabase(
        {
            "scores": _ListResp(ts_rows, count=2),
            "jobs": _ListResp(postings_storage_order),
        }
    )

    result = await _list_jobs_for_target_two_query(
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

    assert [p["id"] for p in result["postings"]] == ["j-fresh", "j-stale"]
    # #665: the helper returns THE score — the aged one, straight from the
    # stored column — because that is the number the list shows, sorts by and
    # floors on. j-stale's raw fit is 95 but it has decayed to 48, and 48 is
    # what a user sees. There is no second number applied a layer up any more.
    assert [p["score"] for p in result["postings"]] == [70, 48]
    assert [p["raw_score"] for p in result["postings"]] == [70, 95]


async def test_across_targets_orders_by_recency_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "recency_decay_enabled", True)
    # Location filter active → forces the Python sort path (not the
    # scores-layer slice), exercising the _sort_key display-value branch.
    # The sort keys on read-time display decay (#47): fresh-70 > stale-95→66.
    fresh = datetime.now(UTC).isoformat()
    old = (datetime.now(UTC) - timedelta(days=27)).isoformat()
    score_rows = [
        {
            "job_posting_id": "j-fresh",
            "target_id": "t-1",
            "score": 70,
            "recency_score": 70,
            "score_breakdown": {},
            "scoring_status": "complete",
        },
        {
            "job_posting_id": "j-stale",
            "target_id": "t-2",
            "score": 95,
            "recency_score": 48,
            "score_breakdown": {},
            "scoring_status": "complete",
        },
    ]
    postings = [
        {"id": "j-stale", "title": "stale", "location": "Remote · US", "cataloged_at": old},
        {"id": "j-fresh", "title": "fresh", "location": "Remote · US", "cataloged_at": fresh},
    ]
    sb = _list_supabase({"scores": _ListResp(score_rows), "jobs": _ListResp(postings)})

    result = await _list_jobs_across_user_targets(
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
        only_terms=["remote"],
    )

    assert [p["id"] for p in result["postings"]] == ["j-fresh", "j-stale"]
    # #665: THE score is the aged one (see the twin test above).
    assert [p["score"] for p in result["postings"]] == [70, 48]
    assert [p["raw_score"] for p in result["postings"]] == [70, 95]


async def test_two_query_sorts_by_weighted_display_not_raw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With custom axis weights, the list sorts by the WEIGHTED score the user
    sees — not the raw fit (#47). A lower-raw job whose weighted score is higher
    ranks first. (Decay off to isolate the weighting effect.)"""
    monkeypatch.setattr(settings, "recency_decay_enabled", False)
    ts_rows = [
        {
            "job_posting_id": "j-weighted-high",
            "score": 60,
            "axis_scores": {
                "title_fit": 95,
                "skills_fit": 95,
                "seniority_fit": 20,
                "domain_fit": 20,
            },
            "score_breakdown": {},
            "scoring_status": "complete",
        },
        {
            "job_posting_id": "j-raw-high",
            "score": 90,
            "axis_scores": {
                "title_fit": 20,
                "skills_fit": 20,
                "seniority_fit": 95,
                "domain_fit": 95,
            },
            "score_breakdown": {},
            "scoring_status": "complete",
        },
    ]
    # Storage order is the inverse of the weighted order, so a passthrough
    # (raw) sort would leave j-raw-high on top — the bug this guards.
    postings = [
        {"id": "j-raw-high", "title": "raw 90"},
        {"id": "j-weighted-high", "title": "raw 60"},
    ]
    sb = _list_supabase({"scores": _ListResp(ts_rows, count=2), "jobs": _ListResp(postings)})
    weights = AxisWeights(title_fit=0.5, skills_fit=0.5, seniority_fit=0.0, domain_fit=0.0)

    result = await _list_jobs_for_target_two_query(
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
        axis_weights=weights,
    )

    # Weighted display ~95 (title+skills) beats ~20, so the raw-60 job ranks
    # first despite the other's raw 90 — order follows the visible number.
    assert [p["id"] for p in result["postings"]] == [
        "j-weighted-high",
        "j-raw-high",
    ]
    assert result["postings"][0]["score"] > result["postings"][1]["score"]


# ---- display_recency_score (read-time decay) -------------------------------


def test_display_recency_score_decays_from_first_seen() -> None:
    now = datetime(2026, 6, 29, tzinfo=UTC)
    fresh = now.isoformat()
    old = (now - timedelta(days=27)).isoformat()
    assert display_recency_score(90, fresh, now) == 90  # inside grace
    assert display_recency_score(90, old, now) == 63  # round(90 * 0.70)


def test_display_recency_score_missing_first_seen_treated_as_fresh() -> None:
    now = datetime(2026, 6, 29, tzinfo=UTC)
    assert display_recency_score(90, None, now) == 90


# ---- _display_sort_value (THE score, #665) ---------------------------------
#
# ``_apply_display_recency`` was DELETED in #665. It re-decayed the shown score
# at read time, which made it a second implementation of the displayed number —
# and it promptly drifted away from the floor, so "Score 70+" rendered cards at
# 56. The displayed value now comes from one place: the stored ``recency_score``
# column, which a scheduler sweep keeps current.


def test_display_value_reads_the_stored_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No read-time arithmetic on the un-weighted path — the column IS the
    answer, which is also what makes the floor indexable."""
    monkeypatch.setattr(settings, "recency_decay_enabled", True)
    old = (datetime.now(UTC) - timedelta(days=27)).isoformat()
    row = {"job_posting_id": "j1", "score": 100, "recency_score": 70}

    value = _display_sort_value(row, weights=None, posted_at=old, now=datetime.now(UTC))

    assert value == 70


def test_display_value_ignores_the_decay_flag_when_unweighted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``recency_score`` is an identity write when decay is off
    (``compute_recency_score(..., enabled=False)`` returns ``score``), so the
    reader does not branch on the flag at all."""
    monkeypatch.setattr(settings, "recency_decay_enabled", False)
    row = {"job_posting_id": "j1", "score": 100, "recency_score": 70}

    value = _display_sort_value(row, weights=None, posted_at=None, now=datetime.now(UTC))

    assert value == 70


def test_display_value_falls_back_to_raw_when_column_absent() -> None:
    """A select that didn't fetch the column must not sink the row to 0."""
    row = {"job_posting_id": "j1", "score": 82}

    value = _display_sort_value(row, weights=None, posted_at=None, now=datetime.now(UTC))

    assert value == 82


def test_display_value_still_decays_at_read_time_for_custom_weights(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE exception. ``recency_score`` derives from ``score``, not from the
    per-membership weighted blend, so the weighted path recomputes. Mirrors
    ``v_gk`` in ``get_cross_target_jobs`` — keep the two in step."""
    monkeypatch.setattr(settings, "recency_decay_enabled", True)
    old = (datetime.now(UTC) - timedelta(days=27)).isoformat()
    # Weights that lift the blend well above the stored (un-weighted) column,
    # so reading the column instead would be visible.
    weights = AxisWeights(title_fit=1.0, skills_fit=0.0, seniority_fit=0.0, domain_fit=0.0)
    row = {
        "job_posting_id": "j1",
        "score": 50,
        "recency_score": 35,
        "axis_scores": {"title_fit": 100, "skills_fit": 0, "seniority_fit": 0, "domain_fit": 0},
    }

    value = _display_sort_value(row, weights=weights, posted_at=old, now=datetime.now(UTC))

    # blend = 100 (all weight on title_fit), decayed by 27 days => round(100*0.70)
    assert value == 70
    # ...and emphatically NOT the stored column, which knows nothing of weights.
    assert value != 35


# ---- refresh_all_recency_scores (full sweep, set-based #604) ---------------
#
# The sweep is now a keyset loop over the ``sweep_recency_scores`` RPC — the
# decay arithmetic lives in SQL, so these pin the LOOP mechanics (cursor
# advance, termination, fail-soft, and that the module's constants are what
# ride the wire). Value parity between the SQL and
# ``compute_recency_score`` is pinned against real Postgres in
# tests/integration/test_recency_sweep_parity.py.


class _SweepRpcChain:
    def __init__(self, result: Any) -> None:
        self._result = result

    async def execute(self) -> _Resp:
        if isinstance(self._result, Exception):
            raise self._result
        return _Resp(self._result)


def _sweep_supabase(
    batches: list[Any],
    rpc_calls: list[tuple[str, dict[str, Any]]],
) -> MagicMock:
    """Fake AsyncClient whose ``sweep_recency_scores`` RPC serves ``batches``
    in order — each entry a response ``data`` list, or an Exception to raise."""
    sb = MagicMock()
    remaining = list(batches)

    def _rpc(name: str, params: dict[str, Any]) -> _SweepRpcChain:
        rpc_calls.append((name, params))
        return _SweepRpcChain(remaining.pop(0) if remaining else [])

    sb.rpc.side_effect = _rpc
    return sb


def _batch(scanned: int, written: int, last_id: str | None) -> list[dict[str, Any]]:
    return [{"scanned": scanned, "written": written, "last_id": last_id}]


@pytest.mark.asyncio
async def test_refresh_all_pages_the_sweep_rpc_with_a_keyset_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "recency_decay_enabled", True)
    monkeypatch.setattr(recency_mod, "_SWEEP_BATCH_SIZE", 2)
    rpc_calls: list[tuple[str, dict[str, Any]]] = []
    sb = _sweep_supabase(
        [_batch(2, 1, "id-a"), _batch(2, 2, "id-b"), _batch(1, 0, "id-c")],
        rpc_calls,
    )

    written = await refresh_all_recency_scores(sb)

    assert written == 3  # only CHANGED rows count — no-op writes are gone
    assert [name for name, _ in rpc_calls] == ["sweep_recency_scores"] * 3
    # The cursor is the previous batch's last_id; the loop ends on the short
    # batch (scanned < batch size ⇒ the id range is exhausted).
    assert [p["p_after_id"] for _, p in rpc_calls] == [
        "00000000-0000-0000-0000-000000000000",
        "id-a",
        "id-b",
    ]
    # The module constants are the single source of truth for the decay
    # curve — they must be what rides to SQL.
    _, params = rpc_calls[0]
    assert params["p_enabled"] is True
    assert params["p_batch_size"] == 2
    assert params["p_grace_days"] == float(recency_mod.RECENCY_GRACE_DAYS)
    assert params["p_daily_decay"] == recency_mod.RECENCY_DAILY_DECAY
    assert params["p_floor"] == recency_mod.RECENCY_FLOOR


@pytest.mark.asyncio
async def test_refresh_all_passes_the_flag_off_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "recency_decay_enabled", False)
    rpc_calls: list[tuple[str, dict[str, Any]]] = []
    sb = _sweep_supabase([_batch(0, 0, None)], rpc_calls)

    await refresh_all_recency_scores(sb)

    assert rpc_calls[0][1]["p_enabled"] is False


@pytest.mark.asyncio
async def test_refresh_all_keeps_partial_progress_on_a_failed_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mid-sweep error must not raise (APScheduler would swallow the
    trace) and must not discard the batches already written — the next tick
    finishes the rest."""
    monkeypatch.setattr(settings, "recency_decay_enabled", True)
    monkeypatch.setattr(recency_mod, "_SWEEP_BATCH_SIZE", 2)
    rpc_calls: list[tuple[str, dict[str, Any]]] = []
    sb = _sweep_supabase([_batch(2, 5, "id-a"), RuntimeError("57014")], rpc_calls)

    assert await refresh_all_recency_scores(sb) == 5
    assert len(rpc_calls) == 2


@pytest.mark.asyncio
async def test_refresh_all_handles_an_empty_response() -> None:
    rpc_calls: list[tuple[str, dict[str, Any]]] = []
    sb = _sweep_supabase([[]], rpc_calls)

    assert await refresh_all_recency_scores(sb) == 0
    assert len(rpc_calls) == 1


@pytest.mark.asyncio
async def test_refresh_poll_read_chunks_stay_url_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for the 414 the #57 load test caught: the ``.in_()`` ID
    reads ride the request URL, and 500 UUIDs build a ~19KB query string that
    Kong rejects with "URI too long" — silently killing the refresh (fail-
    soft) on any cycle bigger than a few hundred rows. Reads must chunk at
    ``_RECENCY_READ_CHUNK_SIZE``; only the JSONB-body rpc write may batch at
    the larger ``_RECENCY_CHUNK_SIZE``."""

    read_chunks: list[int] = []
    rpc_chunks: list[int] = []

    class _Chain:
        def select(self, *_a: Any, **_kw: Any) -> _Chain:
            return self

        def in_(self, _col: str, ids: list[str]) -> _Chain:
            read_chunks.append(len(ids))
            return self

        async def execute(self) -> _Resp:
            return _Resp([])

    class _Client:
        def table(self, _name: str) -> _Chain:
            return _Chain()

        def rpc(self, _name: str, params: dict[str, Any]) -> Any:
            rpc_chunks.append(len(params["p_updates"]))

            class _H:
                async def execute(self) -> _Resp:
                    return _Resp([])

            return _H()

    monkeypatch.setattr(db_write, "get_async_supabase", lambda: _Client())

    ids = [f"00000000-0000-4000-8000-{i:012d}" for i in range(400)]
    await refresh_recency_scores_poll(MagicMock(), ids)

    # 400 ids at read-chunk 150 → 3 chunks per pass x 2 passes (jobs, scores).
    assert len(read_chunks) == 6
    assert all(n <= recency_mod._RECENCY_READ_CHUNK_SIZE for n in read_chunks)
    assert max(read_chunks) == recency_mod._RECENCY_READ_CHUNK_SIZE
    # Empty score reads → no updates → no rpc chunks in this run.
    assert rpc_chunks == []
