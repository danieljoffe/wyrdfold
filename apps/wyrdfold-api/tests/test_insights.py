"""Tests for insights aggregation logic (#512)."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from app.models.insights import FunnelStage
from app.services.insights import (
    _fetch_in_chunks,
    compute_pipeline,
    compute_skills_cost,
    compute_targets,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now(UTC)
_WEEK_AGO = _NOW - timedelta(days=7)


def _ts(dt: datetime) -> str:
    return dt.isoformat()


def _mock_supabase(tables: dict[str, list[dict]]) -> MagicMock:
    """Build a MagicMock that simulates chained Supabase queries.

    *tables* maps table name → list of dicts that .execute().data returns.
    Every chained method (.select, .eq, .gte, .order, .limit) returns the
    same mock, so the final .execute() always resolves.
    """
    client = MagicMock()

    def table_side_effect(name: str) -> MagicMock:
        tbl = MagicMock()
        result = MagicMock()
        result.data = tables.get(name, [])

        # Every chainable method returns the same table mock
        for method in ("select", "eq", "gte", "lt", "lte", "order", "limit", "neq", "in_"):
            getattr(tbl, method).return_value = tbl
        tbl.execute.return_value = result
        return tbl

    client.table.side_effect = table_side_effect
    return client


def _user_jobs(postings: list[dict]) -> list[dict]:
    """#75 C4: per-user pipeline status moved off ``jobs.status`` into
    ``user_jobs``. Derive the ``user_jobs`` rows the compute functions read
    from the per-posting ``status`` the test seeds (postings stay status-less
    in the ``jobs`` table). Omit 'new' rows — absent reads back as 'new'."""
    return [
        {"job_posting_id": p["id"], "status": p["status"]}
        for p in postings
        if p.get("status") and p["status"] != "new"
    ]


# All per-user-status insights tests run as this synthetic user.
_USER = "u1"


# ===========================================================================
# Pipeline
# ===========================================================================


class TestComputePipeline:
    def test_basic_funnel_counts(self):
        postings = [
            {"id": "1", "status": "new", "created_at": _ts(_NOW)},
            {"id": "2", "status": "new", "created_at": _ts(_NOW)},
            {"id": "3", "status": "applied", "created_at": _ts(_NOW)},
            {"id": "4", "status": "interviewing", "created_at": _ts(_NOW)},
            {"id": "5", "status": "offer", "created_at": _ts(_NOW)},
        ]
        sb = _mock_supabase({"jobs": postings, "user_jobs": _user_jobs(postings)})
        result = compute_pipeline(sb, since=None, user_id=_USER)

        assert result.total_applications == 3  # applied + interviewing + offer
        assert result.total_interviews == 2  # interviewing + offer
        assert result.total_offers == 1

        funnel_map = {f.stage: f.count for f in result.funnel}
        assert funnel_map["new"] == 2
        assert funnel_map["applied"] == 1
        assert funnel_map["interviewing"] == 1
        assert funnel_map["offer"] == 1

    def test_response_rate(self):
        postings = [
            {"id": "1", "status": "applied", "created_at": _ts(_NOW)},
            {"id": "2", "status": "applied", "created_at": _ts(_NOW)},
            {"id": "3", "status": "interviewing", "created_at": _ts(_NOW)},
            {"id": "4", "status": "offer", "created_at": _ts(_NOW)},
        ]
        sb = _mock_supabase({"jobs": postings, "user_jobs": _user_jobs(postings)})
        result = compute_pipeline(sb, since=None, user_id=_USER)

        # 4 applied-or-beyond, 2 interviewing-or-beyond → 0.5
        assert result.response_rate == 0.5

    def test_avg_days_to_response(self):
        logs = [
            {
                "posting_id": "1",
                "old_status": "resume_ready",
                "new_status": "applied",
                "created_at": _ts(_NOW - timedelta(days=10)),
            },
            {
                "posting_id": "1",
                "old_status": "applied",
                "new_status": "interviewing",
                "created_at": _ts(_NOW - timedelta(days=4)),
            },
        ]
        postings = [{"id": "1", "status": "interviewing", "created_at": _ts(_NOW)}]
        sb = _mock_supabase({"jobs": postings, "status_log": logs})
        result = compute_pipeline(sb, since=None)

        assert result.avg_days_to_response == 6.0

    def test_empty_data(self):
        sb = _mock_supabase({})
        result = compute_pipeline(sb, since=None)

        assert result.total_applications == 0
        assert result.response_rate is None
        assert result.avg_days_to_response is None
        assert result.velocity == []

    def test_velocity_grouping(self):
        resumes = [
            {"job_posting_id": "1", "created_at": _ts(_NOW)},
            {"job_posting_id": "2", "created_at": _ts(_NOW - timedelta(days=1))},
            {"job_posting_id": "3", "created_at": _ts(_NOW - timedelta(days=8))},
        ]
        sb = _mock_supabase({"documents": resumes})
        result = compute_pipeline(sb, since=None)

        # Should group into weeks — at least 1 or 2 week buckets
        assert len(result.velocity) >= 1
        total_resumes = sum(v.resumes_generated for v in result.velocity)
        assert total_resumes == 3

    def test_previous_is_none_without_prior_window(self):
        sb = _mock_supabase({})
        result = compute_pipeline(sb, since=None)
        assert result.previous is None

    def test_previous_populated_when_prior_window_supplied(self):
        # Mock supabase ignores filter args, so both windows resolve to the
        # same row set. We're asserting that a prior_window triggers the
        # second aggregation pass — not the math, which is covered above.
        postings = [
            {"id": "1", "status": "applied", "created_at": _ts(_NOW)},
            {"id": "2", "status": "interviewing", "created_at": _ts(_NOW)},
        ]
        sb = _mock_supabase({"jobs": postings, "user_jobs": _user_jobs(postings)})
        prior_until = _NOW - timedelta(days=30)
        prior_since = _NOW - timedelta(days=60)
        result = compute_pipeline(
            sb,
            since=_NOW - timedelta(days=30),
            prior_window=(prior_since, prior_until),
            user_id=_USER,
        )
        assert result.previous is not None
        assert result.previous.total_applications == 2
        assert result.previous.total_interviews == 1
        assert result.previous.total_offers == 0
        assert result.previous.response_rate == 0.5


# ===========================================================================
# #101 regression: GROUP BY RPC for the pipeline status tally is byte-identical
# ===========================================================================
#
# compute_pipeline's funnel + KPI status distribution now come from the
# `insights_pipeline_status_counts` GROUP BY RPC instead of fetching every
# posting + every user_jobs row and Counting in Python. These tests build a
# representative dataset, run a FAITHFUL Supabase mock (one that actually
# applies .in_/.eq/.gte/.lt filters AND simulates the RPC's GROUP BY against
# the same seeded rows the way Postgres would), and assert the RPC-backed
# output equals a reference re-implementation of the OLD fetch+Python-aggregate
# path. If the RPC ever diverges from the Python it replaced, this fails.


class _FaithfulQuery:
    """A built query that actually applies the filters the production code
    chains, so the seeded rows it returns match what PostgREST would."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def select(self, *_a: object, **_kw: object) -> _FaithfulQuery:
        return self

    def eq(self, col: str, val: object) -> _FaithfulQuery:
        return _FaithfulQuery([r for r in self._rows if r.get(col) == val])

    def neq(self, col: str, val: object) -> _FaithfulQuery:
        return _FaithfulQuery([r for r in self._rows if r.get(col) != val])

    def in_(self, col: str, vals: list) -> _FaithfulQuery:
        s = set(vals)
        return _FaithfulQuery([r for r in self._rows if r.get(col) in s])

    def gte(self, col: str, val: str) -> _FaithfulQuery:
        return _FaithfulQuery([r for r in self._rows if r.get(col) is not None and r[col] >= val])

    def lt(self, col: str, val: str) -> _FaithfulQuery:
        return _FaithfulQuery([r for r in self._rows if r.get(col) is not None and r[col] < val])

    def lte(self, col: str, val: str) -> _FaithfulQuery:
        return _FaithfulQuery([r for r in self._rows if r.get(col) is not None and r[col] <= val])

    def order(self, *_a: object, **_kw: object) -> _FaithfulQuery:
        return self

    def limit(self, *_a: object, **_kw: object) -> _FaithfulQuery:
        return self

    def execute(self) -> MagicMock:
        resp = MagicMock()
        resp.data = self._rows
        return resp


