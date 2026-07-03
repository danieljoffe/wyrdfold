"""Tests for target-aware scoring v2 (#502).

Covers: score_and_upsert, bulk_score_for_target, get_target_scores,
poller integration, list endpoint overlay, re-score endpoint.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

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
    get_target_scores,
    score_and_upsert,
)

_BATCH_UPDATE_PATH = "app.services.target_scoring.batch_update_global_scores"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _target(
    *,
    target_id: str = "target-1",
    core: dict[str, int] | None = None,
    is_active: bool = True,
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
        is_active=is_active,
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
            role_titles=0, technologies=12.0, domain_skills=0,
            seniority_signals=0, negative=0,
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
    supabase.table.return_value.upsert.return_value.execute.return_value.data = (
        upsert_data or []
    )
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


def test_score_and_upsert_calls_upsert_with_correct_shape() -> None:
    row = _upserted_score_row()
    supabase = _make_supabase_mock(upsert_data=[row])
    target = _target(core={"React": 3, "TypeScript": 3})

    result = score_and_upsert(
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


def test_score_and_upsert_raises_on_empty_response() -> None:
    supabase = _make_supabase_mock(upsert_data=[])
    target = _target(core={"React": 3})

    with pytest.raises(RuntimeError, match="Failed to upsert"):
        score_and_upsert(
            supabase,
            job_posting_id="job-1",
            title="Engineer",
            description_html="<p>React.</p>",
            target=target,
        )


def test_score_and_upsert_excluded_by_prefilter_forces_true() -> None:
    """When the caller signals a prefilter rejection, the upserted row
    must carry ``excluded=True`` regardless of what the keyword scorer
    decided. This is the contract the poller relies on so that re-scores
    preserve cosine exclusions.
    """
    supabase = _make_supabase_mock(upsert_data=[_upserted_score_row()])
    # A target with NO negative keywords — the scorer would normally
    # leave ``excluded=False`` for any input.
    target = _target(core={"React": 3})

    score_and_upsert(
        supabase,
        job_posting_id="job-1",
        title="Pharmacy Technician",
        description_html="<p>Filling prescriptions.</p>",
        target=target,
        excluded_by_prefilter=True,
    )

    payload = supabase.table.return_value.upsert.call_args.args[0]
    assert payload["excluded"] is True


def test_score_and_upsert_excluded_by_prefilter_false_preserves_scorer() -> None:
    """``excluded_by_prefilter=False`` is the default and must not change
    the scorer's verdict — negative keyword matches still exclude the row.
    """
    supabase = _make_supabase_mock(upsert_data=[_upserted_score_row()])
    # ``junior`` is in the negative list (see ``_target`` fixture).
    target = _target(core={"React": 3})

    score_and_upsert(
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
# bulk_score_for_target
# ---------------------------------------------------------------------------


def test_bulk_score_for_target_scores_stage1_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """bulk_score_for_target only scores jobs with existing target score rows."""
    monkeypatch.setattr(_BATCH_UPDATE_PATH, MagicMock())

    # Stage 1 score rows (existing matches for this target)
    jts_rows = [
        {"job_posting_id": "job-1"},
        {"job_posting_id": "job-2"},
    ]
    jobs = [
        {"id": "job-1", "title": "Senior FE", "description_html": "<p>React</p>"},
        {"id": "job-2", "title": "Staff FE", "description_html": "<p>TypeScript</p>"},
    ]
    upsert_rows = [
        _upserted_score_row(job_posting_id="job-1"),
        _upserted_score_row(job_posting_id="job-2"),
    ]

    supabase = MagicMock()

    range_calls = {"n": 0}

    def range_side_effect(*args: Any, **kwargs: Any) -> MagicMock:
        range_calls["n"] += 1
        mock = MagicMock()
        if range_calls["n"] == 1:
            mock.execute.return_value.data = jts_rows
        else:
            mock.execute.return_value.data = []
        return mock

    # Chain: .select().eq().lt().range()
    supabase.table.return_value.select.return_value.eq.return_value.lt.return_value.range.side_effect = (
        range_side_effect
    )
    supabase.table.return_value.select.return_value.in_.return_value.execute.return_value.data = (
        jobs
    )
    supabase.table.return_value.upsert.return_value.execute.return_value.data = upsert_rows

    target = _target(core={"React": 3, "TypeScript": 3})
    count = bulk_score_for_target(supabase, target)

    assert count == 2


def test_bulk_score_for_target_preserves_phase1_promising_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bulk re-score must preserve the Phase 1 verdict on every
    ``scores`` row. Without this, a target's ``profile_version`` bump
    (feedback learner, manual /rescore, deploy-triggered poll) would
    re-admit jobs Phase 1 previously dropped — exactly the regression
    we hit after the post-PR-#782 Railway deploy, just now with the
    Phase 1 verdict instead of cosine.

    Mechanism: bulk_score_for_target selects ``scores.promising`` inline in
    the stale-row paging query and uses it as the ``excluded_by_prefilter``
    floor on each rescore. promising=False -> excluded stays True even if
    the keyword scorer would admit; promising=True/None -> scorer decides.
    """
    monkeypatch.setattr(_BATCH_UPDATE_PATH, MagicMock())

    # The stale-row page carries the ``promising`` verdict inline (selected
    # in the same paging query), so the Phase 1 floor is preserved without a
    # separate per-batch ``scores`` lookup. Phase 1 previously admitted
    # "good", rejected "bad".
    jts_rows = [
        {"job_posting_id": "good", "promising": True},
        {"job_posting_id": "bad", "promising": False},
    ]
    jobs = [
        {
            "id": "good",
            "title": "Senior Frontend Engineer",
            "description_html": "<p>React.</p>",
        },
        {
            "id": "bad",
            "title": "Sales Development Representative",
            "description_html": "<p>Outbound sales.</p>",
        },
    ]
    upsert_rows = [
        _upserted_score_row(job_posting_id="good"),
        _upserted_score_row(job_posting_id="bad"),
    ]

    supabase = MagicMock()
    range_calls = {"n": 0}

    def range_side_effect(*args: Any, **kwargs: Any) -> MagicMock:
        range_calls["n"] += 1
        mock = MagicMock()
        mock.execute.return_value.data = jts_rows if range_calls["n"] == 1 else []
        return mock

    # Stale-row paging carries ``promising`` inline: ``.select().eq().lt().range()``.
    supabase.table.return_value.select.return_value.eq.return_value.lt.return_value.range.side_effect = (
        range_side_effect
    )
    # The jobs fetch is ``.select().in_()`` — returns the backing job rows.
    supabase.table.return_value.select.return_value.in_.return_value.execute.return_value.data = (
        jobs
    )
    supabase.table.return_value.upsert.return_value.execute.return_value.data = (
        upsert_rows
    )

    target = _target(core={"React": 3})
    count = bulk_score_for_target(supabase, target)

    assert count == 2
    payload = supabase.table.return_value.upsert.call_args.args[0]
    by_id = {row["job_posting_id"]: row for row in payload}
    assert by_id["good"]["excluded"] is False, "Phase 1 promising row kept"
    assert by_id["bad"]["excluded"] is True, "Phase 1 not-promising row excluded"
    # And ``promising`` carries through on the upsert so a future re-read
    # still finds the verdict.
    assert by_id["good"]["promising"] is True
    assert by_id["bad"]["promising"] is False


