"""Non-blocking tailoring: 202 kick-off, poll surface, ATS re-check (#656).

POST /tailor/resume (~39s) and POST /tailor/cover-letter (~27s) no longer hold
the request open for the LLM pipeline — they hand it to a detached task and
return 202, and the client polls the by-job route until the ``documents`` row
lands. These tests drive the captured task deterministically rather than
relying on real background scheduling (the same technique test_analysis.py
uses for #459).

The load-bearing negatives are as important as the happy path here, because
each one is a way the 202 could quietly swallow a failure the user needs to
see: the gap gate and contact resolution must still 4xx on the POST itself,
a failed claim must be released so a retry re-kicks, and a run that dies must
surface through the poll instead of leaving the client spinning forever.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, settings
from app.dependencies import (
    get_async_service_supabase,
    get_async_user_supabase,
    get_current_user_id,
    get_current_user_id_optional,
    get_llm_client,
    get_settings,
    verify_api_key_or_jwt,
)
from app.main import app
from app.models.ats_lint import LintResult, LintViolation
from app.models.experience import OptimizedDoc, OptimizedPayload, Outcome, Role, Skill
from app.models.llm import LLMResult, LLMUsage
from app.models.tailor import (
    ContactInfo,
    TailoredCoverLetter,
    TailoredResume,
    TailoredResumeRecord,
)
from app.routers import tailor as tailor_router
from app.services.tailor import (
    CoverLetterPipelineLintFailure,
    CoverLetterPipelineSuccess,
    PipelineLintFailure,
    PipelineSuccess,
    run_registry,
)

_NOW = datetime.now(UTC)
_USER = "user-1"
_JOB = "job-1"
_CONTACT = ContactInfo(name="Daniel Joffe", email="d@example.com")

_RESUME = TailoredResume(summary="Senior FE.", contact=_CONTACT, experience=[], skills=["React"])
_LETTER = TailoredCoverLetter(
    contact=_CONTACT,
    recipient_company="Acme",
    salutation="Hi",
    paragraphs=[],
    closing="Best",
    signature="Daniel",
)
_LLM_RESULT = LLMResult(
    content="{}",
    model="claude-sonnet-4-6",
    usage=LLMUsage(input_tokens=100, output_tokens=50),
    cost_usd=0.001,
    latency_ms=50,
)


def _optimized_doc() -> OptimizedDoc:
    """A master doc that clears the structural gap gate."""
    return OptimizedDoc(
        id="opt-1",
        user_id=_USER,
        prose_doc_id=None,
        version=1,
        payload=OptimizedPayload(
            summary="Senior FE.",
            roles=[
                Role(
                    id="fc",
                    company="FightCamp",
                    title="Senior Frontend Engineer",
                    start="2021-11",
                    end="2024-04",
                    summary="Led the PDP rebuild.",
                    skills=["React"],
                    outcome_refs=["o1"],
                )
            ],
            skills=[Skill(name="React"), Skill(name="TypeScript")],
            outcomes=[
                Outcome(description="Cut load times", metric="LCP", value="2s", role_ref="fc")
            ],
        ),
        markdown_view=None,
        source="llm",
        created_at=_NOW,
    )


# Markdown that actually passes ``lint_markdown`` — the linter requires the
# canonical H2 sections, so a hand-waved stub would fail for the wrong reason.
_CLEAN_MD = "# Daniel Joffe\n\n## Experience\n\n### Engineer — Acme\n\n- Did things\n"


def _record(
    *,
    record_id: str = "rec-1",
    document_type: str = "resume",
    lint_violations: list[LintViolation] | None = None,
    payload_md: str | None = _CLEAN_MD,
    approved_at: datetime | None = None,
) -> TailoredResumeRecord:
    return TailoredResumeRecord(
        id=record_id,
        user_id=_USER,
        job_posting_id=_JOB,
        document_type=document_type,
        resume_type="generic",
        jd_snapshot="JD",
        jd_snapshot_hash="abc",
        payload=(_RESUME if document_type == "resume" else _LETTER).model_dump(mode="json"),
        payload_md=payload_md,
        storage_path=None,
        warnings=[],
        model="claude-sonnet-4-6",
        input_tokens=100,
        output_tokens=50,
        cost_usd=0.001,
        latency_ms=50,
        created_at=_NOW,
        approved_at=approved_at,
        lint_violations=lint_violations,
    )


def _success(record: TailoredResumeRecord | None = None) -> PipelineSuccess:
    return PipelineSuccess(
        record=record or _record(),
        resume=_RESUME,
        warnings=[],
        lint=LintResult(ok=True, violations=[]),
        llm_result=_LLM_RESULT,
    )


@contextlib.contextmanager
def _client(
    *,
    supabase: Any = None,
    max_concurrent: int = 3,
) -> Iterator[TestClient]:
    """TestClient with the tailor router's deps overridden.

    ``get_current_user_id_optional`` returns None so ``enforce_llm_budget``
    short-circuits (it bypasses api-key callers) — the budget gate has its own
    tests and isn't what these exercise.
    """
    mock = supabase if supabase is not None else MagicMock()
    app.dependency_overrides[verify_api_key_or_jwt] = lambda: "test"
    app.dependency_overrides[get_async_service_supabase] = lambda: mock
    app.dependency_overrides[get_async_user_supabase] = lambda: mock
    app.dependency_overrides[get_current_user_id] = lambda: _USER
    app.dependency_overrides[get_current_user_id_optional] = lambda: None
    app.dependency_overrides[get_llm_client] = lambda: MagicMock()
    app.dependency_overrides[get_settings] = lambda: settings.model_copy(
        update={"tailor_max_concurrent_runs": max_concurrent}
    )
    try:
        yield TestClient(app, headers={"host": "localhost"})
    finally:
        app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _posting_exists_by_default(request: pytest.FixtureRequest) -> Iterator[None]:
    """Every kick now verifies the posting exists before spawning (a 202 must
    mean work that can actually run). Default it to True module-wide so each
    test states only what it's actually about; the negatives override it
    explicitly with their own ``patch``.

    ``@pytest.mark.real_posting_exists`` opts out — the test of the helper
    itself must reach the real function, not this stub.
    """
    if request.node.get_closest_marker("real_posting_exists"):
        yield
        return
    with patch("app.routers.tailor._posting_exists", new_callable=AsyncMock, return_value=True):
        yield


def _capture_spawned(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Patch the router's ``spawn_detached`` to CAPTURE (not run) the task
    coroutine, so tests await it deterministically. Close each coro in a
    finally to avoid 'never awaited' warnings."""
    captured: list[Any] = []
    monkeypatch.setattr(
        tailor_router,
        "spawn_detached",
        lambda coro, *, name: captured.append(coro) or MagicMock(),
    )
    return captured