def _rpc_status_counts(
    seed: dict[str, list[dict]], params: dict
) -> list[dict]:
    """Simulate the `insights_pipeline_status_counts` GROUP BY exactly as the
    Postgres function does: scores JOIN jobs LEFT JOIN user_jobs, scoped to
    p_target_ids + the created_at window, COUNT(DISTINCT job_posting_id)
    grouped by COALESCE(uj.status,'new')."""
    target_ids = set(params["p_target_ids"])
    since = params["p_since"]
    until = params["p_until"]
    user_id = params["p_user_id"]
    jobs_by_id = {j["id"]: j for j in seed.get("jobs", [])}
    uj_status = {
        uj["job_posting_id"]: uj["status"]
        for uj in seed.get("user_jobs", [])
        if uj.get("user_id") == user_id
    }
    by_status: dict[str, set] = {}
    for s in seed.get("scores", []):
        if s["target_id"] not in target_ids or s.get("excluded"):
            continue
        job = jobs_by_id.get(s["job_posting_id"])
        if job is None:
            continue
        created = job.get("created_at")
        if since is not None and not (created is not None and created >= since):
            continue
        if until is not None and not (created is not None and created < until):
            continue
        status = uj_status.get(s["job_posting_id"], "new")
        by_status.setdefault(status, set()).add(s["job_posting_id"])
    return [{"status": st, "count": len(ids)} for st, ids in by_status.items()]