def test_bulk_score_for_target_streams_multiple_pages_without_holding_all_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit #29: stale rows spanning several pages are all rescored, and the
    function never accumulates every stale id into one in-memory list.

    The streaming contract: each loop iteration re-reads the FIRST page of
    still-stale ``scores`` rows (the prior page's upsert bumps
    ``scored_profile_version`` so those rows drop out of the ``.lt()``
    predicate), fetches just that page's jobs, scores+upserts them, then
    recomputes global scores — before touching the next page. We prove:
      1. every job across all pages is scored exactly once (no skips), and
      2. no single jobs-fetch (``.in_()``) ever receives more than one
         page's worth of ids (peak memory is O(page), not O(catalog)).
    """
    import app.services.target_scoring as ts

    monkeypatch.setattr(_BATCH_UPDATE_PATH, MagicMock())
    # Shrink the page so a small catalog spans multiple pages cheaply.
    page = 2
    monkeypatch.setattr(ts, "_RESCORE_BATCH_SIZE", page)

    # A 5-row catalog → 3 pages at page size 2 (2, 2, 1).
    catalog = [f"job-{i}" for i in range(5)]
    # ``remaining`` models the DB: ids still stale (not yet rescored). Each
    # upsert removes its ids, so the next "first page" is the next slice.
    remaining = list(catalog)
    jobs_in_calls: list[list[str]] = []

    supabase = MagicMock()

    # Stale-row paging: ``.select().eq().lt().range()`` → first ``page`` of
    # whatever is still stale right now.
    def range_side_effect(*_args: Any, **_kwargs: Any) -> MagicMock:
        m = MagicMock()
        m.execute.return_value.data = [
            {"job_posting_id": jid, "promising": None} for jid in remaining[:page]
        ]
        return m

    supabase.table.return_value.select.return_value.eq.return_value.lt.return_value.range.side_effect = (
        range_side_effect
    )

    # Jobs fetch: ``.select().in_(ids)`` → record the width, return job rows
    # for exactly those ids.
    def in_side_effect(_col: str, ids: list[str]) -> MagicMock:
        jobs_in_calls.append(list(ids))
        m = MagicMock()
        m.execute.return_value.data = [
            {"id": jid, "title": "Senior FE", "description_html": "<p>React</p>"}
            for jid in ids
        ]
        return m

    supabase.table.return_value.select.return_value.in_.side_effect = in_side_effect

    # Upsert: mark those ids no-longer-stale (drop from ``remaining``) and echo
    # them back as the upsert result.
    def upsert_side_effect(rows: list[dict[str, Any]], **_kwargs: Any) -> MagicMock:
        for r in rows:
            if r["job_posting_id"] in remaining:
                remaining.remove(r["job_posting_id"])
        m = MagicMock()
        m.execute.return_value.data = rows
        return m

    supabase.table.return_value.upsert.side_effect = upsert_side_effect

    target = _target(core={"React": 3})
    count = bulk_score_for_target(supabase, target)

    # 1. Every job scored exactly once.
    assert count == len(catalog)
    scored_ids = [jid for call in jobs_in_calls for jid in call]
    assert sorted(scored_ids) == sorted(catalog), "every stale job rescored once"
    # 2. Multiple pages were processed (not one giant batch)...
    assert len(jobs_in_calls) == 3, jobs_in_calls
    # ...and no single jobs fetch held more than one page of ids.
    assert all(len(call) <= page for call in jobs_in_calls), jobs_in_calls


def test_bulk_score_for_target_terminates_when_page_has_no_backing_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative control for the streaming loop: if a page of stale ``scores``
    rows has no backing ``jobs`` rows (all deleted), the first-page re-read
    must NOT spin forever. The loop breaks instead of looping on rows it can
    never upsert away.
    """
    import app.services.target_scoring as ts

    monkeypatch.setattr(_BATCH_UPDATE_PATH, MagicMock())
    monkeypatch.setattr(ts, "_RESCORE_BATCH_SIZE", 2)

    range_calls = {"n": 0}

    def range_side_effect(*_args: Any, **_kwargs: Any) -> MagicMock:
        # Always returns stale rows — if the loop didn't break on the empty
        # jobs fetch, it would call this unboundedly.
        range_calls["n"] += 1
        assert range_calls["n"] < 50, "first-page re-read looped without progress"
        m = MagicMock()
        m.execute.return_value.data = [
            {"job_posting_id": "ghost-1", "promising": None},
            {"job_posting_id": "ghost-2", "promising": None},
        ]
        return m

    supabase = MagicMock()
    supabase.table.return_value.select.return_value.eq.return_value.lt.return_value.range.side_effect = (
        range_side_effect
    )
    # No backing jobs for these ids.
    supabase.table.return_value.select.return_value.in_.return_value.execute.return_value.data = []

    target = _target(core={"React": 3})
    count = bulk_score_for_target(supabase, target)

    assert count == 0
    # Upsert never fired — nothing was scorable.
    supabase.table.return_value.upsert.assert_not_called()