@contextlib.contextmanager
def _pipeline(result: Any, *, cover_letter: bool = False) -> Iterator[AsyncMock]:
    """Stub the whole pre-pipeline preamble + the pipeline itself."""
    target = "run_cover_letter_pipeline" if cover_letter else "run_tailor_pipeline"
    run = AsyncMock(return_value=result)
    with (
        patch(
            "app.routers.tailor._optimized_latest",
            new_callable=AsyncMock,
            return_value=_optimized_doc(),
        ),
        patch("app.routers.tailor._preferences_get", new_callable=AsyncMock, return_value=None),
        patch("app.routers.tailor.resolve_contact", new_callable=AsyncMock, return_value=_CONTACT),
        patch(f"app.routers.tailor.{target}", run),
        patch("app.services.tailor.persistence.mark_job_resume_draft", new_callable=AsyncMock),
    ):
        yield run


def _resume_body(**overrides: Any) -> dict[str, Any]:
    return {
        "job_description": "Build things.",
        "job_posting_id": _JOB,
        "force_fresh": True,  # skip the reuse short-circuit deterministically
        **overrides,
    }


# ---- Kick-off: 202 + detached run -----------------------------------------


def test_resume_post_returns_202_without_running_the_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The contract: the POST comes back immediately and the pipeline has NOT
    run inside the request. A blocking implementation would return 200 with a
    record here."""
    captured = _capture_spawned(monkeypatch)
    try:
        with _pipeline(_success()) as run, _client() as tc:
            resp = tc.post("/tailor/resume", json=_resume_body())

        assert resp.status_code == 202
        assert resp.json() == {"status": "running", "message": None}
        run.assert_not_called()
        assert len(captured) == 1
    finally:
        for coro in captured:
            coro.close()


async def test_detached_resume_task_runs_pipeline_and_clears_the_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_spawned(monkeypatch)
    key = run_registry.key_for(user_id=_USER, document_type="resume", job_posting_id=_JOB)
    try:
        with _pipeline(_success()) as run, _client() as tc:
            tc.post("/tailor/resume", json=_resume_body())
            assert run_registry.is_running(key) is True
            await captured[0]

        run.assert_awaited_once()
        # Cleared → the poll now reads the persisted record, not "running".
        assert run_registry.get(key) is None
    finally:
        for coro in captured:
            coro.close()


async def test_detached_resume_task_marks_error_when_the_pipeline_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead run must surface through the poll. Without this the client polls
    a key that will never produce a row until it gives up — the exact failure
    the 202 pattern is supposed to make visible."""
    captured = _capture_spawned(monkeypatch)
    key = run_registry.key_for(user_id=_USER, document_type="resume", job_posting_id=_JOB)
    try:
        with _pipeline(_success()) as run, _client() as tc:
            run.side_effect = RuntimeError("LLM exploded")
            tc.post("/tailor/resume", json=_resume_body())
            # Must NOT raise — spawn_detached's done-callback only logs.
            await captured[0]

        st = run_registry.get(key)
        assert st is not None and st.status == "error"
        assert "retry" in (st.error or "").lower()
        # An error is not "running", so a retry POST can re-claim the key.
        assert run_registry.is_running(key) is False
    finally:
        for coro in captured:
            coro.close()