def _faithful_supabase(seed: dict[str, list[dict]]) -> MagicMock:
    """Mock that applies real filters per table AND answers the RPC by running
    the GROUP BY against the same seeded rows (simulating Postgres)."""
    client = MagicMock()
    client.table.side_effect = lambda name: _FaithfulQuery(seed.get(name, []))

    def rpc_side_effect(fn: str, params: dict) -> MagicMock:
        assert fn == "insights_pipeline_status_counts"
        resp = MagicMock()
        resp.data = _rpc_status_counts(seed, params)
        call = MagicMock()
        call.execute.return_value = resp
        return call

    client.rpc.side_effect = rpc_side_effect
    return client


def _reference_status_counts(
    seed: dict[str, list[dict]],
    *,
    win_since,
    win_until,
    target_ids: set[str],
    user_id: str,
) -> Counter[str]:
    """Independent re-implementation of the OLD fetch+Python-aggregate funnel /
    KPI status path: count each DISTINCT posting under the caller's targets
    (non-excluded score) within the window by its per-user status (absent →
    'new'). Compares created_at as ISO strings, exactly like the production
    PostgREST filters (.gte/.lt with ``.isoformat()``)."""
    member = {
        s["job_posting_id"]
        for s in seed.get("scores", [])
        if s["target_id"] in target_ids and not s.get("excluded")
    }
    jobs_by_id = {j["id"]: j for j in seed.get("jobs", [])}
    uj_status = {
        uj["job_posting_id"]: uj["status"]
        for uj in seed.get("user_jobs", [])
        if uj.get("user_id") == user_id
    }
    since_iso = win_since.isoformat() if win_since else None
    until_iso = win_until.isoformat() if win_until else None
    counts: Counter[str] = Counter()
    for pid in member:
        job = jobs_by_id.get(pid)
        if job is None:
            continue
        created = job.get("created_at")
        if since_iso is not None and not (created is not None and created >= since_iso):
            continue
        if until_iso is not None and not (created is not None and created < until_iso):
            continue
        counts[uj_status.get(pid, "new")] += 1
    return counts


