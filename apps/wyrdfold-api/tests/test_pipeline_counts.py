"""Tests for the /jobs/pipeline-counts projection endpoint.

Replaces the dashboard's seven ``/jobs?status=X&page_size=1`` round-trips.
Counts must match the untargeted JWT list view: scores rows for the
user's targets (excluded=False, optional ``list_min_score`` floor),
deduplicated by job, grouped by job status.
"""

from typing import Any
from unittest.mock import MagicMock

from app.routers.jobs import (
    _pipeline_counts_grouped,
    _pipeline_counts_python,
    pipeline_counts,
)


class _Resp:
    def __init__(self, data: Any) -> None:
        self.data = data


class _Chain:
    """Fluent stub — builder methods return self; execute() returns the
    preloaded response."""

    def __init__(self, resp: _Resp) -> None:
        self._resp = resp

    def select(self, *_a: Any, **_kw: Any) -> "_Chain":
        return self

    def eq(self, *_a: Any, **_kw: Any) -> "_Chain":
        return self

    def in_(self, *_a: Any, **_kw: Any) -> "_Chain":
        return self

    def gte(self, *_a: Any, **_kw: Any) -> "_Chain":
        return self

    def execute(self) -> _Resp:
        return self._resp


def test_python_fallback_dedups_jobs_across_targets() -> None:
    # j1 scored against two targets — must count once.
    score_rows = [
        {"job_posting_id": "j1"},
        {"job_posting_id": "j1"},
        {"job_posting_id": "j2"},
    ]
    job_rows = [
        {"status": "new"},
        {"status": "saved"},
    ]
    sb = MagicMock()
    sb.table.side_effect = lambda name: _Chain(
        _Resp(score_rows if name == "scores" else job_rows)
    )

    counts = _pipeline_counts_python(
        sb, target_ids={"t1", "t2"}, min_score=None
    )
    assert counts == {"new": 1, "saved": 1}


def test_grouped_uses_rpc_result() -> None:
    sb = MagicMock()
    sb.rpc.return_value.execute.return_value = _Resp(
        [
            {"status": "new", "count": 12},
            {"status": "applied", "count": 5},
        ]
    )
    counts = _pipeline_counts_grouped(sb, target_ids={"t1"}, min_score=70)
    assert counts == {"new": 12, "applied": 5}
    sb.rpc.assert_called_once_with(
        "pipeline_counts", {"p_target_ids": ["t1"], "p_min_score": 70}
    )


def test_grouped_falls_back_when_rpc_missing() -> None:
    sb = MagicMock()
    sb.rpc.return_value.execute.side_effect = Exception("function not found")
    sb.table.side_effect = lambda name: _Chain(
        _Resp(
            [{"job_posting_id": "j1"}]
            if name == "scores"
            else [{"status": "interviewing"}]
        )
    )
    counts = _pipeline_counts_grouped(sb, target_ids={"t1"}, min_score=None)
    assert counts == {"interviewing": 1}


def test_endpoint_zero_fills_all_statuses(monkeypatch) -> None:
    import app.routers.jobs as jobs_mod

    monkeypatch.setattr(
        jobs_mod, "get_user_target_ids", lambda _sb, _uid: {"t1"}
    )
    monkeypatch.setattr(
        jobs_mod, "_default_min_score_for_user", lambda _sb, _uid: None
    )
    monkeypatch.setattr(
        jobs_mod,
        "_pipeline_counts_grouped",
        lambda _sb, *, target_ids, min_score: {"new": 3, "offer": 1},
    )

    counts = pipeline_counts(supabase=MagicMock(), user_id="u1")
    assert counts["new"] == 3
    assert counts["offer"] == 1
    # Statuses with no rows are present and zero — the dashboard reads
    # every pipeline status unconditionally.
    for st in (
        "saved",
        "resume_draft",
        "resume_ready",
        "applied",
        "interviewing",
        "rejected",
        "archived",
    ):
        assert counts[st] == 0


def test_endpoint_no_targets_short_circuits() -> None:
    from unittest.mock import patch

    import app.routers.jobs as jobs_mod

    sb = MagicMock()
    with patch.object(jobs_mod, "get_user_target_ids", return_value=set()):
        counts = pipeline_counts(supabase=sb, user_id="u-none")
    assert all(v == 0 for v in counts.values())
    sb.rpc.assert_not_called()


def test_endpoint_caches_per_user() -> None:
    from unittest.mock import patch

    import app.routers.jobs as jobs_mod

    grouped = MagicMock(return_value={"new": 2})
    with (
        patch.object(jobs_mod, "get_user_target_ids", return_value={"t1"}),
        patch.object(
            jobs_mod, "_default_min_score_for_user", return_value=None
        ),
        patch.object(jobs_mod, "_pipeline_counts_grouped", grouped),
    ):
        first = pipeline_counts(supabase=MagicMock(), user_id="u1")
        second = pipeline_counts(supabase=MagicMock(), user_id="u1")
        # Different user must not share the cache entry.
        pipeline_counts(supabase=MagicMock(), user_id="u2")

    assert first == second
    assert grouped.call_count == 2  # u1 once (cached), u2 once