def test_bulk_score_for_target_handles_no_stale_jobs() -> None:
    """Returns 0 when no jobs have stale scores for this target."""
    supabase = MagicMock()
    # No stale score rows (chain: .select().eq().lt().range())
    supabase.table.return_value.select.return_value.eq.return_value.lt.return_value.range.return_value.execute.return_value.data = []

    target = _target(core={"React": 3})
    count = bulk_score_for_target(supabase, target)

    assert count == 0


def test_bulk_score_for_target_skips_inactive_target() -> None:
    """Inactive targets short-circuit before any DB read.

    ``targets.is_active=False`` means no user currently has this target
    enabled (the trigger off ``user_targets`` ORs across users). Re-scoring
    would just burn LLM/CPU on rows nobody will see in the list view.
    """
    supabase = MagicMock()
    target = _target(core={"React": 3}, is_active=False)

    count = bulk_score_for_target(supabase, target)

    assert count == 0
    # And critically: zero DB traffic — no select, no upsert.
    supabase.table.assert_not_called()


# ---------------------------------------------------------------------------
# get_target_scores
# ---------------------------------------------------------------------------


def test_get_target_scores_returns_dict_keyed_by_job_id() -> None:
    rows = [
        _upserted_score_row(job_posting_id="job-1"),
        _upserted_score_row(job_posting_id="job-2"),
    ]
    supabase = _make_supabase_mock(select_data=rows)

    scores = get_target_scores(supabase, "target-1", ["job-1", "job-2"])

    assert "job-1" in scores
    assert "job-2" in scores
    assert scores["job-1"].score == 70