class TestPipelineGroupByRpcByteIdentical:
    def _seed(self) -> dict[str, list[dict]]:
        # Representative dataset: 2 targets, postings across statuses, one
        # posting scored against BOTH targets (dedupe), one excluded score
        # (must not count), one posting outside the window, a few status_log
        # rows and resumes for velocity.
        recent = _ts(_NOW)
        old = _ts(_NOW - timedelta(days=120))
        jobs = [
            {"id": "p1", "created_at": recent},  # new (no user_jobs)
            {"id": "p2", "created_at": recent},  # saved
            {"id": "p3", "created_at": recent},  # applied
            {"id": "p4", "created_at": recent},  # interviewing
            {"id": "p5", "created_at": recent},  # offer
            {"id": "p6", "created_at": recent},  # scored vs both targets → once
            {"id": "p7", "created_at": old},  # outside the 30d window
            {"id": "p8", "created_at": recent},  # only an excluded score → drop
        ]
        scores = [
            {"job_posting_id": "p1", "target_id": "t1", "excluded": False},
            {"job_posting_id": "p2", "target_id": "t1", "excluded": False},
            {"job_posting_id": "p3", "target_id": "t1", "excluded": False},
            {"job_posting_id": "p4", "target_id": "t2", "excluded": False},
            {"job_posting_id": "p5", "target_id": "t2", "excluded": False},
            {"job_posting_id": "p6", "target_id": "t1", "excluded": False},
            {"job_posting_id": "p6", "target_id": "t2", "excluded": False},
            {"job_posting_id": "p7", "target_id": "t1", "excluded": False},
            {"job_posting_id": "p8", "target_id": "t1", "excluded": True},
        ]
        user_jobs = [
            {"job_posting_id": "p2", "user_id": _USER, "status": "saved"},
            {"job_posting_id": "p3", "user_id": _USER, "status": "applied"},
            {"job_posting_id": "p4", "user_id": _USER, "status": "interviewing"},
            {"job_posting_id": "p5", "user_id": _USER, "status": "offer"},
            {"job_posting_id": "p6", "user_id": _USER, "status": "applied"},
            # a different user's row must be ignored by the per-user join
            {"job_posting_id": "p1", "user_id": "other", "status": "offer"},
        ]
        status_log = [
            {
                "posting_id": "p4",
                "old_status": "applied",
                "new_status": "interviewing",
                "created_at": _ts(_NOW - timedelta(days=2)),
            },
            {
                "posting_id": "p4",
                "old_status": "resume_ready",
                "new_status": "applied",
                "created_at": _ts(_NOW - timedelta(days=8)),
            },
        ]
        documents = [
            {"job_posting_id": "p3", "document_type": "resume", "created_at": recent},
        ]
        return {
            "jobs": jobs,
            "scores": scores,
            "user_jobs": user_jobs,
            "status_log": status_log,
            "documents": documents,
        }

    def test_rpc_backed_matches_python_reference(self):
        from app.services.insights import (
            FUNNEL_ORDER,
            _fetch_status_logs_window,
            _kpis_from,
        )

        seed = self._seed()
        since = _NOW - timedelta(days=30)
        prior = (_NOW - timedelta(days=60), _NOW - timedelta(days=30))
        targets = {"t1", "t2"}

        result = compute_pipeline(
            _faithful_supabase(seed),
            since,
            prior_window=prior,
            target_ids=targets,
            user_id=_USER,
        )

        # --- Independent Python recompute of funnel + KPIs ---
        ref_counts = _reference_status_counts(
            seed, win_since=since, win_until=None,
            target_ids=targets, user_id=_USER,
        )
        ref_funnel = [
            FunnelStage(stage=s, count=ref_counts.get(s, 0)) for s in FUNNEL_ORDER
        ]
        # status_log scoping is unchanged code — drive it through the faithful
        # mock against the same window posting-id set the production path uses.
        member = {
            s["job_posting_id"]
            for s in seed["scores"]
            if s["target_id"] in targets and not s["excluded"]
        }
        jobs_by_id = {j["id"]: j for j in seed["jobs"]}
        win_pids = {
            pid
            for pid in member
            if jobs_by_id[pid]["created_at"] >= since.isoformat()
        }
        ref_logs = _fetch_status_logs_window(
            _faithful_supabase(seed), since, None, win_pids
        )
        ref_current = _kpis_from(ref_counts, ref_logs)

        # Funnel is byte-identical to the Python recompute.
        assert result.funnel == ref_funnel
        # Hand-computed funnel: p1=new, p2=saved, p3=applied, p4=interviewing,
        # p5=offer, p6=applied (scored vs both → counts ONCE), p7 out of
        # window, p8 excluded. → new1 saved1 applied2 interviewing1 offer1.
        funnel_map = {f.stage: f.count for f in result.funnel}
        assert funnel_map == {
            "new": 1, "saved": 1, "resume_draft": 0, "resume_ready": 0,
            "applied": 2, "interviewing": 1, "offer": 1,
        }

        # Top-line KPIs byte-identical to the recompute.
        assert result.total_applications == ref_current.total_applications
        assert result.total_interviews == ref_current.total_interviews
        assert result.total_offers == ref_current.total_offers
        assert result.response_rate == ref_current.response_rate
        assert result.avg_days_to_response == ref_current.avg_days_to_response
        # Hand-computed: applied = applied+interviewing+offer = 2+1+1 = 4;
        # interviews = interviewing+offer = 1+1 = 2; offers = 1;
        # response_rate = 2/4 = 0.5; avg_days = (8-2) = 6.0 for p4.
        assert result.total_applications == 4
        assert result.total_interviews == 2
        assert result.total_offers == 1
        assert result.response_rate == 0.5
        assert result.avg_days_to_response == 6.0

        # Prior-window KPIs come from the same RPC for that window. In the seed
        # all live postings are in the current window, so the prior window is
        # empty → zeroed KPIs.
        assert result.previous is not None
        assert result.previous.total_applications == 0
        assert result.previous.total_interviews == 0
        assert result.previous.total_offers == 0

    def test_rpc_path_uses_the_rpc_not_a_posting_scan(self):
        """The RPC must actually be invoked for the status tally — proving the
        ~11k posting + user_jobs counting is gone in the deployed path."""
        seed = self._seed()
        sb = _faithful_supabase(seed)
        compute_pipeline(
            sb,
            _NOW - timedelta(days=30),
            target_ids={"t1", "t2"},
            user_id=_USER,
        )
        called = [c.args[0] for c in sb.rpc.call_args_list]
        assert "insights_pipeline_status_counts" in called

    def test_fallback_equals_rpc_when_rpc_unavailable(self):
        """When the RPC isn't deployed yet the client-side fallback must
        produce the identical funnel — mid-deploy safety."""
        seed = self._seed()
        since = _NOW - timedelta(days=30)

        rpc_result = compute_pipeline(
            _faithful_supabase(seed), since,
            target_ids={"t1", "t2"}, user_id=_USER,
        )

        # Same faithful table behaviour, but the RPC raises (not deployed).
        sb = _faithful_supabase(seed)
        sb.rpc.side_effect = Exception("function not found")
        fallback_result = compute_pipeline(
            sb, since, target_ids={"t1", "t2"}, user_id=_USER,
        )

        assert fallback_result.funnel == rpc_result.funnel
        assert fallback_result.total_applications == rpc_result.total_applications
        assert fallback_result.total_interviews == rpc_result.total_interviews
        assert fallback_result.total_offers == rpc_result.total_offers
        assert fallback_result.response_rate == rpc_result.response_rate


# ===========================================================================
# Targets
# ===========================================================================


