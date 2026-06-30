"""Job analysis end-to-end tests with mocked Supabase + MockLLMClient.

Covers: cache hit, cache miss (LLM call + persist + cost log),
missing optimized doc (404), missing job posting (404), and
LLM JSON parse error.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.models.analysis import (
    JobAnalysis,
    JobAnalysisRecord,
    Scorecard,
    SkillMatch,
)
from app.models.experience import (
    OptimizedDoc,
    OptimizedPayload,
    Outcome,
    Role,
    Skill,
)
from app.services.analysis import persistence as persistence_mod
from app.services.analysis.analyze import (
    DEFAULT_PURPOSE,
    _optimized_section,
    analyze_job,
    build_user_message,
)
from app.services.llm import cost_log as cost_log_mod
from app.services.llm.mock import MockLLMClient

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _optimized_payload() -> OptimizedPayload:
    return OptimizedPayload(
        summary="Senior FE engineer.",
        roles=[
            Role(
                id="fc",
                company="FightCamp",
                title="Senior Frontend Engineer",
                start="2021-11",
                end="2024-04",
                summary="Led the PDP rebuild.",
                skills=["React", "TypeScript"],
                outcome_refs=[],
            )
        ],
        skills=[
            Skill(name="React"),
            Skill(name="TypeScript"),
        ],
        outcomes=[
            Outcome(
                description="Cut mobile load times from 10s to 2s",
                metric="LCP",
                value="2s",
                role_ref="fc",
            )
        ],
    )


def _job_target() -> Any:
    from app.models.targets import JobTarget, ScoringProfile

    return JobTarget(
        id="tgt-1",
        label="Senior Frontend Engineer",
        description="Lead FE engineer at consumer-facing companies",
        scoring_profile=ScoringProfile(),
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _optimized_doc() -> OptimizedDoc:
    return OptimizedDoc(
        id="opt-1",
        user_id=None,
        prose_doc_id=None,
        version=1,
        payload=_optimized_payload(),
        markdown_view=None,
        source="llm",
        created_at=datetime.now(UTC),
    )


def _valid_analysis() -> JobAnalysis:
    return JobAnalysis(
        scorecard=Scorecard(
            skills_matched=[
                SkillMatch(
                    name="React",
                    matched=True,
                    confidence="high",
                    evidence="Listed in skills and used at FightCamp",
                ),
                SkillMatch(
                    name="TypeScript",
                    matched=True,
                    confidence="high",
                    evidence="Listed in skills",
                ),
            ],
            skills_missing=["GraphQL"],
            nice_to_haves=["AWS"],
            seniority_fit="strong",
            seniority_rationale="3+ years senior experience matches the role.",
            domain_fit="moderate",
            domain_rationale="E-commerce adjacent but not exact match.",
        ),
        recommendation="Apply: strong technical match with direct React/TypeScript experience.",
    )


def _valid_analysis_json() -> str:
    return _valid_analysis().model_dump_json()


def _analysis_record_row(record_id: str = "rec-analysis-1") -> dict[str, Any]:
    """Shape returned by supabase.table().insert(...).execute().data."""
    return {
        "id": record_id,
        "job_posting_id": "job-1",
        "target_id": "tgt-1",
        "user_id": None,
        "optimized_doc_id": "opt-1",
        "scorecard": _valid_analysis().scorecard.model_dump(mode="json"),
        "recommendation": _valid_analysis().recommendation,
        "model": "claude-sonnet-4-6",
        "cost_usd": 0.001,
        "latency_ms": 50,
        "created_at": datetime.now(UTC).isoformat(),
    }


def _make_supabase_mock(
    *,
    insert_data: list[dict[str, Any]] | None = None,
    select_data: list[dict[str, Any]] | None = None,
) -> MagicMock:
    supabase = MagicMock()
    # upsert chain (persist now upserts on the cache-key conflict target)
    supabase.table.return_value.upsert.return_value.execute.return_value.data = insert_data or []
    # select chain (get_cached uses .eq * 3 → .order → .limit → .is_/.eq → .execute)
    cached_chain = supabase.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.order.return_value.limit.return_value
    cached_chain.is_.return_value.execute.return_value.data = select_data or []
    cached_chain.eq.return_value.execute.return_value.data = select_data or []
    return supabase


# ---------------------------------------------------------------------------
# analyze_job (service layer)
# ---------------------------------------------------------------------------


async def test_analyze_job_returns_analysis_and_result() -> None:
    llm = MockLLMClient(scripted={DEFAULT_PURPOSE: _valid_analysis_json()})

    analysis, result = await analyze_job(
        llm,
        optimized=_optimized_payload(),
        job_description="We want a senior React engineer with TypeScript.",
    )

    assert analysis.recommendation.startswith("Apply")
    assert len(analysis.scorecard.skills_matched) == 2
    assert analysis.scorecard.seniority_fit == "strong"
    assert result.model == "claude-sonnet-4-6"
    assert result.cost_usd > 0


async def test_analyze_job_includes_target_context_in_message() -> None:
    seen: dict[str, str] = {}

    def responder(latest_user: str, _messages: object) -> str:
        seen["latest"] = latest_user
        return _valid_analysis_json()

    llm = MockLLMClient(scripted={DEFAULT_PURPOSE: responder})
    await analyze_job(
        llm,
        optimized=_optimized_payload(),
        job_description="JD text",
        target_context="Target: Senior FE at Acme",
    )
    assert "[TargetContext]" in seen["latest"]
    assert "Senior FE at Acme" in seen["latest"]


async def test_analyze_job_sets_cache_breakpoint_on_master_doc() -> None:
    """The user turn carries a cache breakpoint at the end of the master doc
    (#73), byte-identical to the OptimizedPayload section, so the heavy prefix
    is cached across analyses while the job description is re-billed."""
    optimized = _optimized_payload()
    llm = MockLLMClient(scripted={DEFAULT_PURPOSE: _valid_analysis_json()})
    await analyze_job(llm, optimized=optimized, job_description="JD text")

    user_msg = llm.calls[-1]["messages"][0]  # type: ignore[index]
    section = _optimized_section(optimized)
    assert user_msg.cache_prefix_chars == len(section)
    assert user_msg.content[: user_msg.cache_prefix_chars] == section


async def test_analyze_job_json_parse_error_raises() -> None:
    llm = MockLLMClient(scripted={DEFAULT_PURPOSE: "not valid json"})
    with pytest.raises(Exception):
        await analyze_job(
            llm,
            optimized=_optimized_payload(),
            job_description="JD text",
        )


# ---------------------------------------------------------------------------
# build_user_message
# ---------------------------------------------------------------------------


def test_build_user_message_contains_payload_and_jd() -> None:
    msg = build_user_message(
        optimized=_optimized_payload(),
        job_description="We need a React developer.",
    )
    assert "[OptimizedPayload]" in msg
    assert "[JobDescription]" in msg
    assert "React developer" in msg
    assert "FightCamp" in msg


def test_build_user_message_omits_target_when_none() -> None:
    msg = build_user_message(
        optimized=_optimized_payload(),
        job_description="JD",
        target_context=None,
    )
    assert "[TargetContext]" not in msg


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------


def test_get_cached_returns_none_on_empty() -> None:
    supabase = _make_supabase_mock(select_data=[])
    result = persistence_mod.get_cached(
        supabase, "job-1", target_id="tgt-1", optimized_doc_id="opt-1", user_id=None
    )
    assert result is None


def test_get_cached_returns_record_when_exists() -> None:
    row = _analysis_record_row()
    supabase = _make_supabase_mock(select_data=[row])
    result = persistence_mod.get_cached(
        supabase, "job-1", target_id="tgt-1", optimized_doc_id="opt-1", user_id=None
    )
    assert result is not None
    assert result.id == "rec-analysis-1"
    assert result.recommendation.startswith("Apply")


def test_get_cached_scopes_query_to_user_tenant() -> None:
    """The cache read must filter by ``user_id`` so one user's analysis is
    never returned to another (and so the poller — which now stamps the
    doc's owning user — shares the same cache entry the user view reads).

    For a real user the query ends in ``.eq("user_id", <uuid>)``; for the
    api-key/legacy ``None`` caller it ends in ``.is_("user_id", "null")``.
    """
    supabase = _make_supabase_mock(select_data=[])
    persistence_mod.get_cached(
        supabase, "job-1", target_id="tgt-1", optimized_doc_id="opt-1", user_id="user-A"
    )
    # The terminal filter for a real user is the user_id .eq on the
    # ordered+limited query (the 4th .eq in the chain).
    final_eq = supabase.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.order.return_value.limit.return_value.eq
    final_eq.assert_called_once_with("user_id", "user-A")

    # And the None caller uses the IS NULL branch, never the user .eq.
    supabase_null = _make_supabase_mock(select_data=[])
    persistence_mod.get_cached(
        supabase_null,
        "job-1",
        target_id="tgt-1",
        optimized_doc_id="opt-1",
        user_id=None,
    )
    null_chain = supabase_null.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.order.return_value.limit.return_value
    null_chain.is_.assert_called_once_with("user_id", "null")
    null_chain.eq.assert_not_called()


def test_persist_upserts_and_returns_record() -> None:
    from app.models.llm import LLMResult, LLMUsage

    supabase = _make_supabase_mock(insert_data=[_analysis_record_row()])
    llm_result = LLMResult(
        content="{}",
        model="claude-sonnet-4-6",
        usage=LLMUsage(input_tokens=100, output_tokens=50),
        cost_usd=0.001,
        latency_ms=50,
    )
    record = persistence_mod.persist(
        supabase,
        job_posting_id="job-1",
        target_id="tgt-1",
        user_id=None,
        optimized_doc_id="opt-1",
        analysis=_valid_analysis(),
        llm_result=llm_result,
    )
    assert record.id == "rec-analysis-1"
    # Idempotent upsert (not a blind insert) so concurrent/duplicate
    # computations collapse onto one row via the cache-key conflict target.
    supabase.table.return_value.upsert.assert_called_once()
    _, kwargs = supabase.table.return_value.upsert.call_args
    assert kwargs["on_conflict"] == persistence_mod._CACHE_KEY_COLS


def test_persist_raises_on_empty_upsert() -> None:
    from app.models.llm import LLMResult, LLMUsage

    supabase = _make_supabase_mock(insert_data=[])
    llm_result = LLMResult(
        content="{}",
        model="claude-sonnet-4-6",
        usage=LLMUsage(input_tokens=100, output_tokens=50),
        cost_usd=0.001,
        latency_ms=50,
    )
    with pytest.raises(RuntimeError, match="Failed to upsert"):
        persistence_mod.persist(
            supabase,
            job_posting_id="job-1",
            target_id="tgt-1",
            user_id=None,
            optimized_doc_id="opt-1",
            analysis=_valid_analysis(),
            llm_result=llm_result,
        )


# ---------------------------------------------------------------------------
# Poller ↔ user-view shared cache (the phase-3 re-fire root cause)
# ---------------------------------------------------------------------------


def _owned_optimized_doc(user_id: str) -> OptimizedDoc:
    return OptimizedDoc(
        id="opt-owned-1",
        user_id=user_id,
        prose_doc_id=None,
        version=3,
        payload=_optimized_payload(),
        markdown_view=None,
        source="llm",
        created_at=datetime.now(UTC),
    )


async def test_poller_stage3_persists_under_doc_owner_then_reuses_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for the phase-3 re-fire bug.

    The cron poller computed analyses against each user's OWN optimized
    doc but persisted them under ``user_id=None`` — a separate tenant
    namespace the user-facing view (which reads under the JWT ``sub``)
    could never hit, so every first visit re-ran a full LLM analysis the
    cron had already paid for.

    This proves two things end to end through ``_run_llm_scoring_for_row``:
      1. The first pass persists the analysis under the doc's OWNER
         (``optimized_doc.user_id``), not None.
      2. A second identical pass finds that row via the cache and does
         NOT call the LLM again — exactly the inputs the user view sees.
    """
    from app.services import poller as poller_mod
    from app.services.targets import crud as targets_crud_mod  # noqa: F401

    owner = "user-owner-42"
    doc = _owned_optimized_doc(owner)

    # An in-memory stand-in for the analyses cache. Keyed by the full cache
    # key INCLUDING user_id so a tenant mismatch (the old bug) would miss.
    store: dict[tuple[str, str, str, str | None], JobAnalysisRecord] = {}

    def fake_get_cached(
        _sb: Any,
        job_posting_id: str,
        *,
        target_id: str,
        optimized_doc_id: str,
        user_id: str | None,
    ) -> JobAnalysisRecord | None:
        return store.get((job_posting_id, target_id, optimized_doc_id, user_id))

    persisted_user_ids: list[str | None] = []

    def fake_persist(
        _sb: Any,
        *,
        job_posting_id: str,
        target_id: str,
        user_id: str | None,
        optimized_doc_id: str | None,
        analysis: JobAnalysis,
        llm_result: Any,
    ) -> JobAnalysisRecord:
        persisted_user_ids.append(user_id)
        rec = JobAnalysisRecord.model_validate(
            {
                **_analysis_record_row(record_id=f"rec-{len(store)}"),
                "user_id": user_id,
                "optimized_doc_id": optimized_doc_id,
            }
        )
        store[(job_posting_id, target_id, cast_str(optimized_doc_id), user_id)] = rec
        return rec

    monkeypatch.setattr(poller_mod, "get_cached_analysis", fake_get_cached)
    monkeypatch.setattr(poller_mod, "persist_analysis", fake_persist)
    # Don't touch the real DB for the score-blend writes / mark-complete.
    monkeypatch.setattr(poller_mod, "mark_target_scores_complete", lambda *_a, **_k: None)
    monkeypatch.setattr(poller_mod, "enqueue_llm_cost", lambda *_a, **_k: None)

    llm = MockLLMClient(scripted={"poll_scoring": _valid_analysis_json()})
    supabase = MagicMock()
    target = _job_target()
    row_data = {"id": "job-1", "description_html": "We want a React engineer."}

    # First pass: cache miss → one LLM call → persist under the owner.
    await poller_mod._run_llm_scoring_for_row(
        supabase,
        row_data,
        doc,
        llm,
        target,
        current_score=80,
        payer_user_id=owner,
    )
    assert len(llm.calls) == 1
    assert persisted_user_ids == [owner], (
        "poller must persist under the doc's owning user, not None"
    )

    # Second pass with identical inputs: must hit the cache, no 2nd LLM call.
    await poller_mod._run_llm_scoring_for_row(
        supabase,
        row_data,
        doc,
        llm,
        target,
        current_score=80,
        payer_user_id=owner,
    )
    assert len(llm.calls) == 1, "second identical pass must reuse the cache"
    assert persisted_user_ids == [owner], "no second persist on a cache hit"


def cast_str(v: str | None) -> str:
    assert v is not None
    return v


# ---------------------------------------------------------------------------
# Router integration (via TestClient)
# ---------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    """FastAPI TestClient with mocked dependencies."""
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


async def test_router_cache_hit_skips_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a cached analysis exists, the LLM should not be called."""

    cached_record = JobAnalysisRecord.model_validate(_analysis_record_row())
    monkeypatch.setattr(persistence_mod, "get_cached", lambda *_a, **_kw: cached_record)

    from app.services.experience import optimized as opt_mod

    monkeypatch.setattr(opt_mod, "get_latest", lambda *_a, **_kw: _optimized_doc())

    llm = MockLLMClient()
    supabase = MagicMock()

    from fastapi.testclient import TestClient

    from app.main import app

    app.dependency_overrides[get_supabase] = lambda: supabase
    app.dependency_overrides[get_llm_client] = lambda: llm
    app.dependency_overrides[verify_api_key_or_jwt] = lambda: "test"
    app.dependency_overrides[get_current_user_id_optional] = lambda: None
    app.dependency_overrides[get_supabase_for_caller] = lambda: MagicMock()

    try:
        tc = TestClient(app)
        resp = tc.post("/analysis/job-1?target_id=tgt-1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "rec-analysis-1"
        assert len(llm.calls) == 0  # LLM was NOT called
    finally:
        app.dependency_overrides.clear()


async def test_router_cache_miss_runs_llm_and_persists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cache miss → LLM call → cost log → persist → return record."""

    monkeypatch.setattr(persistence_mod, "get_cached", lambda *_a, **_kw: None)
    monkeypatch.setattr(cost_log_mod, "record", MagicMock())

    inserted_row = _analysis_record_row()
    monkeypatch.setattr(
        persistence_mod,
        "persist",
        lambda *_a, **kw: JobAnalysisRecord.model_validate(inserted_row),
    )

    # Mock optimized.get_latest
    from app.services.experience import optimized as opt_mod

    monkeypatch.setattr(opt_mod, "get_latest", lambda *_a, **_kw: _optimized_doc())

    # Mock targets_crud.get (used to build target_context)
    from app.services.targets import crud as targets_crud_mod

    monkeypatch.setattr(targets_crud_mod, "get", lambda *_a, **_kw: _job_target())

    llm = MockLLMClient(scripted={DEFAULT_PURPOSE: _valid_analysis_json()})

    # Mock job posting existence check (now includes description_html)
    supabase = MagicMock()
    supabase.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
        {"id": "job-1", "description_html": "We want a React engineer."}
    ]

    from fastapi.testclient import TestClient

    from app.main import app

    app.dependency_overrides[get_supabase] = lambda: supabase
    app.dependency_overrides[get_llm_client] = lambda: llm
    app.dependency_overrides[verify_api_key_or_jwt] = lambda: "test"
    app.dependency_overrides[get_current_user_id_optional] = lambda: None
    app.dependency_overrides[get_supabase_for_caller] = lambda: MagicMock()

    try:
        tc = TestClient(app)
        resp = tc.post("/analysis/job-1?target_id=tgt-1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "rec-analysis-1"
        assert len(llm.calls) == 1
        cost_log_mod.record.assert_called_once()
    finally:
        app.dependency_overrides.clear()


async def test_router_missing_optimized_doc_returns_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(persistence_mod, "get_cached", lambda *_a, **_kw: None)

    from app.services.experience import optimized as opt_mod

    monkeypatch.setattr(opt_mod, "get_latest", lambda *_a, **_kw: None)

    from fastapi.testclient import TestClient

    from app.main import app

    app.dependency_overrides[get_supabase] = lambda: MagicMock()
    app.dependency_overrides[get_llm_client] = lambda: MockLLMClient()
    app.dependency_overrides[verify_api_key_or_jwt] = lambda: "test"
    app.dependency_overrides[get_current_user_id_optional] = lambda: None
    app.dependency_overrides[get_supabase_for_caller] = lambda: MagicMock()

    try:
        tc = TestClient(app)
        resp = tc.post("/analysis/job-1?target_id=tgt-1")
        assert resp.status_code == 404
        # Structured detail so the client can render a "set up your profile"
        # empty state — and it must NOT leak the internal endpoint path (#105).
        detail = resp.json()["detail"]
        assert detail["code"] == "no_profile"
        assert "profile" in detail["message"].lower()
        assert "/experience/derive" not in str(detail)
        assert "POST" not in detail["message"]
    finally:
        app.dependency_overrides.clear()


async def test_router_empty_description_returns_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job with no description_html should 422, not silently call the LLM."""
    monkeypatch.setattr(persistence_mod, "get_cached", lambda *_a, **_kw: None)

    from app.services.experience import optimized as opt_mod

    monkeypatch.setattr(opt_mod, "get_latest", lambda *_a, **_kw: _optimized_doc())

    from app.services.targets import crud as targets_crud_mod

    monkeypatch.setattr(targets_crud_mod, "get", lambda *_a, **_kw: _job_target())

    supabase = MagicMock()
    supabase.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
        {"id": "job-1", "description_html": ""}
    ]

    from fastapi.testclient import TestClient

    from app.main import app

    app.dependency_overrides[get_supabase] = lambda: supabase
    app.dependency_overrides[get_llm_client] = lambda: MockLLMClient()
    app.dependency_overrides[verify_api_key_or_jwt] = lambda: "test"
    app.dependency_overrides[get_current_user_id_optional] = lambda: None
    app.dependency_overrides[get_supabase_for_caller] = lambda: MagicMock()

    try:
        tc = TestClient(app)
        resp = tc.post("/analysis/job-1?target_id=tgt-1")
        assert resp.status_code == 422
        assert "description" in resp.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


async def test_router_missing_job_posting_returns_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(persistence_mod, "get_cached", lambda *_a, **_kw: None)

    from app.services.experience import optimized as opt_mod

    monkeypatch.setattr(opt_mod, "get_latest", lambda *_a, **_kw: _optimized_doc())

    from app.services.targets import crud as targets_crud_mod

    monkeypatch.setattr(targets_crud_mod, "get", lambda *_a, **_kw: _job_target())

    supabase = MagicMock()
    supabase.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = []

    from fastapi.testclient import TestClient

    from app.main import app

    app.dependency_overrides[get_supabase] = lambda: supabase
    app.dependency_overrides[get_llm_client] = lambda: MockLLMClient()
    app.dependency_overrides[verify_api_key_or_jwt] = lambda: "test"
    app.dependency_overrides[get_current_user_id_optional] = lambda: None
    app.dependency_overrides[get_supabase_for_caller] = lambda: MagicMock()

    try:
        tc = TestClient(app)
        resp = tc.post("/analysis/job-1?target_id=tgt-1")
        assert resp.status_code == 404
        assert "job posting" in resp.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


async def test_router_missing_target_returns_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown target_id returns 404, not a silent LLM call."""
    monkeypatch.setattr(persistence_mod, "get_cached", lambda *_a, **_kw: None)

    from app.services.experience import optimized as opt_mod

    monkeypatch.setattr(opt_mod, "get_latest", lambda *_a, **_kw: _optimized_doc())

    from app.services.targets import crud as targets_crud_mod

    monkeypatch.setattr(targets_crud_mod, "get", lambda *_a, **_kw: None)

    from fastapi.testclient import TestClient

    from app.main import app

    app.dependency_overrides[get_supabase] = lambda: MagicMock()
    app.dependency_overrides[get_llm_client] = lambda: MockLLMClient()
    app.dependency_overrides[verify_api_key_or_jwt] = lambda: "test"
    app.dependency_overrides[get_current_user_id_optional] = lambda: None
    app.dependency_overrides[get_supabase_for_caller] = lambda: MagicMock()

    try:
        tc = TestClient(app)
        resp = tc.post("/analysis/job-1?target_id=missing")
        assert resp.status_code == 404
        assert "target" in resp.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


async def test_router_blend_writes_score_via_caller_rpc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#6 R2: the scores blend is written via the user_apply_score_blend
    SECURITY DEFINER RPC on the caller's client — not a direct service-role
    scores update."""
    cached_record = JobAnalysisRecord.model_validate(_analysis_record_row())
    monkeypatch.setattr(persistence_mod, "get_cached", lambda *_a, **_kw: cached_record)

    from app.services.experience import optimized as opt_mod

    monkeypatch.setattr(opt_mod, "get_latest", lambda *_a, **_kw: _optimized_doc())

    supabase = MagicMock(name="service_role")
    # The keyword-score read in _apply_llm_blend (.select.eq.eq.limit.execute).
    supabase.table.return_value.select.return_value.eq.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
        {"score": 60}
    ]
    caller = MagicMock(name="caller")

    from fastapi.testclient import TestClient

    from app.main import app

    app.dependency_overrides[get_supabase] = lambda: supabase
    app.dependency_overrides[get_supabase_for_caller] = lambda: caller
    app.dependency_overrides[get_llm_client] = lambda: MockLLMClient()
    app.dependency_overrides[verify_api_key_or_jwt] = lambda: "test"
    app.dependency_overrides[get_current_user_id_optional] = lambda: None

    try:
        resp = TestClient(app).post("/analysis/job-1?target_id=tgt-1")
        assert resp.status_code == 200
        # Gated write goes through the RPC on the caller client...
        caller.rpc.assert_called_once()
        assert caller.rpc.call_args[0][0] == "user_apply_score_blend"
        rpc_args = caller.rpc.call_args[0][1]
        assert rpc_args["p_job_posting_id"] == "job-1"
        assert rpc_args["p_target_id"] == "tgt-1"
        # ...not via a direct service-role scores UPDATE.
        supabase.table.return_value.update.assert_not_called()
    finally:
        app.dependency_overrides.clear()


# Need these imports for dependency overrides
from app.dependencies import (
    get_current_user_id_optional,
    get_llm_client,
    get_supabase,
    get_supabase_for_caller,
    verify_api_key_or_jwt,
)