def test_get_target_scores_returns_empty_dict_when_no_scores() -> None:
    supabase = _make_supabase_mock(select_data=[])

    scores = get_target_scores(supabase, "target-1", ["job-1"])

    assert scores == {}


def test_get_target_scores_empty_id_list_skips_query() -> None:
    """An empty ``job_posting_ids`` must NOT relax to an unbounded SELECT —
    ``.in_("…", [])`` returns all target scores in PostgREST. The guard
    short-circuits to an empty dict with zero queries."""
    supabase = MagicMock()

    scores = get_target_scores(supabase, "target-1", [])

    assert scores == {}
    supabase.table.assert_not_called()


def test_get_target_scores_uses_rpc_body() -> None:
    """A large id-list score lookup is ONE ``get_target_scores_by_ids`` RPC
    carrying the target id + every job id in the jsonb body (no URL
    ``.in_()`` chunking), folded into the same {job_id: score} dict (#93)."""
    job_ids = [f"job-{i}" for i in range(450)]
    rpc_rows = [_upserted_score_row(job_posting_id=jid) for jid in job_ids]

    supabase = MagicMock()
    supabase.rpc.return_value.execute.return_value.data = rpc_rows

    scores = get_target_scores(supabase, "target-1", job_ids)

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
        get_current_user_id_optional,
        get_supabase,
        get_supabase_for_caller,
        verify_api_key_or_jwt,
    )
    from app.main import app

    def _fluent_mock(data: list[dict]) -> MagicMock:
        m = MagicMock()
        m.execute.return_value = MagicMock(data=data, count=len(data))
        for method in ("select", "eq", "neq", "gte", "in_", "is_", "ilike", "order", "range"):
            getattr(m, method).return_value = m
        return m

    jp_mock = _fluent_mock([
        {
            "id": "job-1",
            "score": 50,
            "score_breakdown": None,
            "title": "Engineer",
            "company_name": "Acme",
            "created_at": "2026-01-01T00:00:00Z",
        }
    ])

    supabase = MagicMock()
    supabase.table.return_value = jp_mock

    app.dependency_overrides[get_supabase] = lambda: supabase
    # api-key caller (user_id None): dual-auth resolves the caller client to
    # the service-role client, so mirror the seeded fake.
    app.dependency_overrides[get_supabase_for_caller] = lambda: supabase
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
        get_current_user_id_optional,
        get_supabase,
        get_supabase_for_caller,
        verify_api_key_or_jwt,
    )
    from app.main import app

    def _fluent_mock(data: list[dict]) -> MagicMock:
        """Mock that chains any query method and returns data on .execute()."""
        m = MagicMock()
        m.execute.return_value = MagicMock(data=data, count=len(data))
        for method in ("select", "eq", "neq", "gte", "in_", "is_", "ilike", "order", "range"):
            getattr(m, method).return_value = m
        return m

    ts_mock = _fluent_mock([{
        "job_posting_id": "job-1",
        "score": 85,
        "score_breakdown": {
            "role_titles": 0, "technologies": 12.0, "domain_skills": 0,
            "seniority_signals": 0, "negative": 0,
        },
    }])

    jp_mock = _fluent_mock([{
        "id": "job-1",
        "external_id": "ext-1",
        "source_id": "src-1",
        "title": "Frontend Engineer",
        "company_name": "Acme",
        "location": "Remote",
        "department": None,
        "absolute_url": "https://example.com/job-1",
        "score": 50,
        "score_breakdown": None,
        "status": "new",
        "first_seen_at": None,
        "created_at": "2026-04-26T00:00:00Z",
    }])

    supabase = MagicMock()
    supabase.table.side_effect = (
        lambda name: ts_mock if name == "scores" else jp_mock
    )

    app.dependency_overrides[get_supabase] = lambda: supabase
    # api-key caller (user_id None): dual-auth resolves the caller client to
    # the service-role client, so mirror the seeded fake.
    app.dependency_overrides[get_supabase_for_caller] = lambda: supabase
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

    from app.dependencies import get_supabase, verify_api_key, verify_api_key_or_jwt
    from app.main import app
    from app.routers import jobs as jobs_router

    target = _target()
    monkeypatch.setattr(jobs_router, "get_target", lambda *_a, **_kw: target)
    monkeypatch.setattr(jobs_router, "bulk_score_for_target", lambda *_a, **_kw: 42)

    supabase = MagicMock()
    app.dependency_overrides[get_supabase] = lambda: supabase
    app.dependency_overrides[verify_api_key_or_jwt] = lambda: "test"
    # /rescore now requires the operator-only ``verify_api_key`` dep —
    # not callable from the FE, so the route's auth model is api-key.
    app.dependency_overrides[verify_api_key] = lambda: "test"

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

    from app.dependencies import get_supabase, verify_api_key, verify_api_key_or_jwt
    from app.main import app
    from app.routers import jobs as jobs_router

    monkeypatch.setattr(jobs_router, "get_target", lambda *_a, **_kw: None)

    supabase = MagicMock()
    app.dependency_overrides[get_supabase] = lambda: supabase
    app.dependency_overrides[verify_api_key_or_jwt] = lambda: "test"
    app.dependency_overrides[verify_api_key] = lambda: "test"

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