class TestComputeTargets:
    def test_basic_target_comparison(self):
        targets = [
            {"id": "t1", "label": "Frontend"},
            {"id": "t2", "label": "Backend"},
        ]
        postings = [
            {"id": "1", "target_id": "t1", "score": 80, "status": "applied", "created_at": _ts(_NOW)},
            {"id": "2", "target_id": "t1", "score": 60, "status": "new", "created_at": _ts(_NOW)},
            {"id": "3", "target_id": "t2", "score": 90, "status": "interviewing", "created_at": _ts(_NOW)},
        ]
        sb = _mock_supabase(
            {"targets": targets, "jobs": postings, "user_jobs": _user_jobs(postings)}
        )
        result = compute_targets(sb, since=None, user_id=_USER)

        assert len(result.targets) == 2
        fe = next(t for t in result.targets if t.target_label == "Frontend")
        assert fe.job_count == 2
        assert fe.avg_score == 70.0
        assert fe.applied_count == 1

        be = next(t for t in result.targets if t.target_label == "Backend")
        assert be.job_count == 1
        assert be.avg_score == 90.0

    def test_targets_with_no_jobs_are_filtered_out(self):
        targets = [
            {"id": "t1", "label": "Frontend"},
            {"id": "t2", "label": "Backend"},
            {"id": "t3", "label": "DevOps"},  # no postings — should be dropped
        ]
        postings = [
            {"id": "1", "target_id": "t1", "score": 80, "status": "new", "created_at": _ts(_NOW)},
            {"id": "2", "target_id": "t2", "score": 70, "status": "new", "created_at": _ts(_NOW)},
        ]
        sb = _mock_supabase({"targets": targets, "jobs": postings})
        result = compute_targets(sb, since=None)

        labels = {t.target_label for t in result.targets}
        assert labels == {"Frontend", "Backend"}
        assert "DevOps" not in labels

    def test_unscored_postings_excluded_from_distribution(self):
        """Postings with score=None bump unscored_count, not the 0-10 bucket."""
        postings = [
            {"id": "1", "target_id": None, "score": None, "status": "new", "created_at": _ts(_NOW)},
            {"id": "2", "target_id": None, "score": None, "status": "new", "created_at": _ts(_NOW)},
            {"id": "3", "target_id": None, "score": 5, "status": "new", "created_at": _ts(_NOW)},
        ]
        sb = _mock_supabase({"jobs": postings})
        result = compute_targets(sb, since=None)

        assert result.unscored_count == 2
        bucket_map = {b.bucket: b.count for b in result.score_distribution}
        # Only the score=5 row contributes to 0-10
        assert bucket_map["0-10"] == 1

    def test_score_distribution(self):
        postings = [
            {"id": "1", "target_id": None, "score": 15, "status": "new", "created_at": _ts(_NOW)},
            {"id": "2", "target_id": None, "score": 85, "status": "new", "created_at": _ts(_NOW)},
            {"id": "3", "target_id": None, "score": 85, "status": "new", "created_at": _ts(_NOW)},
        ]
        sb = _mock_supabase({"jobs": postings})
        result = compute_targets(sb, since=None)

        bucket_map = {b.bucket: b.count for b in result.score_distribution}
        assert bucket_map["10-20"] == 1
        assert bucket_map["80-90"] == 2
        assert bucket_map["0-10"] == 0

    def test_empty_targets(self):
        sb = _mock_supabase({})
        result = compute_targets(sb, since=None)

        assert result.targets == []
        assert len(result.score_distribution) == 10  # Always 10 buckets
        assert result.score_trend == []
        assert result.unscored_count == 0

    def test_score_trend(self):
        postings = [
            {"id": "1", "target_id": None, "score": 60, "status": "new", "created_at": _ts(_NOW)},
            {"id": "2", "target_id": None, "score": 80, "status": "new", "created_at": _ts(_NOW - timedelta(days=8))},
        ]
        sb = _mock_supabase({"jobs": postings})
        result = compute_targets(sb, since=None)

        # At least 1 week bucket
        assert len(result.score_trend) >= 1

    def test_per_user_path_excludes_excluded_scores(self):
        """Scores marked ``excluded=true`` (closed jobs, irrelevant matches)
        must not inflate the funnel / distribution. The list endpoint
        filters ``excluded = False`` and insights has to mirror that —
        otherwise pipeline funnel counts balloon by ~4x (one excluded
        scores row per posting per re-poll).

        This test mocks the mock to track that ``.eq("excluded", False)``
        is invoked on the ``scores`` table query — exact filter values
        can't be asserted with the current _mock_supabase shape, so we
        spy on the chain. A regression here would mean the production
        funnel returns counts that don't match the list view.
        """
        # The MagicMock chain returns the same table mock for every call,
        # so we record the .eq() arguments to assert excluded filter.
        eq_calls: list[tuple] = []

        sb = MagicMock()
        tbl = MagicMock()

        def eq_recorder(*args, **kwargs):
            eq_calls.append(args)
            return tbl

        tbl.select.return_value = tbl
        tbl.eq.side_effect = eq_recorder
        tbl.in_.return_value = tbl
        tbl.gte.return_value = tbl
        tbl.lt.return_value = tbl
        tbl.order.return_value = tbl
        tbl.limit.return_value = tbl
        tbl.execute.return_value.data = []
        sb.table.return_value = tbl

        compute_targets(sb, since=None, target_ids={"t1"})

        # At least one .eq("excluded", False) on the scores table path.
        assert ("excluded", False) in eq_calls, (
            f"expected scores query to filter excluded=False, "
            f"got eq() calls: {eq_calls}"
        )

    def test_per_user_path_pivots_through_scores_table(self):
        """When ``target_ids`` is passed, membership + score come from
        the ``scores`` table — ``jobs.target_id`` is vestigial and
        ``jobs.score`` is the global blended score, neither is
        authoritative for per-target rollups. Same architectural
        invariant as #676 / #678 ownership checks."""
        targets = [
            {"id": "t1", "label": "Frontend"},
            {"id": "t2", "label": "Backend"},
        ]
        # Postings carry NO ``target_id`` or ``score`` — production
        # reality (the poller writes both into ``scores``, not back
        # onto ``jobs``).
        postings = [
            {"id": "1", "status": "applied", "created_at": _ts(_NOW)},
            {"id": "2", "status": "new", "created_at": _ts(_NOW)},
            {"id": "3", "status": "interviewing", "created_at": _ts(_NOW)},
        ]
        scores = [
            {"job_posting_id": "1", "target_id": "t1", "score": 80},
            {"job_posting_id": "2", "target_id": "t1", "score": 60},
            {"job_posting_id": "3", "target_id": "t2", "score": 90},
        ]
        sb = _mock_supabase(
            {
                "targets": targets,
                "jobs": postings,
                "scores": scores,
                "user_jobs": _user_jobs(postings),
            }
        )
        result = compute_targets(
            sb, since=None, target_ids={"t1", "t2"}, user_id=_USER
        )

        assert len(result.targets) == 2
        fe = next(t for t in result.targets if t.target_label == "Frontend")
        assert fe.job_count == 2
        assert fe.avg_score == 70.0
        assert fe.applied_count == 1
        be = next(t for t in result.targets if t.target_label == "Backend")
        assert be.avg_score == 90.0
        assert be.interview_count == 1