def test_second_kick_dedups_to_202_without_a_second_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two kicks for the same (user, type, posting) → ONE detached task. This
    is what keeps a double-click or a StrictMode double-invoke from paying for
    the same document twice."""
    captured = _capture_spawned(monkeypatch)
    try:
        with _pipeline(_success()), _client() as tc:
            r1 = tc.post("/tailor/resume", json=_resume_body())
            r2 = tc.post("/tailor/resume", json=_resume_body())

        assert r1.status_code == 202
        assert r2.status_code == 202
        assert r2.json()["status"] == "running"
        assert len(captured) == 1
    finally:
        for coro in captured:
            coro.close()


def test_a_different_user_is_not_deduped_away(monkeypatch: pytest.MonkeyPatch) -> None:
    """The key is scoped by user: one user's in-flight run must never dedup
    away another user's kick for the same (globally shared) posting."""
    captured = _capture_spawned(monkeypatch)
    try:
        with _pipeline(_success()):
            with _client() as tc:
                assert tc.post("/tailor/resume", json=_resume_body()).status_code == 202
            app.dependency_overrides.clear()
            with _client() as tc:
                app.dependency_overrides[get_current_user_id] = lambda: "user-2"
                assert tc.post("/tailor/resume", json=_resume_body()).status_code == 202

        assert len(captured) == 2
    finally:
        for coro in captured:
            coro.close()


