"""Tests for target-aware scoring v2 (#502).

Covers: score_and_upsert, bulk_score_for_target, get_target_scores,
poller integration, list endpoint overlay, re-score endpoint.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.schemas import ScoreBreakdown
from app.models.targets import (
    CategoryProfile,
    DomainProfile,
    JobTarget,
    NegativeProfile,
    ScoringProfile,
    SeniorityProfile,
)
from app.services.target_scoring import (
    bulk_score_for_target,
    bulk_title_score_for_target,
    get_target_scores,
    score_and_upsert,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _target(
    *,
    target_id: str = "target-1",
    core: dict[str, int] | None = None,
    app_active: bool = True,
) -> JobTarget:
    cats: dict[str, CategoryProfile] = {}
    if core is not None:
        cats["core_skills"] = CategoryProfile(keywords=core, weight=2.0)
    return JobTarget(
        id=target_id,
        label="Senior FE",
        scoring_profile=ScoringProfile(
            categories=cats,
            seniority=SeniorityProfile(level="senior", signals=["5+ years"]),
            domain=DomainProfile(signals=["fintech"], weight=0.5),
            negative=NegativeProfile(keywords=["junior"], weight=-10.0),
        ),
        app_active=app_active,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _upserted_score_row(
    *,
    score: int = 70,
    job_posting_id: str = "job-1",
    target_id: str = "target-1",
) -> dict[str, Any]:
    return {
        "id": "score-1",
        "job_posting_id": job_posting_id,
        "target_id": target_id,
        "score": score,
        "score_breakdown": ScoreBreakdown(
            role_titles=0,
            technologies=12.0,
            domain_skills=0,
            seniority_signals=0,
            negative=0,
        ).model_dump(),
        "matched_keywords": ["React", "TypeScript"],
        "excluded": False,
        "created_at": datetime.now(UTC).isoformat(),
        "updated_at": datetime.now(UTC).isoformat(),
    }


def _make_supabase_mock(
    *,
    upsert_data: list[dict[str, Any]] | None = None,
    select_data: list[dict[str, Any]] | None = None,
) -> MagicMock:
    supabase = MagicMock()
    # upsert chain
    supabase.table.return_value.upsert.return_value.execute.return_value.data = upsert_data or []
    # select chain (for get_target_scores / bulk_score_for_target)
    supabase.table.return_value.select.return_value.eq.return_value.in_.return_value.execute.return_value.data = (
        select_data or []
    )
    supabase.table.return_value.select.return_value.eq.return_value.execute.return_value.data = (
        select_data or []
    )
    # For bulk_score_for_target: range query on jobs
    supabase.table.return_value.select.return_value.range.return_value.execute.return_value.data = (
        select_data or []
    )
    # #93: get_target_scores' explicit-id-list branch is the
    # ``get_target_scores_by_ids`` RPC now (ids in the jsonb body, not the
    # URL) instead of ``scores.select().eq().in_()``.
    supabase.rpc.return_value.execute.return_value.data = select_data or []
    return supabase


# ---------------------------------------------------------------------------
# score_and_upsert
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_score_and_upsert_calls_upsert_with_correct_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seam_sync_fallback(monkeypatch)
    row = _upserted_score_row()
    supabase = _make_supabase_mock(upsert_data=[row])
    target = _target(core={"React": 3, "TypeScript": 3})

    result = await score_and_upsert(
        supabase,
        job_posting_id="job-1",
        title="Senior Frontend Engineer",
        description_html="<p>React and TypeScript required.</p>",
        target=target,
    )

    assert result.job_posting_id == "job-1"
    assert result.target_id == "target-1"
    # Verify upsert was called on the right table
    supabase.table.assert_any_call("scores")


@pytest.mark.asyncio
async def test_score_and_upsert_raises_on_empty_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seam_sync_fallback(monkeypatch)
    supabase = _make_supabase_mock(upsert_data=[])
    target = _target(core={"React": 3})

    with pytest.raises(RuntimeError, match="Failed to upsert"):
        await score_and_upsert(
            supabase,
            job_posting_id="job-1",
            title="Engineer",
            description_html="<p>React.</p>",
            target=target,
        )


@pytest.mark.asyncio
async def test_score_and_upsert_excluded_by_prefilter_forces_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the caller signals a prefilter rejection, the upserted row
    must carry ``excluded=True`` regardless of what the keyword scorer
    decided. This is the contract the poller relies on so that re-scores
    preserve cosine exclusions.
    """
    _seam_sync_fallback(monkeypatch)
    supabase = _make_supabase_mock(upsert_data=[_upserted_score_row()])
    # A target with NO negative keywords — the scorer would normally
    # leave ``excluded=False`` for any input.
    target = _target(core={"React": 3})

    await score_and_upsert(
        supabase,
        job_posting_id="job-1",
        title="Pharmacy Technician",
        description_html="<p>Filling prescriptions.</p>",
        target=target,
        excluded_by_prefilter=True,
    )

    payload = supabase.table.return_value.upsert.call_args.args[0]
    assert payload["excluded"] is True