# ===========================================================================
# Skills + Cost
# ===========================================================================


class TestComputeSkillsCost:
    def test_basic_skill_frequencies(self):
        analyses = [
            {
                "scorecard": {
                    "skills_matched": [
                        {"name": "Python", "matched": True, "confidence": "high", "evidence": ""},
                        {"name": "React", "matched": False, "confidence": "low", "evidence": ""},
                    ],
                    "skills_missing": ["Docker"],
                },
                "created_at": _ts(_NOW),
            },
            {
                "scorecard": {
                    "skills_matched": [
                        {"name": "Python", "matched": True, "confidence": "high", "evidence": ""},
                    ],
                    "skills_missing": ["React", "Docker"],
                },
                "created_at": _ts(_NOW),
            },
        ]
        sb = _mock_supabase({"analyses": analyses})
        result = compute_skills_cost(sb, since=None)

        skill_map = {s.skill: s for s in result.top_skills}
        assert "Python" in skill_map
        assert skill_map["Python"].matched_count == 2
        assert skill_map["Python"].missing_count == 0

        # React: 1 unmatched in skills_matched + 1 in skills_missing
        assert "React" in skill_map
        assert skill_map["React"].missing_count == 2

        assert "Docker" in skill_map
        assert skill_map["Docker"].missing_count == 2

        # Docker is never matched → should be in top_missing
        missing_skills = [m.skill for m in result.top_missing]
        assert "Docker" in missing_skills

    def test_top_missing_ranked_by_score_weighted_priority(self):
        """A skill missing from one 90-score job outranks a skill missing
        from two 30-score jobs, since 90 > 30+30."""
        analyses = [
            {
                "job_posting_id": "high-1",
                "scorecard": {
                    "skills_matched": [],
                    "skills_missing": ["Kubernetes"],
                },
                "created_at": _ts(_NOW),
            },
            {
                "job_posting_id": "low-1",
                "scorecard": {
                    "skills_matched": [],
                    "skills_missing": ["Rust"],
                },
                "created_at": _ts(_NOW),
            },
            {
                "job_posting_id": "low-2",
                "scorecard": {
                    "skills_matched": [],
                    "skills_missing": ["Rust"],
                },
                "created_at": _ts(_NOW),
            },
        ]
        postings = [
            {"id": "high-1", "llm_score": 90.0, "created_at": _ts(_NOW)},
            {"id": "low-1", "llm_score": 30.0, "created_at": _ts(_NOW)},
            {"id": "low-2", "llm_score": 30.0, "created_at": _ts(_NOW)},
        ]
        sb = _mock_supabase({"analyses": analyses, "jobs": postings})
        result = compute_skills_cost(sb, since=None)

        skills = [m.skill for m in result.top_missing]
        assert skills[0] == "Kubernetes"  # priority 90 beats 60
        assert skills[1] == "Rust"

        kubernetes = next(m for m in result.top_missing if m.skill == "Kubernetes")
        assert kubernetes.missing_count == 1
        assert kubernetes.avg_job_score == 90.0
        assert kubernetes.priority_score == 90.0

        rust = next(m for m in result.top_missing if m.skill == "Rust")
        assert rust.missing_count == 2
        assert rust.avg_job_score == 30.0
        assert rust.priority_score == 60.0

    def test_top_missing_falls_back_to_count_when_no_scores(self):
        """If no posting has llm_score, ranking should still produce a
        stable order using missing_count."""
        analyses = [
            {
                "job_posting_id": "p1",
                "scorecard": {"skills_matched": [], "skills_missing": ["A", "A", "B"]},
                "created_at": _ts(_NOW),
            },
        ]
        # postings with no llm_score
        postings = [{"id": "p1", "llm_score": None, "created_at": _ts(_NOW)}]
        sb = _mock_supabase({"analyses": analyses, "jobs": postings})
        result = compute_skills_cost(sb, since=None)

        by_skill = {m.skill: m for m in result.top_missing}
        assert by_skill["A"].avg_job_score is None
        assert by_skill["A"].priority_score == 2.0  # missing_count fallback
        assert by_skill["B"].priority_score == 1.0
        # A ranks above B
        assert result.top_missing[0].skill == "A"

    def test_cost_over_time(self):
        resume_costs = [
            {"cost_usd": "0.0050", "created_at": _ts(_NOW)},
            {"cost_usd": "0.0030", "created_at": _ts(_NOW - timedelta(days=1))},
        ]
        cost_logs = [
            {"purpose": "tailor", "cost_usd": "0.0080", "created_at": _ts(_NOW)},
        ]
        sb = _mock_supabase({
            "documents": resume_costs,
            "llm_costs": cost_logs,
        })
        result = compute_skills_cost(sb, since=None)

        assert result.total_cost == 0.008
        assert result.avg_cost_per_resume is not None
        assert len(result.cost_over_time) >= 1
        total_resume_cost = sum(c.total_cost for c in result.cost_over_time)
        assert total_resume_cost == 0.008

    def test_cost_by_purpose(self):
        cost_logs = [
            {"purpose": "tailor", "cost_usd": "0.01", "created_at": _ts(_NOW)},
            {"purpose": "tailor", "cost_usd": "0.02", "created_at": _ts(_NOW)},
            {"purpose": "analysis", "cost_usd": "0.005", "created_at": _ts(_NOW)},
        ]
        sb = _mock_supabase({"llm_costs": cost_logs})
        result = compute_skills_cost(sb, since=None)

        purpose_map = {p.purpose: p for p in result.cost_by_purpose}
        assert purpose_map["tailor"].total_cost == 0.03
        assert purpose_map["tailor"].call_count == 2
        assert purpose_map["analysis"].total_cost == 0.005

    def test_empty_data(self):
        sb = _mock_supabase({})
        result = compute_skills_cost(sb, since=None)

        assert result.top_skills == []
        assert result.top_missing == []
        assert result.cost_over_time == []
        assert result.total_cost == 0.0
        assert result.avg_cost_per_resume is None