def test_concurrency_cap_429s_a_fan_out_across_postings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard this PR owes: backgrounding removed the serialization a 39s
    blocking request imposed on a browser tab, and enforce_llm_budget meters
    SPEND whose llm_costs rows don't exist until each run's LLM returns — so
    without a concurrency cap, N simultaneous kicks across N postings all read
    the same pre-burst spend and all pass."""
    captured = _capture_spawned(monkeypatch)
    try:
        with _pipeline(_success()), _client(max_concurrent=2) as tc:
            a = tc.post("/tailor/resume", json=_resume_body(job_posting_id="job-a"))
            b = tc.post("/tailor/resume", json=_resume_body(job_posting_id="job-b"))
            c = tc.post("/tailor/resume", json=_resume_body(job_posting_id="job-c"))

        assert [a.status_code, b.status_code] == [202, 202]
        assert c.status_code == 429
        assert c.json()["detail"]["code"] == "tailor_concurrent_limit"
        # The rejected kick spawned nothing and left no claim behind.
        assert len(captured) == 2
        assert (
            run_registry.is_running(
                run_registry.key_for(user_id=_USER, document_type="resume", job_posting_id="job-c")
            )
            is False
        )
    finally:
        for coro in captured:
            coro.close()


def test_cap_of_zero_disables_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_spawned(monkeypatch)
    try:
        with _pipeline(_success()), _client(max_concurrent=0) as tc:
            codes = [
                tc.post("/tailor/resume", json=_resume_body(job_posting_id=f"j{i}")).status_code
                for i in range(5)
            ]
        assert codes == [202] * 5
    finally:
        for coro in captured:
            coro.close()


# ---- What must still fail FAST, in front of the 202 ------------------------


def test_gap_gate_still_422s_instantly_and_claims_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision 1: the structural gap gate stays synchronous. A master doc too
    thin to generate from is a setup problem with its own frontend CTA —
    deferring it behind a 202 would turn an instant, actionable 422 into a
    30-second wait ending in a generic poll error."""
    captured = _capture_spawned(monkeypatch)
    thin = OptimizedDoc(
        id="opt-thin",
        user_id=_USER,
        prose_doc_id=None,
        version=1,
        payload=OptimizedPayload(summary="Senior engineer."),  # no roles
        markdown_view=None,
        source="llm",
        created_at=_NOW,
    )
    try:
        with (
            patch(
                "app.routers.tailor._optimized_latest",
                new_callable=AsyncMock,
                return_value=thin,
            ),
            _client() as tc,
        ):
            resp = tc.post("/tailor/resume", json=_resume_body())

        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "gap_gate"
        assert captured == []
        assert (
            run_registry.is_running(
                run_registry.key_for(user_id=_USER, document_type="resume", job_posting_id=_JOB)
            )
            is False
        )
    finally:
        for coro in captured:
            coro.close()


def test_missing_contact_name_400s_and_releases_the_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contact resolution runs after the claim but before the 202, so its 400
    reaches the frontend's inline name-prompt-and-retry flow. The claim must be
    released — otherwise that retry would dedup against a dead run and the user
    would be locked out until the TTL swept it."""
    from fastapi import HTTPException

    captured = _capture_spawned(monkeypatch)
    key = run_registry.key_for(user_id=_USER, document_type="resume", job_posting_id=_JOB)
    try:
        with (
            patch(
                "app.routers.tailor._optimized_latest",
                new_callable=AsyncMock,
                return_value=_optimized_doc(),
            ),
            patch("app.routers.tailor._preferences_get", new_callable=AsyncMock, return_value=None),
            patch(
                "app.routers.tailor.resolve_contact",
                new_callable=AsyncMock,
                side_effect=HTTPException(status_code=400, detail="No contact name on file."),
            ),
            _client() as tc,
        ):
            resp = tc.post("/tailor/resume", json=_resume_body())

        assert resp.status_code == 400
        assert captured == []
        assert run_registry.get(key) is None, "a failed claim must not linger"
    finally:
        for coro in captured:
            coro.close()


def test_jd_only_caller_keeps_the_blocking_path() -> None:
    """No job_posting_id → no poll surface (``by-job/{id}`` is keyed on it), so
    the operator/api-key path still blocks and returns the record inline."""
    with _pipeline(_success()) as run, _client() as tc:
        resp = tc.post("/tailor/resume", json={"job_description": "Build things."})

    assert resp.status_code == 200
    assert resp.json()["record"]["id"] == "rec-1"
    run.assert_awaited_once()


# ---- Flagged drafts --------------------------------------------------------


def test_generation_lint_failure_returns_the_flagged_draft_not_a_422() -> None:
    """Decision 2: a post-generation lint failure persists the draft flagged
    rather than erroring. Pre-#656 this branch raised 422 and threw away a
    fully-generated resume the user had already paid for."""
    flagged = _record(
        record_id="rec-flagged",
        lint_violations=[
            LintViolation(code="no_tables", message="Contains a table", severity="error")
        ],
    )
    failure = PipelineLintFailure(
        lint=LintResult(
            ok=False,
            violations=[
                LintViolation(code="no_tables", message="Contains a table", severity="error")
            ],
        ),
        resume=_RESUME,
        warnings=[],
        llm_result=_LLM_RESULT,
        record=flagged,
        payload_md="# Flagged",
    )
    with _pipeline(failure), _client() as tc:
        resp = tc.post("/tailor/resume", json={"job_description": "Build things."})

    assert resp.status_code == 200
    body = resp.json()
    assert body["record"]["id"] == "rec-flagged"
    assert [v["code"] for v in body["record"]["lint_violations"]] == ["no_tables"]


async def test_backgrounded_lint_failure_finishes_rather_than_erroring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A flagged draft is a RESULT, not a failure: the task finishes so the
    client's poll finds a real record. Marking it ``error`` would hide the very
    draft the flagged-persist decision exists to preserve."""
    captured = _capture_spawned(monkeypatch)
    key = run_registry.key_for(user_id=_USER, document_type="resume", job_posting_id=_JOB)
    failure = PipelineLintFailure(
        lint=LintResult(
            ok=False,
            violations=[LintViolation(code="no_tables", message="t", severity="error")],
        ),
        resume=_RESUME,
        warnings=[],
        llm_result=_LLM_RESULT,
        record=_record(
            lint_violations=[LintViolation(code="no_tables", message="t", severity="error")]
        ),
        payload_md="# Flagged",
    )
    try:
        with _pipeline(failure), _client() as tc:
            tc.post("/tailor/resume", json=_resume_body())
            await captured[0]
        assert run_registry.get(key) is None
    finally:
        for coro in captured:
            coro.close()