@pytest.mark.asyncio
async def test_score_and_upsert_excluded_by_prefilter_false_preserves_scorer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``excluded_by_prefilter=False`` is the default and must not change
    the scorer's verdict — negative keyword matches still exclude the row.
    """
    _seam_sync_fallback(monkeypatch)
    supabase = _make_supabase_mock(upsert_data=[_upserted_score_row()])
    # ``junior`` is in the negative list (see ``_target`` fixture).
    target = _target(core={"React": 3})

    await score_and_upsert(
        supabase,
        job_posting_id="job-1",
        title="Junior React Developer",
        description_html="<p>Junior role on the React team.</p>",
        target=target,
        excluded_by_prefilter=False,
    )

    payload = supabase.table.return_value.upsert.call_args.args[0]
    assert payload["excluded"] is True  # scorer excluded via negative keyword


# ---------------------------------------------------------------------------
# bulk_score_for_target — streaming re-score (shared ``_rescore_page_rows``
# scoring math) on the pooled async service client (feedback-learner + operator
# /rescore paths).
# ---------------------------------------------------------------------------


class _AsyncBulkResp:
    def __init__(self, data: Any) -> None:
        self.data = data
        self.count = len(data) if isinstance(data, list) else (1 if data else 0)


class _AsyncBulkQuery:
    def __init__(self, fake: _AsyncBulkSupabase, table: str) -> None:
        self._fake = fake
        self._table = table
        self._op = "select"

    def select(self, *_a: Any, **_k: Any) -> _AsyncBulkQuery:
        self._op = "select"
        return self

    def eq(self, *_a: Any, **_k: Any) -> _AsyncBulkQuery:
        return self

    def lt(self, *_a: Any, **_k: Any) -> _AsyncBulkQuery:
        return self

    def limit(self, *_a: Any, **_k: Any) -> _AsyncBulkQuery:
        return self

    def range(self, *_a: Any, **_k: Any) -> _AsyncBulkQuery:
        return self

    def in_(self, *_a: Any, **_k: Any) -> _AsyncBulkQuery:
        return self

    def upsert(self, payload: Any, **_k: Any) -> _AsyncBulkQuery:
        self._op = "upsert"
        self._fake.upserts.append(payload)
        return self

    async def execute(self) -> _AsyncBulkResp:
        return _AsyncBulkResp(self._fake.next(self._table, self._op))


class _AsyncBulkSupabase:
    """Scripts ``(table, op) -> response`` for the async bulk re-scorer and
    captures each ``scores`` upsert payload. ``execute`` is awaitable, so this
    stands in for the pooled ``AsyncClient``."""

    def __init__(self) -> None:
        self.script: dict[tuple[str, str], list[Any]] = {}
        self.upserts: list[list[dict[str, Any]]] = []

    def push(self, table: str, op: str, data: Any) -> None:
        self.script.setdefault((table, op), []).append(data)

    def next(self, table: str, op: str) -> Any:
        queued = self.script.get((table, op))
        return queued.pop(0) if queued else []

    def table(self, name: str) -> _AsyncBulkQuery:
        return _AsyncBulkQuery(self, name)


@pytest.mark.asyncio
async def test_bulk_score_for_target_scores_and_preserves_promising() -> None:
    """Re-scores a stale page and upserts, preserving the Phase 1 ``promising``
    floor: promising=False keeps ``excluded`` True, promising=True lets the
    keyword scorer decide."""
    sb = _AsyncBulkSupabase()
    sb.push("targets", "select", [{"app_active": True}])  # pipeline-active
    sb.push(
        "scores",
        "select",
        [
            {"job_posting_id": "good", "promising": True},
            {"job_posting_id": "bad", "promising": False},
        ],
    )
    sb.push("scores", "select", [])  # second page drained → terminate
    sb.push(
        "jobs",
        "select",
        [
            {"id": "good", "title": "Senior Frontend Engineer", "description_html": "<p>React</p>"},
            {"id": "bad", "title": "Senior Frontend Engineer", "description_html": "<p>React</p>"},
        ],
    )

    target = _target(core={"React": 3, "TypeScript": 3})
    count = await bulk_score_for_target(sb, target)  # type: ignore[arg-type]

    assert count == 2
    assert len(sb.upserts) == 1
    rows = {r["job_posting_id"]: r for r in sb.upserts[0]}
    assert rows["bad"]["excluded"] is True
    assert rows["bad"]["promising"] is False
    assert rows["good"]["promising"] is True
    assert rows["good"]["scoring_status"] == "stage2"


@pytest.mark.asyncio
async def test_bulk_score_for_target_streams_multiple_pages() -> None:
    """Re-reads the first page each iteration (an upsert bumps the version out of
    the stale predicate) until a page comes back empty — one upsert per page."""
    sb = _AsyncBulkSupabase()
    sb.push("targets", "select", [{"app_active": True}])
    sb.push("scores", "select", [{"job_posting_id": "j1", "promising": True}])
    sb.push(
        "jobs", "select", [{"id": "j1", "title": "Senior FE", "description_html": "<p>React</p>"}]
    )
    sb.push("scores", "select", [{"job_posting_id": "j2", "promising": True}])
    sb.push(
        "jobs",
        "select",
        [{"id": "j2", "title": "Staff FE", "description_html": "<p>TypeScript</p>"}],
    )
    sb.push("scores", "select", [])  # empty page → terminate

    target = _target(core={"React": 3, "TypeScript": 3})
    count = await bulk_score_for_target(sb, target)  # type: ignore[arg-type]

    assert count == 2
    assert len(sb.upserts) == 2


@pytest.mark.asyncio
async def test_bulk_score_for_target_skips_inactive_target() -> None:
    """Non-pipeline-active target (not ``app_active`` AND no active membership)
    short-circuits before any ``scores`` read — no re-score writes."""
    sb = _AsyncBulkSupabase()
    sb.push("targets", "select", [{"app_active": False}])  # instance floor off
    sb.push("user_targets", "select", [])  # count exact → 0 active memberships

    target = _target(core={"React": 3}, app_active=False)
    count = await bulk_score_for_target(sb, target)  # type: ignore[arg-type]

    assert count == 0
    assert sb.upserts == []


# ---------------------------------------------------------------------------
# get_target_scores
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_target_scores_returns_dict_keyed_by_job_id() -> None:
    rows = [
        _upserted_score_row(job_posting_id="job-1"),
        _upserted_score_row(job_posting_id="job-2"),
    ]
    supabase = _make_supabase_mock(select_data=rows)
    # get_target_scores is async now (#57 slice 3); the id-list branch awaits
    # the get_target_scores_by_ids RPC.
    supabase.rpc.return_value.execute = AsyncMock(return_value=MagicMock(data=rows))

    scores = await get_target_scores(supabase, "target-1", ["job-1", "job-2"])

    assert "job-1" in scores
    assert "job-2" in scores
    assert scores["job-1"].score == 70


@pytest.mark.asyncio
async def test_get_target_scores_returns_empty_dict_when_no_scores() -> None:
    supabase = _make_supabase_mock(select_data=[])
    supabase.rpc.return_value.execute = AsyncMock(return_value=MagicMock(data=[]))

    scores = await get_target_scores(supabase, "target-1", ["job-1"])

    assert scores == {}


@pytest.mark.asyncio
async def test_get_target_scores_empty_id_list_skips_query() -> None:
    """An empty ``job_posting_ids`` must NOT relax to an unbounded SELECT —
    ``.in_("…", [])`` returns all target scores in PostgREST. The guard
    short-circuits to an empty dict with zero queries."""
    supabase = MagicMock()

    scores = await get_target_scores(supabase, "target-1", [])

    assert scores == {}
    supabase.table.assert_not_called()


@pytest.mark.asyncio
async def test_get_target_scores_uses_rpc_body() -> None:
    """A large id-list score lookup is ONE ``get_target_scores_by_ids`` RPC
    carrying the target id + every job id in the jsonb body (no URL
    ``.in_()`` chunking), folded into the same {job_id: score} dict (#93)."""
    job_ids = [f"job-{i}" for i in range(450)]
    rpc_rows = [_upserted_score_row(job_posting_id=jid) for jid in job_ids]

    supabase = MagicMock()
    supabase.rpc.return_value.execute = AsyncMock(return_value=MagicMock(data=rpc_rows))

    scores = await get_target_scores(supabase, "target-1", job_ids)

    supabase.rpc.assert_called_once_with(
        "get_target_scores_by_ids",
        {"p_target_id": "target-1", "p_ids": job_ids},
    )
    # No table-level read at all — the lookup is the RPC.
    supabase.table.assert_not_called()
    # Every id keyed once, identical to the old single-query result.
    assert len(scores) == 450
    assert set(scores.keys()) == set(job_ids)


# ---------------------------------------------------------------------------
# Router: list endpoint with target_id overlay
# ---------------------------------------------------------------------------


def test_list_jobs_without_target_returns_global_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Global view queries jobs directly with global scores."""
    from fastapi.testclient import TestClient

    from app.dependencies import (
        get_async_service_supabase,
        get_async_supabase_for_caller,
        get_current_user_id_optional,
        verify_api_key_or_jwt,
    )
    from app.main import app

    def _fluent_mock(data: list[dict]) -> MagicMock:
        m = MagicMock()
        # /jobs is async now (#57 slice 3) — the handler awaits .execute().
        m.execute = AsyncMock(return_value=MagicMock(data=data, count=len(data)))
        for method in ("select", "eq", "neq", "gte", "in_", "is_", "ilike", "order", "range"):
            getattr(m, method).return_value = m
        m.not_ = m  # `.not_.is_(...)` negation is an attribute access (#60 non-US gate)
        return m

    jp_mock = _fluent_mock(
        [
            {
                "id": "job-1",
                "score": 50,
                "score_breakdown": None,
                "title": "Engineer",
                "company_name": "Acme",
                "created_at": "2026-01-01T00:00:00Z",
            }
        ]
    )

    supabase = MagicMock()
    supabase.table.return_value = jp_mock

    app.dependency_overrides[get_async_service_supabase] = lambda: supabase
    # api-key caller (user_id None): dual-auth resolves the caller client to
    # the (async) service-role client, so mirror the seeded fake.
    app.dependency_overrides[get_async_supabase_for_caller] = lambda: supabase
    app.dependency_overrides[verify_api_key_or_jwt] = lambda: "test"
    app.dependency_overrides[get_current_user_id_optional] = lambda: None

    try:
        tc = TestClient(app)
        resp = tc.get("/jobs")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["postings"]) == 1
        # Global view preserves the global score
        assert data["postings"][0]["score"] == 50
    finally:
        app.dependency_overrides.clear()


def test_list_jobs_with_target_overlays_target_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    from app.dependencies import (
        get_async_service_supabase,
        get_async_supabase_for_caller,
        get_current_user_id_optional,
        verify_api_key_or_jwt,
    )
    from app.main import app

    def _fluent_mock(data: list[dict]) -> MagicMock:
        """Mock that chains any query method and awaits data on .execute()."""
        m = MagicMock()
        # /jobs is async now (#57 slice 3) — the handler awaits .execute().
        m.execute = AsyncMock(return_value=MagicMock(data=data, count=len(data)))
        for method in ("select", "eq", "neq", "gte", "in_", "is_", "ilike", "order", "range"):
            getattr(m, method).return_value = m
        m.not_ = m  # `.not_.is_(...)` negation is an attribute access (#60 non-US gate)
        return m

    ts_mock = _fluent_mock(
        [
            {
                "job_posting_id": "job-1",
                "score": 85,
                "score_breakdown": {
                    "role_titles": 0,
                    "technologies": 12.0,
                    "domain_skills": 0,
                    "seniority_signals": 0,
                    "negative": 0,
                },
            }
        ]
    )

    jp_mock = _fluent_mock(
        [
            {
                "id": "job-1",
                "external_id": "ext-1",
                "source_id": "src-1",
                "title": "Frontend Engineer",
                "company_name": "Acme",
                "location": "Remote",
                "absolute_url": "https://example.com/job-1",
                "score": 50,
                "score_breakdown": None,
                "status": "new",
                "cataloged_at": None,
                "created_at": "2026-04-26T00:00:00Z",
            }
        ]
    )

    supabase = MagicMock()
    supabase.table.side_effect = lambda name: ts_mock if name == "scores" else jp_mock
    # The per-target score sort tries the cross-target RPC first; a non-list
    # payload makes it fall back to the two-query path (ts_mock + jp_mock),
    # exactly as before — the await just needs an awaitable .execute().
    supabase.rpc.return_value.execute = AsyncMock(return_value=MagicMock(data=None))

    app.dependency_overrides[get_async_service_supabase] = lambda: supabase
    # api-key caller (user_id None): dual-auth resolves the caller client to
    # the (async) service-role client, so mirror the seeded fake.
    app.dependency_overrides[get_async_supabase_for_caller] = lambda: supabase
    app.dependency_overrides[verify_api_key_or_jwt] = lambda: "test"
    app.dependency_overrides[get_current_user_id_optional] = lambda: None

    try:
        tc = TestClient(app)
        resp = tc.get("/jobs?target_id=target-1")
        assert resp.status_code == 200
        data = resp.json()
        # Score should be overlaid with target score (85), not global score (50)
        assert data["postings"][0]["score"] == 85
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Router: re-score endpoint
# ---------------------------------------------------------------------------


def test_rescore_endpoint_returns_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    from app.dependencies import (
        get_async_service_supabase,
        verify_api_key,
        verify_api_key_or_jwt,
    )
    from app.main import app
    from app.routers import jobs as jobs_router

    target = _target()
    # #57 PR-G2e-4: /rescore runs on the async service client via the router-inline
    # ``_get_target_async`` + the ``bulk_score_for_target`` twin.
    monkeypatch.setattr(jobs_router, "_get_target_async", AsyncMock(return_value=target))
    monkeypatch.setattr(jobs_router, "bulk_score_for_target", AsyncMock(return_value=42))
    app.dependency_overrides[verify_api_key_or_jwt] = lambda: "test"
    # /rescore now requires the operator-only ``verify_api_key`` dep —
    # not callable from the FE, so the route's auth model is api-key.
    app.dependency_overrides[verify_api_key] = lambda: "test"
    app.dependency_overrides[get_async_service_supabase] = lambda: MagicMock()

    try:
        tc = TestClient(app)
        resp = tc.post("/jobs/rescore/target-1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["target_id"] == "target-1"
        assert data["jobs_scored"] == 42
    finally:
        app.dependency_overrides.clear()


def test_rescore_endpoint_missing_target_returns_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    from app.dependencies import (
        get_async_service_supabase,
        verify_api_key,
        verify_api_key_or_jwt,
    )
    from app.main import app
    from app.routers import jobs as jobs_router

    monkeypatch.setattr(jobs_router, "_get_target_async", AsyncMock(return_value=None))
    app.dependency_overrides[verify_api_key_or_jwt] = lambda: "test"
    app.dependency_overrides[verify_api_key] = lambda: "test"
    app.dependency_overrides[get_async_service_supabase] = lambda: MagicMock()

    try:
        tc = TestClient(app)
        resp = tc.post("/jobs/rescore/nonexistent")
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Global score aggregation — graded (LLM) rows win over keyword placeholders
# (#194: stop averaging keyword-heuristic placeholders into LLM fit scores)
# ---------------------------------------------------------------------------


def _score_row(
    score: int,
    *,
    scoring_status: str = "complete",
    excluded: bool = False,
    job_posting_id: str = "job-1",
) -> dict[str, Any]:
    return {
        "job_posting_id": job_posting_id,
        "score": score,
        "scoring_status": scoring_status,
        "excluded": excluded,
    }


# ---------------------------------------------------------------------------
# Async DB seam (#57)
#
# ``score_and_upsert`` / ``score_title_and_upsert`` route their DB hop through
# ``app.services.db_write.poll_db_write`` — async-on-loop with a
# sync-in-thread fallback when the async client is absent. These tests mirror
# tests/test_db_write.py's recorder pattern — a chainable query-builder
# stand-in that records ops and answers ``execute()`` from a queue, in a sync
# and an async flavour — so each backend selection is pinned.
# ---------------------------------------------------------------------------


class _SeamRecorder:
    """Chainable supabase-client stand-in: records the query chain in ``ops``
    and answers ``execute()`` either from ``rows_by_job`` (filtered on the
    ids of the preceding ``.in_()`` — deterministic regardless of chunk
    order) or by popping the next queued response (empty when exhausted).
    """

    def __init__(
        self,
        responses: list[list[dict[str, Any]]] | None = None,
        rows_by_job: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.ops: list[tuple[Any, ...]] = []
        self.executed = 0
        self.responses = list(responses or [])
        self.rows_by_job = rows_by_job
        self._pending_in_ids: list[str] | None = None

    # -- chainable query-builder methods (each records and returns self) --
    def table(self, name: str) -> _SeamRecorder:
        self.ops.append(("table", name))
        return self

    def select(self, *cols: str) -> _SeamRecorder:
        self.ops.append(("select", *cols))
        return self

    def eq(self, col: str, val: Any) -> _SeamRecorder:
        self.ops.append(("eq", col, val))
        return self

    def lt(self, col: str, val: Any) -> _SeamRecorder:
        self.ops.append(("lt", col, val))
        return self

    def in_(self, col: str, vals: list[str]) -> _SeamRecorder:
        self.ops.append(("in_", col, list(vals)))
        self._pending_in_ids = list(vals)
        return self

    def upsert(self, row: Any, **kwargs: Any) -> _SeamRecorder:
        self.ops.append(("upsert", row, kwargs))
        return self

    def update(self, payload: dict[str, Any]) -> _SeamRecorder:
        self.ops.append(("update", payload))
        return self

    def rpc(self, name: str, params: dict[str, Any]) -> _SeamRecorder:
        self.ops.append(("rpc", name, params))
        return self

    def order(self, *args: Any, **kwargs: Any) -> _SeamRecorder:
        self.ops.append(("order", args, kwargs))
        return self

    def limit(self, n: int) -> _SeamRecorder:
        self.ops.append(("limit", n))
        return self

    def range(self, start: int, end: int) -> _SeamRecorder:
        self.ops.append(("range", start, end))
        return self

    # -- execution --
    def _result(self) -> Any:
        self.executed += 1
        pending, self._pending_in_ids = self._pending_in_ids, None
        if pending is not None and self.rows_by_job is not None:
            data = [row for jid in pending for row in self.rows_by_job.get(jid, [])]
            return MagicMock(data=data)
        return MagicMock(data=self.responses.pop(0) if self.responses else [])

    # -- introspection helpers --
    def op(self, kind: str) -> tuple[Any, ...]:
        matches = [o for o in self.ops if o[0] == kind]
        assert len(matches) == 1, f"expected exactly one {kind!r} op, got {matches!r}"
        return matches[0]

    def upsert_payload(self) -> dict[str, Any]:
        payload = self.op("upsert")[1]
        assert isinstance(payload, dict)
        return dict(payload)


class _SyncSeamClient(_SeamRecorder):
    def execute(self) -> Any:
        return self._result()


class _AsyncSeamClient(_SeamRecorder):
    async def execute(self) -> Any:
        return self._result()


def _seam_sync_fallback(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Force the sync seam fallback by making the async client absent (#57
    slice 4 removed the POLLER_ASYNC_DB flag; the seam is unconditionally async
    now, falling back to sync only when ``get_async_supabase`` returns None).
    Returns the async-client lookup log: the seam DOES consult it (recording the
    lookup) but it returns None, so the sync path takes the call."""
    from app.services import db_write

    lookups: list[int] = []
    monkeypatch.setattr(db_write, "get_async_supabase", lambda: lookups.append(1))
    return lookups


def _seam_flag_on(monkeypatch: pytest.MonkeyPatch, async_client: _AsyncSeamClient) -> None:
    """Route the seam's async path onto ``async_client``."""
    from app.services import db_write

    monkeypatch.setattr(db_write, "get_async_supabase", lambda: async_client)


# ---- score_title_and_upsert -------------------------------------------


@pytest.mark.asyncio
async def test_score_title_and_upsert_flag_off_uses_sync_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.target_scoring import score_title_and_upsert

    lookups = _seam_sync_fallback(monkeypatch)
    sync_client = _SyncSeamClient(responses=[[_upserted_score_row()]])

    result = await score_title_and_upsert(
        sync_client,  # type: ignore[arg-type]
        job_posting_id="job-1",
        title="Senior React Engineer",
        target=_target(core={"React": 3}),
    )

    assert result is not None
    assert result.job_posting_id == "job-1"
    assert sync_client.executed == 1  # sync client took the write
    assert lookups == [1]  # async consulted, returned None → sync fallback
    assert ("table", "scores") in sync_client.ops
    assert sync_client.op("upsert")[2] == {"on_conflict": "job_posting_id,target_id"}


@pytest.mark.asyncio
async def test_score_title_and_upsert_flag_on_uses_async_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.target_scoring import score_title_and_upsert

    async_client = _AsyncSeamClient(responses=[[_upserted_score_row(score=41)]])
    _seam_flag_on(monkeypatch, async_client)
    sync_client = _SyncSeamClient()

    result = await score_title_and_upsert(
        sync_client,  # type: ignore[arg-type]
        job_posting_id="job-1",
        title="Senior React Engineer",
        target=_target(core={"React": 3}),
    )

    # Response parsed from the async client's row...
    assert result is not None
    assert result.job_posting_id == "job-1"
    assert result.target_id == "target-1"
    assert result.score == 41
    # ...which took the write; the sync client was untouched.
    assert async_client.executed == 1
    assert sync_client.executed == 0
    payload = async_client.upsert_payload()
    assert payload["scoring_status"] == "stage1"


@pytest.mark.asyncio
async def test_score_title_and_upsert_no_match_skips_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same skip contract as the sync original: no keyword match and no
    exclusion -> ``None`` and zero DB traffic on either backend."""
    from app.services.target_scoring import score_title_and_upsert

    lookups = _seam_sync_fallback(monkeypatch)
    sync_client = _SyncSeamClient()

    result = await score_title_and_upsert(
        sync_client,  # type: ignore[arg-type]
        job_posting_id="job-1",
        title="Pharmacy Technician",
        target=_target(core={"React": 3}),
    )

    assert result is None
    assert sync_client.executed == 0
    assert sync_client.ops == []
    assert lookups == []  # no match → write skipped before the seam is reached


# ---- score_and_upsert --------------------------------------------------


@pytest.mark.asyncio
async def test_score_and_upsert_flag_off_uses_sync_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.target_scoring import score_and_upsert

    lookups = _seam_sync_fallback(monkeypatch)
    sync_client = _SyncSeamClient(responses=[[_upserted_score_row()]])

    result = await score_and_upsert(
        sync_client,  # type: ignore[arg-type]
        job_posting_id="job-1",
        title="Senior Frontend Engineer",
        description_html="<p>React and TypeScript required.</p>",
        target=_target(core={"React": 3, "TypeScript": 3}),
    )

    assert result.job_posting_id == "job-1"
    assert sync_client.executed == 1
    assert lookups == [1]
    assert ("table", "scores") in sync_client.ops
    assert sync_client.upsert_payload()["scoring_status"] == "stage2"


@pytest.mark.asyncio
async def test_score_and_upsert_flag_on_uses_async_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.target_scoring import score_and_upsert

    async_client = _AsyncSeamClient(responses=[[_upserted_score_row()]])
    _seam_flag_on(monkeypatch, async_client)
    sync_client = _SyncSeamClient()

    result = await score_and_upsert(
        sync_client,  # type: ignore[arg-type]
        job_posting_id="job-1",
        title="Senior Frontend Engineer",
        description_html="<p>React and TypeScript required.</p>",
        target=_target(core={"React": 3, "TypeScript": 3}),
        excluded_by_prefilter=True,
        promising=False,
        phase1_confidence=91,
    )

    # Response parsed from the async client's row; sync client untouched.
    assert result.job_posting_id == "job-1"
    assert result.target_id == "target-1"
    assert result.score == 70
    assert async_client.executed == 1
    assert sync_client.executed == 0
    payload = async_client.upsert_payload()
    # The prefilter OR and the Phase 1 columns ride through the poll path.
    assert payload["excluded"] is True
    assert payload["promising"] is False
    assert payload["phase1_confidence"] == 91
    assert async_client.op("upsert")[2] == {"on_conflict": "job_posting_id,target_id"}


# ---- batch_update_global_scores_poll ----------------------------------------

_BATCH_ROWS_BY_JOB: dict[str, list[dict[str, Any]]] = {
    # job-1: graded 90 + keyword 10 -> 90 (graded-first, #194)
    "job-1": [
        _score_row(90, scoring_status="complete", job_posting_id="job-1"),
        _score_row(10, scoring_status="stage2", job_posting_id="job-1"),
    ],
    # job-2: keyword only -> avg(20, 40) = 30
    "job-2": [
        _score_row(20, scoring_status="stage1", job_posting_id="job-2"),
        _score_row(40, scoring_status="stage2", job_posting_id="job-2"),
    ],
}


# ---------------------------------------------------------------------------
# bulk_title_score_for_target (retro-score at activation — audit P3/M7)
# ---------------------------------------------------------------------------


class _RetroSupabase:
    """Fake supabase for ``bulk_title_score_for_target``: keyset-paginates a
    fixed ``jobs`` dataset (``.order('id').limit(n).gt('id', cursor)``) and
    records every ``scores`` bulk upsert, so a test can assert one upsert PER
    PAGE (not per row) and the exact rows written. It deliberately has NO
    ``.range`` — if the code paginated by OFFSET the test would AttributeError,
    which pins the keyset behaviour."""

    def __init__(self, jobs: list[dict[str, Any]]) -> None:
        self._jobs = sorted(jobs, key=lambda j: j["id"])
        self.upsert_calls: list[list[dict[str, Any]]] = []
        self._after: str | None = None
        self._limit: int | None = None

    def table(self, _name: str) -> _RetroSupabase:
        self._after = None
        self._limit = None
        return self

    def select(self, *_a: object, **_k: object) -> _RetroSupabase:
        return self

    def order(self, col: str, **_k: object) -> _RetroSupabase:
        assert col == "id"
        return self

    def limit(self, n: int) -> _RetroSupabase:
        self._limit = n
        return self

    def gt(self, col: str, val: str) -> _RetroSupabase:
        assert col == "id"
        self._after = val
        return self

    def upsert(self, rows: list[dict[str, Any]], **_k: object) -> MagicMock:
        self.upsert_calls.append(rows)
        resp = MagicMock()
        resp.data = rows
        return resp

    def _read_resp(self) -> MagicMock:
        rows = self._jobs
        if self._after is not None:
            rows = [j for j in rows if j["id"] > self._after]
        rows = rows[: (self._limit or len(rows))]
        resp = MagicMock()
        resp.data = rows
        return resp

    def execute(self) -> MagicMock:
        return self._read_resp()


class _AsyncRetroSupabase(_RetroSupabase):
    """Async twin of ``_RetroSupabase`` — the keyset ``jobs`` read and the
    per-page ``scores`` upsert are both awaited on the loop, so ``execute`` is a
    coroutine (the ``bulk_title_score_for_target`` contract, #57 PR-G2e-4)."""

    async def execute(self) -> MagicMock:  # type: ignore[override]
        return self._read_resp()

    def upsert(self, rows: list[dict[str, Any]], **_k: object) -> MagicMock:
        self.upsert_calls.append(rows)
        resp = MagicMock()
        resp.data = rows
        resp.execute = AsyncMock(return_value=MagicMock(data=rows))
        return resp


def _retro_jobs() -> list[dict[str, str]]:
    # 6 jobs; matches (contain "React") = j01, j05, j06. j05 also has the
    # "junior" negative -> written but excluded. j02/j03/j04 don't match and
    # (with page size 2) j03+j04 form a whole page with NO matches.
    return [
        {"id": "j01", "title": "React Engineer"},
        {"id": "j02", "title": "Backend Go Developer"},
        {"id": "j03", "title": "Data Scientist"},
        {"id": "j04", "title": "DevOps Engineer"},
        {"id": "j05", "title": "Junior React Developer"},
        {"id": "j06", "title": "React Native Lead"},
    ]


@pytest.mark.asyncio
async def test_bulk_title_score_async_matches_sync_batching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The activation-pipeline path produces the expected per-page batching + row
    set, building rows via the shared ``_title_score_page_rows`` (#57 PR-G2e-4)."""
    import app.services.target_scoring as ts

    monkeypatch.setattr(ts, "_RETRO_TITLE_BATCH_SIZE", 2)  # force multi-page

    supabase = _AsyncRetroSupabase(_retro_jobs())
    target = _target(core={"React": 3})

    written = await bulk_title_score_for_target(supabase, target)  # type: ignore[arg-type]

    assert written == 3
    assert [len(c) for c in supabase.upsert_calls] == [1, 2]
    upserted = [r for call in supabase.upsert_calls for r in call]
    assert {r["job_posting_id"] for r in upserted} == {"j01", "j05", "j06"}
    assert all(r["scoring_status"] == "stage1" for r in upserted)
    by_id = {r["job_posting_id"]: r for r in upserted}
    assert by_id["j05"]["excluded"] is True
    assert by_id["j01"]["excluded"] is False


@pytest.mark.asyncio
async def test_bulk_title_score_async_empty_catalog_writes_nothing() -> None:
    supabase = _AsyncRetroSupabase([])
    target = _target(core={"React": 3})

    written = await bulk_title_score_for_target(supabase, target)  # type: ignore[arg-type]

    assert written == 0
    assert supabase.upsert_calls == []