# ===========================================================================
# IN-list chunking (#93)
# ===========================================================================
#
# A real beta user has ~11,300 postings under their targets. Passing all
# of them into a single ``.in_("id", [...])`` builds a ~400KB PostgREST URL
# that gets truncated/rejected — silently dropping rows. ``_fetch_in_chunks``
# splits the id filter into batches of 200 and concatenates the rows so the
# union equals the single-query result (callers fold into order-independent
# Counters/dicts).


class TestFetchInChunks:
    def test_batches_at_200_and_concatenates(self):
        """A >200-id list is split into 200-id batches; ``make_query`` is
        called once per batch and the rows are concatenated in order."""
        ids = [f"id-{i}" for i in range(450)]  # 200 + 200 + 50 → 3 batches

        seen_batches: list[list[str]] = []

        def make_query(batch: list[str]) -> MagicMock:
            seen_batches.append(batch)
            q = MagicMock()
            # Each chunk returns one row per id so we can verify the union.
            q.execute.return_value.data = [{"id": i} for i in batch]
            return q

        rows = _fetch_in_chunks(make_query, ids, label="test")

        # One call per 200-id chunk: 200, 200, 50.
        assert [len(b) for b in seen_batches] == [200, 200, 50]
        # Batches partition the input with no overlap or gaps.
        assert [i for b in seen_batches for i in b] == ids
        # Union of chunk rows == what a single ``.in_(all_ids)`` would return.
        assert [r["id"] for r in rows] == ids

    def test_single_batch_when_under_chunk_size(self):
        ids = [f"id-{i}" for i in range(5)]
        calls = 0

        def make_query(batch: list[str]) -> MagicMock:
            nonlocal calls
            calls += 1
            q = MagicMock()
            q.execute.return_value.data = [{"id": i} for i in batch]
            return q

        rows = _fetch_in_chunks(make_query, ids, label="test")

        assert calls == 1
        assert [r["id"] for r in rows] == ids

    def test_exact_multiple_of_chunk_size(self):
        ids = [f"id-{i}" for i in range(400)]  # exactly 2 batches
        batch_sizes: list[int] = []

        def make_query(batch: list[str]) -> MagicMock:
            batch_sizes.append(len(batch))
            q = MagicMock()
            q.execute.return_value.data = [{"id": i} for i in batch]
            return q

        rows = _fetch_in_chunks(make_query, ids, label="test")

        assert batch_sizes == [200, 200]
        assert len(rows) == 400

    def test_custom_chunk_size(self):
        ids = [f"id-{i}" for i in range(5)]
        batch_sizes: list[int] = []

        def make_query(batch: list[str]) -> MagicMock:
            batch_sizes.append(len(batch))
            q = MagicMock()
            q.execute.return_value.data = []
            return q

        _fetch_in_chunks(make_query, ids, label="test", chunk=2)

        assert batch_sizes == [2, 2, 1]