# ---- Cover letters ---------------------------------------------------------


def test_cover_letter_post_returns_202(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_spawned(monkeypatch)
    success = CoverLetterPipelineSuccess(
        record=_record(document_type="cover_letter"),
        letter=_LETTER,
        warnings=[],
        lint=LintResult(ok=True, violations=[]),
        llm_result=_LLM_RESULT,
    )
    try:
        with _pipeline(success, cover_letter=True), _client() as tc:
            resp = tc.post(
                "/tailor/cover-letter",
                json={
                    "job_description": "Build things.",
                    "company_name": "Acme",
                    "job_posting_id": _JOB,
                },
            )
        assert resp.status_code == 202
        assert len(captured) == 1
    finally:
        for coro in captured:
            coro.close()


async def test_backgrounded_cover_letter_lint_failure_finishes_flagged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover letters now match resumes exactly: a lint failure is a RESULT, not
    a failure. The pipeline persisted a flagged draft, so the task finishes and
    the poll finds a real record — it must NOT mark the run ``error``, which
    would hide the very draft the flagged-persist decision exists to preserve.
    """
    captured = _capture_spawned(monkeypatch)
    key = run_registry.key_for(user_id=_USER, document_type="cover_letter", job_posting_id=_JOB)
    failure = CoverLetterPipelineLintFailure(
        lint=LintResult(
            ok=False,
            violations=[
                LintViolation(code="no_tables", message="Contains a table", severity="error")
            ],
        ),
        letter=_LETTER,
        warnings=[],
        llm_result=_LLM_RESULT,
        record=_record(
            document_type="cover_letter",
            lint_violations=[
                LintViolation(code="no_tables", message="Contains a table", severity="error")
            ],
        ),
        payload_md="# Flagged letter",
    )
    try:
        with _pipeline(failure, cover_letter=True), _client() as tc:
            tc.post(
                "/tailor/cover-letter",
                json={
                    "job_description": "Build things.",
                    "company_name": "Acme",
                    "job_posting_id": _JOB,
                },
            )
            await captured[0]

        # Cleared, not failed — the poll reads the flagged record.
        assert run_registry.get(key) is None
    finally:
        for coro in captured:
            coro.close()


# ---- Poll surface ----------------------------------------------------------


async def test_poll_reports_running_then_the_record() -> None:
    key = run_registry.key_for(user_id=_USER, document_type="resume", job_posting_id=_JOB)
    supabase = MagicMock()

    # In flight, nothing persisted yet.
    run_registry.begin(key, user_id=_USER)
    with patch(
        "app.services.tailor.persistence.get_by_job",
        new_callable=AsyncMock,
        return_value=None,
    ):
        state = await tailor_router.get_resume_by_job(
            job_posting_id=_JOB, supabase=supabase, user_id=_USER
        )
    assert state.status == "running"
    assert state.record is None

    # The row landed. Even with the registry entry not yet cleared, the record
    # WINS — a poll must not bounce back to "generating…" for the width of the
    # window between persist and finish().
    with patch(
        "app.services.tailor.persistence.get_by_job",
        new_callable=AsyncMock,
        return_value=_record(),
    ):
        state = await tailor_router.get_resume_by_job(
            job_posting_id=_JOB, supabase=supabase, user_id=_USER
        )
    assert state.status == "idle"
    assert state.record is not None and state.record.id == "rec-1"


async def test_poll_reports_error_with_its_message() -> None:
    key = run_registry.key_for(user_id=_USER, document_type="resume", job_posting_id=_JOB)
    run_registry.fail(key, message="Resume generation failed. Please retry.")
    with patch(
        "app.services.tailor.persistence.get_by_job",
        new_callable=AsyncMock,
        return_value=None,
    ):
        state = await tailor_router.get_resume_by_job(
            job_posting_id=_JOB, supabase=MagicMock(), user_id=_USER
        )
    assert state.status == "error"
    assert state.message == "Resume generation failed. Please retry."


async def test_poll_is_scoped_to_the_caller() -> None:
    """Another user's in-flight run must not show as ``running`` on my poll."""
    run_registry.begin(
        run_registry.key_for(user_id="someone-else", document_type="resume", job_posting_id=_JOB),
        user_id="someone-else",
    )
    with patch(
        "app.services.tailor.persistence.get_by_job",
        new_callable=AsyncMock,
        return_value=None,
    ):
        state = await tailor_router.get_resume_by_job(
            job_posting_id=_JOB, supabase=MagicMock(), user_id=_USER
        )
    assert state.status == "idle"


async def test_poll_surfaces_a_flagged_draft_with_its_violations() -> None:
    with patch(
        "app.services.tailor.persistence.get_by_job",
        new_callable=AsyncMock,
        return_value=_record(
            lint_violations=[
                LintViolation(code="no_tables", message="Contains a table", severity="error")
            ]
        ),
    ):
        state = await tailor_router.get_resume_by_job(
            job_posting_id=_JOB, supabase=MagicMock(), user_id=_USER
        )
    assert state.status == "idle"
    assert state.record is not None
    assert [v.code for v in state.record.lint_violations or []] == ["no_tables"]


# ---- ATS re-check ----------------------------------------------------------


async def test_ats_recheck_clears_the_flag_once_the_draft_is_fixed() -> None:
    """Decision 3's payoff: lint is deterministic, so confirming a fix costs
    nothing. A clean re-check writes ``[]`` — "linted, clean" — which is what
    actually un-flags the draft."""
    fixed = _record(payload_md=_CLEAN_MD)
    with (
        patch(
            "app.services.tailor.persistence.get",
            new_callable=AsyncMock,
            return_value=_record(
                payload_md=_CLEAN_MD,
                lint_violations=[
                    LintViolation(code="no_tables", message="was flagged", severity="error")
                ],
            ),
        ),
        patch(
            "app.services.tailor.persistence.update_lint_violations",
            new_callable=AsyncMock,
            return_value=fixed,
        ) as mock_update,
    ):
        resp = await tailor_router.recheck_tailored_resume(
            resume_id="rec-1", supabase=MagicMock(), user_id=_USER
        )

    assert resp.ok is True
    assert resp.violations == []
    # Wrote [] (linted clean), NOT None — None means "never linted".
    assert mock_update.call_args.args[2] == []


async def test_ats_recheck_reports_violations_that_remain() -> None:
    md_with_table = "# Daniel\n\n| a | b |\n| - | - |\n| 1 | 2 |\n"
    with (
        patch(
            "app.services.tailor.persistence.get",
            new_callable=AsyncMock,
            return_value=_record(payload_md=md_with_table),
        ),
        patch(
            "app.services.tailor.persistence.update_lint_violations",
            new_callable=AsyncMock,
            return_value=_record(payload_md=md_with_table),
        ) as mock_update,
    ):
        resp = await tailor_router.recheck_tailored_resume(
            resume_id="rec-1", supabase=MagicMock(), user_id=_USER
        )

    assert resp.ok is False
    assert any(v.code == "no_tables" for v in resp.violations)
    assert mock_update.call_args.args[2] != []


async def test_ats_recheck_404s_for_someone_elses_document() -> None:
    """``persistence.get`` returns None both for "missing" and "not yours", so
    a 404 here is also what stops cross-tenant existence from leaking."""
    from fastapi import HTTPException

    with patch("app.services.tailor.persistence.get", new_callable=AsyncMock, return_value=None):
        with pytest.raises(HTTPException) as exc:
            await tailor_router.recheck_tailored_resume(
                resume_id="rec-someone-else", supabase=MagicMock(), user_id=_USER
            )
    assert exc.value.status_code == 404


async def test_ats_recheck_works_for_a_cover_letter() -> None:
    """Letters persist flagged like resumes, so they need the same free way to
    confirm a fix. The lint runs under the row's OWN document_type — the
    cover-letter rule set differs from the resume one."""
    letter = _record(document_type="cover_letter", payload_md=_CLEAN_MD)
    with (
        patch(
            "app.services.tailor.persistence.get",
            new_callable=AsyncMock,
            return_value=letter,
        ),
        patch(
            "app.services.tailor.persistence.update_lint_violations",
            new_callable=AsyncMock,
            return_value=letter,
        ) as mock_update,
        patch("app.routers.tailor.lint_markdown") as mock_lint,
    ):
        mock_lint.return_value = LintResult(ok=True, violations=[])
        resp = await tailor_router.recheck_tailored_resume(
            resume_id="cl-1", supabase=MagicMock(), user_id=_USER
        )

    assert resp.ok is True
    assert mock_lint.call_args.kwargs["document_type"] == "cover_letter"
    assert mock_update.call_args.args[2] == []


async def test_ats_recheck_422s_when_there_is_no_markdown() -> None:
    """Legacy rows predating the markdown pivot have nothing to lint."""
    from fastapi import HTTPException

    with patch(
        "app.services.tailor.persistence.get",
        new_callable=AsyncMock,
        return_value=_record(payload_md=None),
    ):
        with pytest.raises(HTTPException) as exc:
            await tailor_router.recheck_tailored_resume(
                resume_id="rec-legacy", supabase=MagicMock(), user_id=_USER
            )
    assert exc.value.status_code == 422


async def test_ats_recheck_is_allowed_on_an_approved_document() -> None:
    """Re-checking inspects content that didn't change and only refreshes
    metadata about it — and knowing a locked resume has an ATS problem is
    exactly when you'd want to unlock it."""
    approved = _record(approved_at=_NOW)
    with (
        patch(
            "app.services.tailor.persistence.get",
            new_callable=AsyncMock,
            return_value=approved,
        ),
        patch(
            "app.services.tailor.persistence.update_lint_violations",
            new_callable=AsyncMock,
            return_value=approved,
        ),
    ):
        resp = await tailor_router.recheck_tailored_resume(
            resume_id="rec-1", supabase=MagicMock(), user_id=_USER
        )
    assert resp.record.approved_at is not None


# ---- Settings sanity -------------------------------------------------------


def test_concurrency_cap_default_is_conservative() -> None:
    assert Settings().tailor_max_concurrent_runs == 3


def test_dedup_precedes_the_reuse_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second kick while a run is in flight must 202 BEFORE the #504 reuse
    probe runs.

    The probe can clone a similar resume from a sibling posting — so reaching
    it mid-run would let a second tab land a clone alongside the document the
    background run is already generating, and the user would end up with two.
    Ordering dedup first also skips several DB round-trips on a request whose
    only possible answer is "keep polling".
    """
    captured = _capture_spawned(monkeypatch)
    probed: list[str] = []
    try:
        with (
            _pipeline(_success()),
            patch(
                "app.routers.tailor._resolve_target_for_posting",
                new_callable=AsyncMock,
                side_effect=lambda *a, **k: probed.append("probe") or None,
            ),
            _client() as tc,
        ):
            # force_fresh=False so the reuse probe is on the table for both.
            first = tc.post(
                "/tailor/resume",
                json={"job_description": "Build things.", "job_posting_id": _JOB},
            )
            probes_after_first = len(probed)
            second = tc.post(
                "/tailor/resume",
                json={"job_description": "Build things.", "job_posting_id": _JOB},
            )

        assert first.status_code == 202
        assert second.status_code == 202
        assert second.json()["status"] == "running"
        assert len(captured) == 1, "the second kick must not spawn a second run"
        assert len(probed) == probes_after_first, (
            "the second kick reached the reuse probe — dedup ran too late"
        )
    finally:
        for coro in captured:
            coro.close()


# ---- A 202 must mean "work that can actually run" -------------------------


def test_unknown_posting_404s_before_spending_an_llm_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Release-gate finding: ``TailorRequest.job_posting_id`` is a plain ``str``,
    so a bogus id sailed through validation, got a 202, and the detached run
    spent a FULL LLM call before dying on the foreign key at insert — burning
    the caller's daily cap on work that could never have succeeded. Verified
    live: a ``job_posting_id="not-a-uuid"`` kick returned 202 and still wrote
    an ``llm_costs`` row.

    ``/analysis`` already validates its posting synchronously before spawning;
    this is the same contract.
    """
    captured = _capture_spawned(monkeypatch)
    try:
        with (
            _pipeline(_success()) as run,
            patch(
                "app.routers.tailor._posting_exists",
                new_callable=AsyncMock,
                return_value=False,
            ),
            _client() as tc,
        ):
            resp = tc.post("/tailor/resume", json=_resume_body(job_posting_id="ghost"))

        assert resp.status_code == 404
        assert captured == [], "a doomed run must never be spawned"
        run.assert_not_called(), "the LLM must not be reached"
        # And no claim was left behind for the bogus key.
        assert (
            run_registry.is_running(
                run_registry.key_for(user_id=_USER, document_type="resume", job_posting_id="ghost")
            )
            is False
        )
    finally:
        for coro in captured:
            coro.close()


def test_cover_letter_unknown_posting_also_404s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_spawned(monkeypatch)
    try:
        with (
            _pipeline(_success(), cover_letter=True) as run,
            patch(
                "app.routers.tailor._posting_exists",
                new_callable=AsyncMock,
                return_value=False,
            ),
            _client() as tc,
        ):
            resp = tc.post(
                "/tailor/cover-letter",
                json={
                    "job_description": "Build things.",
                    "company_name": "Acme",
                    "job_posting_id": "ghost",
                },
            )
        assert resp.status_code == 404
        assert captured == []
        run.assert_not_called()
    finally:
        for coro in captured:
            coro.close()


def test_a_real_posting_still_kicks_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard must not break the happy path — the negative above is only
    meaningful next to this."""
    captured = _capture_spawned(monkeypatch)
    try:
        with (
            _pipeline(_success()),
            patch(
                "app.routers.tailor._posting_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            _client() as tc,
        ):
            resp = tc.post("/tailor/resume", json=_resume_body())
        assert resp.status_code == 202
        assert len(captured) == 1
    finally:
        for coro in captured:
            coro.close()


@pytest.mark.real_posting_exists
async def test_posting_exists_reads_the_jobs_table_by_id() -> None:
    """Pins the query shape: an indexed id lookup, not a scan — this runs on
    every kick, so it has to stay cheap."""
    from app.routers.tailor import _posting_exists

    supabase = MagicMock()
    chain = supabase.table.return_value.select.return_value.eq.return_value.limit.return_value
    chain.execute = AsyncMock(return_value=MagicMock(data=[{"id": "job-1"}]))
    assert await _posting_exists(supabase, "job-1") is True

    chain.execute = AsyncMock(return_value=MagicMock(data=[]))
    assert await _posting_exists(supabase, "ghost") is False
    supabase.table.assert_called_with("jobs")