def test_aggregate_ignores_keyword_placeholder_when_a_grade_exists() -> None:
    """The #194 regression: a keyword placeholder must not drag a real grade.

    Old behaviour averaged the two mixed scales — avg(80, 10) = 45. The graded
    row must now stand alone at 80.
    """
    from app.services.target_scoring import _aggregate_global_score

    rows = [
        _score_row(80, scoring_status="complete"),
        _score_row(10, scoring_status="stage2"),  # keyword placeholder
    ]
    assert _aggregate_global_score(rows) == 80


def test_aggregate_keyword_placeholder_cannot_inflate_a_grade() -> None:
    """The inverse: a high keyword placeholder can't lift a low real grade."""
    from app.services.target_scoring import _aggregate_global_score

    rows = [
        _score_row(20, scoring_status="complete"),
        _score_row(95, scoring_status="stage1"),  # keyword placeholder
    ]
    assert _aggregate_global_score(rows) == 20  # not avg(20, 95) == 58


def test_aggregate_averages_only_graded_rows_when_several_exist() -> None:
    from app.services.target_scoring import _aggregate_global_score

    rows = [
        _score_row(80, scoring_status="complete"),
        _score_row(60, scoring_status="complete"),
        _score_row(30, scoring_status="stage2"),  # ignored
    ]
    assert _aggregate_global_score(rows) == 70  # avg(80, 60), placeholder dropped