def _chunk_tracking_supabase(
    tables: dict[str, list[dict]],
    in_batches: dict[str, list[list]],
) -> MagicMock:
    """Like ``_mock_supabase`` but records the ids passed to each
    ``.in_(col, ids)`` call, keyed by table name, into *in_batches*.

    Each table mock returns its seeded ``tables`` rows on ``.execute()``
    regardless of the batch — the test asserts on the recorded batch sizes,
    not on per-batch row filtering (the production helper concatenates, so
    returning the full set per batch is fine for the call-count assertion).
    """
    client = MagicMock()

    def table_side_effect(name: str) -> MagicMock:
        tbl = MagicMock()
        result = MagicMock()
        result.data = tables.get(name, [])

        for method in ("select", "eq", "gte", "lt", "lte", "order", "limit", "neq"):
            getattr(tbl, method).return_value = tbl

        def in_recorder(col: str, ids: list, _name: str = name) -> MagicMock:
            in_batches.setdefault(_name, []).append(list(ids))
            return tbl

        tbl.in_.side_effect = in_recorder
        tbl.execute.return_value = result
        return tbl

    client.table.side_effect = table_side_effect
    return client


class TestComputeChunksLargeIdLists:
    """End-to-end: a target-scoped compute over >200 postings issues the
    posting-id ``.in_(...)`` filters in 200-id batches (#93)."""

    def test_compute_pipeline_chunks_posting_id_filters(self):
        # 250 postings → 2 batches (200 + 50) for every posting-id .in_().
        n = 250
        scores = [
            {"job_posting_id": f"p{i}", "target_id": "t1"} for i in range(n)
        ]
        postings = [
            {"id": f"p{i}", "created_at": _ts(_NOW)} for i in range(n)
        ]
        in_batches: dict[str, list[list]] = {}
        sb = _chunk_tracking_supabase(
            {"scores": scores, "jobs": postings, "user_jobs": [], "documents": []},
            in_batches,
        )

        compute_pipeline(sb, since=_WEEK_AGO, target_ids={"t1"}, user_id=_USER)

        # jobs (postings window), status_log, documents (resumes), and
        # user_jobs all filter by the resolved posting-id set in 200-id
        # batches. The mock returns the full posting set for every table,
        # so each of those resolves a 250-id list → [200, 50].
        for table in ("jobs", "status_log", "documents"):
            sizes = [len(b) for b in in_batches.get(table, [])]
            assert sizes and all(s <= 200 for s in sizes), (
                f"{table} not chunked at 200: {sizes}"
            )
            assert sizes[0] == 200

    def test_compute_targets_chunks_posting_id_filters(self):
        n = 250
        targets = [{"id": "t1", "label": "Frontend"}]
        scores = [
            {"job_posting_id": f"p{i}", "target_id": "t1", "score": 50}
            for i in range(n)
        ]
        postings = [{"id": f"p{i}", "created_at": _ts(_NOW)} for i in range(n)]
        in_batches: dict[str, list[list]] = {}
        sb = _chunk_tracking_supabase(
            {
                "targets": targets,
                "scores": scores,
                "jobs": postings,
                "user_jobs": [],
            },
            in_batches,
        )

        compute_targets(sb, since=_WEEK_AGO, target_ids={"t1"}, user_id=_USER)

        # jobs (postings) chunked by id; scores chunked by job_posting_id.
        for table in ("jobs", "scores"):
            sizes = [len(b) for b in in_batches.get(table, [])]
            big = [s for s in sizes if s > 1]  # ignore the small target_id in_()
            assert big and all(s <= 200 for s in big), (
                f"{table} not chunked at 200: {sizes}"
            )

    def test_compute_skills_cost_chunks_posting_id_filters(self):
        n = 250
        scores = [
            {"job_posting_id": f"p{i}", "target_id": "t1"} for i in range(n)
        ]
        postings = [
            {"id": f"p{i}", "llm_score": 50.0, "created_at": _ts(_NOW)}
            for i in range(n)
        ]
        in_batches: dict[str, list[list]] = {}
        sb = _chunk_tracking_supabase(
            {
                "scores": scores,
                "jobs": postings,
                "analyses": [],
                "llm_costs": [],
                "documents": [],
            },
            in_batches,
        )

        compute_skills_cost(sb, since=_WEEK_AGO, target_ids={"t1"}, user_id=_USER)

        # jobs (id), analyses (job_posting_id), documents (job_posting_id)
        # all chunked at 200.
        for table in ("jobs", "analyses", "documents"):
            sizes = [len(b) for b in in_batches.get(table, [])]
            assert sizes and all(s <= 200 for s in sizes), (
                f"{table} not chunked at 200: {sizes}"
            )
            assert sizes[0] == 200