def test_aggregate_falls_back_to_keyword_rows_when_nothing_graded() -> None:
    """A not-yet-graded job still gets its keyword placeholder aggregate."""
    from app.services.target_scoring import _aggregate_global_score

    rows = [
        _score_row(10, scoring_status="stage1"),
        _score_row(20, scoring_status="stage2"),
    ]
    assert _aggregate_global_score(rows) == 15


def test_aggregate_missing_status_is_treated_as_keyword() -> None:
    """A row without scoring_status is a placeholder (mirrors _is_pending)."""
    from app.services.target_scoring import _aggregate_global_score

    graded_plus_unknown = [
        _score_row(90, scoring_status="complete"),
        {"job_posting_id": "job-1", "score": 25, "excluded": False},  # no status
    ]
    assert _aggregate_global_score(graded_plus_unknown) == 90

    only_unknown = [{"job_posting_id": "job-1", "score": 25, "excluded": False}]
    assert _aggregate_global_score(only_unknown) == 25


def test_aggregate_drops_excluded_rows_before_the_graded_split() -> None:
    from app.services.target_scoring import _aggregate_global_score

    # The only graded row is excluded → fall back to the live keyword row.
    rows = [
        _score_row(80, scoring_status="complete", excluded=True),
        _score_row(12, scoring_status="stage2", excluded=False),
    ]
    assert _aggregate_global_score(rows) == 12

    # An excluded keyword row can't drag a live grade either.
    rows2 = [
        _score_row(70, scoring_status="complete", excluded=False),
        _score_row(5, scoring_status="stage1", excluded=True),
    ]
    assert _aggregate_global_score(rows2) == 70


def test_aggregate_empty_or_all_excluded_is_zero() -> None:
    from app.services.target_scoring import _aggregate_global_score

    assert _aggregate_global_score([]) == 0
    assert _aggregate_global_score([_score_row(90, excluded=True)]) == 0


def test_update_global_score_writes_graded_first_value() -> None:
    """End-to-end through the DB path: the write uses the graded-first rule."""
    from app.services.target_scoring import update_global_score

    supabase = MagicMock()
    supabase.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [
        _score_row(85, scoring_status="complete"),
        _score_row(15, scoring_status="stage2"),
    ]
    update_global_score(supabase, "job-1")

    update_call = supabase.table.return_value.update
    update_call.assert_called_once_with({"score": 85})


def test_batch_update_global_scores_aggregates_each_job_graded_first() -> None:
    """The batch path groups by job and applies the same graded-first rule."""
    from app.services.target_scoring import batch_update_global_scores

    supabase = MagicMock()
    supabase.table.return_value.select.return_value.in_.return_value.execute.return_value.data = [
        # job-1: graded 90 + keyword 10 -> 90
        _score_row(90, scoring_status="complete", job_posting_id="job-1"),
        _score_row(10, scoring_status="stage2", job_posting_id="job-1"),
        # job-2: keyword only -> avg(20, 40) = 30
        _score_row(20, scoring_status="stage1", job_posting_id="job-2"),
        _score_row(40, scoring_status="stage2", job_posting_id="job-2"),
    ]
    batch_update_global_scores(supabase, ["job-1", "job-2"])

    supabase.rpc.assert_called_once()
    name, payload = supabase.rpc.call_args.args
    assert name == "bulk_update_scores"
    by_id = {u["id"]: u["score"] for u in payload["p_updates"]}
    assert by_id == {"job-1": 90, "job-2": 30}
