"""Orchestration tests for ``from_input`` (create-or-link + deferred derive).

The inline path (``from_manual`` / ``from_url``) only runs the normalize
LLM call (manual) or no LLM call (URL) before linking the user and
returning. The expensive ``derive_profile_*`` + ``derive_fit_score`` work
is scheduled onto a ``BackgroundTask`` — these tests assert both halves:

* the inline path links + schedules the right background function without
  touching derive/fit, and
* the background functions (``derive_manual_target_bg`` /
  ``derive_url_target_bg``) derive the profile, flip the activation status,
  and upsert the fit score — marking the target ``error`` on failure.

The LLM and crud helpers are monkeypatched so the focus stays on
orchestration — the underlying pieces have their own dedicated tests.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import BackgroundTasks

from app.config import settings
from app.models.experience import OptimizedPayload
from app.models.llm import LLMResult, LLMUsage
from app.models.targets import (
    DerivedTarget,
    JobTarget,
    ScoringProfile,
    TargetCreate,
    TargetReferenceJD,
    TargetSuggestion,
    TargetUpdate,
    UserTarget,
)
from app.services.llm import cost_log
from app.services.targets import crud, from_input
from app.services.targets.fit_score import FitScoreResult

# ---- Helpers ----------------------------------------------------------------


def _llm_result() -> LLMResult:
    return LLMResult(
        content="{}",
        model="claude-sonnet-4-6",
        usage=LLMUsage(input_tokens=1, output_tokens=1),
        cost_usd=0.0001,
        latency_ms=10,
    )


def _target(
    *,
    id: str = "t-1",
    label: str = "Senior Frontend Engineer",
    profile_version: int = 1,
    description: str | None = None,
    activation_status: str = "idle",
) -> JobTarget:
    now = datetime.now(UTC)
    return JobTarget(
        id=id,
        label=label,
        description=description,
        normalized_label=label.lower().strip(),
        scoring_profile=ScoringProfile(),
        search_keywords=["frontend"],
        activation_status=activation_status,
        profile_version=profile_version,
        is_active=False,
        created_at=now,
        updated_at=now,
    )


def _user_target(
    *,
    user_id: str = "user-1",
    target_id: str = "t-1",
    fit_score: int | None = None,
) -> UserTarget:
    now = datetime.now(UTC)
    return UserTarget(
        id="ut-1",
        user_id=user_id,
        target_id=target_id,
        is_active=False,
        fit_score=fit_score,
        fit_score_reasoning="Strong fit." if fit_score is not None else None,
        created_at=now,
        updated_at=now,
    )


def _ref_jd(*, target_id: str = "t-1", jd_url: str | None = None) -> TargetReferenceJD:
    return TargetReferenceJD(
        id="ref-1",
        target_id=target_id,
        jd_url=jd_url,
        jd_text="x" * 100,
        extracted_profile=ScoringProfile(),
        created_at=datetime.now(UTC),
    )


class _Recorder:
    """Captures calls to monkeypatched async/sync helpers."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def record(self, name: str, **kwargs: Any) -> None:
        self.calls.append((name, kwargs))

    def names(self) -> list[str]:
        return [n for n, _ in self.calls]

    def by_name(self, name: str) -> list[dict[str, Any]]:
        return [kw for n, kw in self.calls if n == name]


@pytest.fixture
def recorder() -> _Recorder:
    return _Recorder()


@pytest.fixture
def stub_llm_helpers(monkeypatch: pytest.MonkeyPatch, recorder: _Recorder) -> _Recorder:
    """Stub all LLM-driven helpers and cost_log so the orchestration runs offline."""

    async def fake_normalize(llm, *, label, description, payload):  # type: ignore[no-untyped-def]
        recorder.record("normalize", label=label, description=description)
        return (
            TargetSuggestion(
                label="Senior Frontend Engineer",
                description="Canonical description.",
                core_skills=["React"],
            ),
            _llm_result(),
        )

    async def fake_derive_label(llm, *, label):  # type: ignore[no-untyped-def]
        recorder.record("derive_from_label", label=label)
        return (
            DerivedTarget(
                scoring_profile=ScoringProfile(),
                search_keywords=["frontend engineer"],
            ),
            _llm_result(),
        )

    async def fake_derive_jd(llm, *, jd_text, **_kwargs):  # type: ignore[no-untyped-def]
        recorder.record("derive_from_jd", jd_len=len(jd_text))
        return (
            DerivedTarget(
                scoring_profile=ScoringProfile(),
                search_keywords=["frontend engineer", "ui engineer"],
            ),
            _llm_result(),
        )

    async def fake_fit_score(llm, *, payload, target):  # type: ignore[no-untyped-def]
        recorder.record("fit_score", target_id=target.id)
        return FitScoreResult(fit_score=82, reasoning="Strong fit."), _llm_result()

    # _apply_fit_score resolves a payload fresh vs. the current master doc
    # (BUG 2 seam) before scoring; stub it so the fit path has an experience
    # payload without hitting the real prose/optimized reads.
    async def fake_resolve(supabase, llm, *, cost_supabase, user_id):  # type: ignore[no-untyped-def]
        recorder.record("resolve_payload", user_id=user_id)
        return OptimizedPayload()

    def fake_cost_record(supabase, **kwargs):  # type: ignore[no-untyped-def]
        recorder.record("cost_log", **kwargs)

    # The from-url flow now materializes the posting as a job (job_ingest) +
    # saves it (user_jobs). Stub both so the orchestration tests stay offline;
    # they record so tests can assert the posting was materialized + saved.
    async def fake_materialize(supabase, **kwargs):  # type: ignore[no-untyped-def]
        recorder.record(
            "materialize_job",
            title=kwargs.get("title"),
            company_name=kwargs.get("company_name"),
            target_ids=[t.id for t in kwargs.get("targets", [])],
        )
        return "posting-1"

    def fake_upsert_user_job(supabase, **kwargs):  # type: ignore[no-untyped-def]
        recorder.record("user_job", **kwargs)

    monkeypatch.setattr(from_input, "normalize_manual_input", fake_normalize)
    monkeypatch.setattr(from_input, "derive_profile_from_label", fake_derive_label)
    monkeypatch.setattr(from_input, "derive_profile_from_jd", fake_derive_jd)
    monkeypatch.setattr(from_input, "derive_fit_score", fake_fit_score)
    monkeypatch.setattr(from_input, "resolve_current_payload", fake_resolve)
    monkeypatch.setattr(from_input, "materialize_and_score_job", fake_materialize)
    monkeypatch.setattr(from_input.persistence, "upsert_user_job", fake_upsert_user_job)
    monkeypatch.setattr(cost_log, "record", fake_cost_record)
    return recorder


@pytest.fixture
def stub_crud(monkeypatch: pytest.MonkeyPatch, recorder: _Recorder) -> _Recorder:
    """Stub crud link/update; specific tests override create/get as needed."""

    def fake_link(supabase, **kwargs):  # type: ignore[no-untyped-def]
        recorder.record("link", **kwargs)
        return _user_target(
            user_id=kwargs["user_id"],
            target_id=kwargs["target_id"],
            fit_score=kwargs.get("fit_score"),
        )

    def fake_update(supabase, target_id, body):  # type: ignore[no-untyped-def]
        recorder.record("update", target_id=target_id, body=body)
        return _target(
            id=target_id,
            activation_status=body.activation_status or "idle",
            profile_version=body.profile_version or 1,
        )

    # Default the URL corpus path under the per-user reference-JD cap; the
    # cap-specific test overrides this. Only derive_url_target_bg reads it.
    monkeypatch.setattr(crud, "count_user_reference_jds", lambda _s, **kw: 0)

    monkeypatch.setattr(crud, "link_user_to_target", fake_link)
    monkeypatch.setattr(crud, "update", fake_update)
    return recorder


def _scheduled(bg: BackgroundTasks) -> list[tuple[Any, dict[str, Any]]]:
    """(func, kwargs) for each task queued on a BackgroundTasks instance."""
    return [(t.func, t.kwargs) for t in bg.tasks]


# ---- from_manual: inline path -----------------------------------------------


@pytest.mark.asyncio
async def test_from_manual_matched_links_inline_defers_fit_score(
    monkeypatch: pytest.MonkeyPatch,
    stub_llm_helpers: _Recorder,
    stub_crud: _Recorder,
) -> None:
    """Matched → link inline (no fit yet), schedule the fit-score task only."""
    supabase = MagicMock()
    matched = _target(id="existing")
    monkeypatch.setattr(from_input, "find_matching_target", lambda _s, _l: matched)

    create_calls: list[TargetCreate] = []
    monkeypatch.setattr(
        crud,
        "create",
        lambda _s, *, payload: create_calls.append(payload) or matched,  # type: ignore[func-returns-value]
    )

    bg = BackgroundTasks()
    result = await from_input.from_manual(
        supabase,
        MagicMock(),
        bg,
        user_id="user-1",
        label="sr fe eng",
        description=None,
        payload=OptimizedPayload(),
    )

    assert result.was_matched is True
    assert result.target.id == "existing"
    # Inline: normalize only — no derive/fit/create.
    assert "normalize" in stub_llm_helpers.names()
    assert "derive_from_label" not in stub_llm_helpers.names()
    assert "fit_score" not in stub_llm_helpers.names()
    assert create_calls == []
    # Linked inactive with no fit score yet.
    link_kwargs = stub_crud.by_name("link")[0]
    assert link_kwargs["is_active"] is False
    assert link_kwargs["target_id"] == "existing"
    assert link_kwargs.get("fit_score") is None
    # Deferred: fit-score task only.
    scheduled = _scheduled(bg)
    assert len(scheduled) == 1
    func, kwargs = scheduled[0]
    assert func is from_input._apply_fit_score
    assert kwargs["target"].id == "existing"
    assert kwargs["user_id"] == "user-1"


@pytest.mark.asyncio
async def test_from_manual_new_creates_deriving_and_schedules_derivation(
    monkeypatch: pytest.MonkeyPatch,
    stub_llm_helpers: _Recorder,
    stub_crud: _Recorder,
) -> None:
    """New → create in 'deriving', link inline, schedule full derivation."""
    supabase = MagicMock()
    monkeypatch.setattr(from_input, "find_matching_target", lambda _s, _l: None)

    created = _target(id="new", label="Senior Frontend Engineer")
    create_calls: list[TargetCreate] = []
    monkeypatch.setattr(
        crud,
        "create",
        lambda _s, *, payload: create_calls.append(payload) or created,  # type: ignore[func-returns-value]
    )

    bg = BackgroundTasks()
    result = await from_input.from_manual(
        supabase,
        MagicMock(),
        bg,
        user_id="user-1",
        label="sr fe eng",
        description="frontend roles at growth-stage companies",
        payload=OptimizedPayload(),
    )

    assert result.was_matched is False
    assert result.target.id == "new"
    # Returned target carries the deriving status for the FE pending UI.
    assert result.target.activation_status == "deriving"
    # Inline: normalize only, no derive/fit.
    assert stub_llm_helpers.names().count("normalize") == 1
    assert "derive_from_label" not in stub_llm_helpers.names()
    assert "fit_score" not in stub_llm_helpers.names()
    # Created from the canonical suggestion (label + description), empty profile.
    assert len(create_calls) == 1
    assert create_calls[0].label == "Senior Frontend Engineer"
    assert create_calls[0].description == "Canonical description."
    assert create_calls[0].search_keywords == []
    # Status flipped to 'deriving' inline.
    update_call = stub_crud.by_name("update")[0]
    assert update_call["body"].activation_status == "deriving"
    # Linked inactive, no fit score yet.
    link_kwargs = stub_crud.by_name("link")[0]
    assert link_kwargs["target_id"] == "new"
    assert link_kwargs["is_active"] is False
    assert link_kwargs.get("fit_score") is None
    # Deferred: the manual derivation task.
    scheduled = _scheduled(bg)
    assert len(scheduled) == 1
    func, kwargs = scheduled[0]
    assert func is from_input.derive_manual_target_bg
    assert kwargs["target_id"] == "new"
    assert kwargs["label"] == "Senior Frontend Engineer"


@pytest.mark.asyncio
async def test_from_manual_malformed_llm_output_raises_clean_502(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed LLM response (fails TargetSuggestion validation) yields a
    clean 502 HTTPException, not an unhandled pydantic ValidationError /
    raw 500 (Finding 2)."""
    import pydantic
    from fastapi import HTTPException

    # Reproduce the real failure: the schema parse inside
    # normalize_manual_input raises a pydantic.ValidationError.
    def _validation_error() -> pydantic.ValidationError:
        try:
            TargetSuggestion.model_validate({})  # missing required fields
        except pydantic.ValidationError as exc:
            return exc
        raise AssertionError("expected TargetSuggestion validation to fail")

    async def fake_normalize(llm, *, label, description, payload):  # type: ignore[no-untyped-def]
        raise _validation_error()

    monkeypatch.setattr(from_input, "normalize_manual_input", fake_normalize)
    # If the guard works, matching/crud are never reached — fail loudly if they are.
    monkeypatch.setattr(
        from_input,
        "find_matching_target",
        lambda *_a, **_kw: pytest.fail("should not reach matching on malformed LLM"),
    )

    with pytest.raises(HTTPException) as exc_info:
        await from_input.from_manual(
            MagicMock(),
            MagicMock(),
            BackgroundTasks(),
            user_id="user-1",
            label="sr fe eng",
            description=None,
            payload=OptimizedPayload(),
        )

    assert exc_info.value.status_code == 502
    detail = exc_info.value.detail.lower()
    # User-facing, retry-friendly message — no traceback / pydantic internals.
    assert "try again" in detail
    assert "validationerror" not in detail
    assert "field required" not in detail


# ---- derive_manual_target_bg: background path -------------------------------


@pytest.mark.asyncio
async def test_derive_manual_target_bg_derives_profile_and_fit(
    monkeypatch: pytest.MonkeyPatch,
    stub_llm_helpers: _Recorder,
    stub_crud: _Recorder,
) -> None:
    """Background: derive profile → status idle → fit score upserted."""
    supabase = MagicMock()

    await from_input.derive_manual_target_bg(
        supabase,
        MagicMock(),
        user_id="user-1",
        target_id="new",
        label="Senior Frontend Engineer",
        payload=OptimizedPayload(),
    )

    names = stub_llm_helpers.names()
    assert "derive_from_label" in names
    assert "fit_score" in names
    # Profile update sets keywords and flips status to idle.
    update_body: TargetUpdate = stub_crud.by_name("update")[0]["body"]
    assert update_body.search_keywords == ["frontend engineer"]
    assert update_body.activation_status == "idle"
    # Fit score upserted onto the link.
    link_kwargs = stub_crud.by_name("link")[0]
    assert link_kwargs["fit_score"] == 82
    assert link_kwargs["is_active"] is False
    # Cost logged for both deferred calls.
    purposes = [c["purpose"] for c in stub_llm_helpers.by_name("cost_log")]
    assert "target.derive_from_label" in purposes
    assert "target.fit_score" in purposes


@pytest.mark.asyncio
async def test_derive_manual_target_bg_marks_error_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    stub_llm_helpers: _Recorder,
    stub_crud: _Recorder,
) -> None:
    """A failing derive flips the target to 'error' and never links a fit."""
    supabase = MagicMock()

    async def boom(llm, *, label, payload):  # type: ignore[no-untyped-def]
        raise RuntimeError("LLM down")

    monkeypatch.setattr(from_input, "derive_profile_from_label", boom)

    await from_input.derive_manual_target_bg(
        supabase,
        MagicMock(),
        user_id="user-1",
        target_id="new",
        label="Senior Frontend Engineer",
        payload=OptimizedPayload(),
    )

    update_bodies = [c["body"] for c in stub_crud.by_name("update")]
    assert any(b.activation_status == "error" for b in update_bodies)
    assert "fit_score" not in stub_llm_helpers.names()
    assert stub_crud.by_name("link") == []


@pytest.mark.asyncio
async def test_derive_manual_target_bg_marks_error_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
    stub_llm_helpers: _Recorder,
    stub_crud: _Recorder,
) -> None:
    """A derive that exceeds the timeout is cancelled and flips to 'error'."""
    supabase = MagicMock()
    # Shrink the ceiling so the test doesn't actually wait.
    monkeypatch.setattr(from_input, "DERIVATION_TIMEOUT_S", 0.05)

    async def hang(llm, *, label, payload):  # type: ignore[no-untyped-def]
        await asyncio.sleep(1)
        raise AssertionError("should have timed out")

    monkeypatch.setattr(from_input, "derive_profile_from_label", hang)

    await from_input.derive_manual_target_bg(
        supabase,
        MagicMock(),
        user_id="user-1",
        target_id="new",
        label="Senior Frontend Engineer",
        payload=OptimizedPayload(),
    )

    update_bodies = [c["body"] for c in stub_crud.by_name("update")]
    assert any(b.activation_status == "error" for b in update_bodies)
    # Timed out before any profile update / fit score landed.
    assert all(b.activation_status != "idle" for b in update_bodies)
    assert "fit_score" not in stub_llm_helpers.names()
    assert stub_crud.by_name("link") == []


# ---- from_suggestion: inline path (profile-independent, no normalize) --------


@pytest.mark.asyncio
async def test_from_suggestion_new_creates_without_normalize_call(
    monkeypatch: pytest.MonkeyPatch,
    stub_llm_helpers: _Recorder,
    stub_crud: _Recorder,
) -> None:
    """New suggestion → create in 'deriving' + link + defer derivation, and —
    unlike from_manual — NO inline normalize LLM call (label already canonical)."""
    supabase = MagicMock()
    monkeypatch.setattr(from_input, "find_matching_target", lambda _s, _l: None)

    created = _target(id="new", label="Senior Frontend Engineer")
    create_calls: list[TargetCreate] = []
    monkeypatch.setattr(
        crud,
        "create",
        lambda _s, *, payload: create_calls.append(payload) or created,  # type: ignore[func-returns-value]
    )

    bg = BackgroundTasks()
    result = await from_input.from_suggestion(
        supabase,
        MagicMock(),
        bg,
        user_id="user-1",
        label="Senior Frontend Engineer",
        description="Frontend roles.",
        payload=OptimizedPayload(),
    )

    assert result.was_matched is False
    assert result.target.activation_status == "deriving"
    # The distinguishing property: NO normalization LLM call.
    assert "normalize" not in stub_llm_helpers.names()
    # Created straight from the (already-canonical) label + description.
    assert create_calls[0].label == "Senior Frontend Engineer"
    assert create_calls[0].description == "Frontend roles."
    link_kwargs = stub_crud.by_name("link")[0]
    assert link_kwargs["is_active"] is False  # never trips the active cap
    scheduled = _scheduled(bg)
    assert len(scheduled) == 1
    assert scheduled[0][0] is from_input.derive_manual_target_bg


@pytest.mark.asyncio
async def test_from_suggestion_matched_dedups_to_existing_row(
    monkeypatch: pytest.MonkeyPatch,
    stub_llm_helpers: _Recorder,
    stub_crud: _Recorder,
) -> None:
    """A suggestion whose label matches an existing catalog row LINKS it instead
    of creating a duplicate — the server-side dedup that a stale client
    ``is_new`` can't defeat."""
    supabase = MagicMock()
    matched = _target(id="existing")
    monkeypatch.setattr(from_input, "find_matching_target", lambda _s, _l: matched)
    create_spy = MagicMock()
    monkeypatch.setattr(crud, "create", create_spy)

    bg = BackgroundTasks()
    result = await from_input.from_suggestion(
        supabase,
        MagicMock(),
        bg,
        user_id="user-1",
        label="Senior Frontend Engineer",
        description="Frontend roles.",
        payload=OptimizedPayload(),
    )

    assert result.was_matched is True
    assert result.target.id == "existing"
    create_spy.assert_not_called()  # NO duplicate row minted
    # Fit-score deferred (we have a profile).
    scheduled = _scheduled(bg)
    assert len(scheduled) == 1
    assert scheduled[0][0] is from_input._apply_fit_score


@pytest.mark.asyncio
async def test_from_suggestion_no_profile_skips_fit_score(
    monkeypatch: pytest.MonkeyPatch,
    stub_llm_helpers: _Recorder,
    stub_crud: _Recorder,
) -> None:
    """payload=None (user has no experience profile): a matched suggestion links
    but schedules NO fit-score task (fit needs the profile). Profile-independent
    is the whole point of the search fallback."""
    supabase = MagicMock()
    matched = _target(id="existing")
    monkeypatch.setattr(from_input, "find_matching_target", lambda _s, _l: matched)

    bg = BackgroundTasks()
    result = await from_input.from_suggestion(
        supabase,
        MagicMock(),
        bg,
        user_id="user-1",
        label="Senior Frontend Engineer",
        description=None,
        payload=None,
    )

    assert result.was_matched is True
    # Linked, but NO fit-score task — nothing to score against.
    assert stub_crud.by_name("link")[0]["is_active"] is False
    assert _scheduled(bg) == []


@pytest.mark.asyncio
async def test_derive_manual_target_bg_skips_fit_when_no_payload(
    monkeypatch: pytest.MonkeyPatch,
    stub_llm_helpers: _Recorder,
    stub_crud: _Recorder,
) -> None:
    """Background derive with payload=None still derives the label profile
    (status→idle) but skips the fit score."""
    supabase = MagicMock()

    await from_input.derive_manual_target_bg(
        supabase,
        MagicMock(),
        user_id="user-1",
        target_id="new",
        label="Senior Frontend Engineer",
        payload=None,
    )

    names = stub_llm_helpers.names()
    assert "derive_from_label" in names  # profile still derived from the label
    assert "fit_score" not in names  # but no per-user fit score
    update_body: TargetUpdate = stub_crud.by_name("update")[0]["body"]
    assert update_body.activation_status == "idle"
    assert stub_crud.by_name("link") == []  # fit-score link upsert skipped


# ---- from_url: inline path --------------------------------------------------


@pytest.mark.asyncio
async def test_from_url_matched_links_inline_defers_derivation(
    monkeypatch: pytest.MonkeyPatch,
    stub_llm_helpers: _Recorder,
    stub_crud: _Recorder,
) -> None:
    """Matched URL → link inline, schedule corpus-building derivation."""
    supabase = MagicMock()
    matched = _target(id="existing", profile_version=4)
    monkeypatch.setattr(from_input, "find_matching_target", lambda _s, _l: matched)

    bg = BackgroundTasks()
    result = await from_input.from_url(
        supabase,
        MagicMock(),
        bg,
        user_id="user-1",
        final_url="https://example.com/jobs/123",
        extracted_title="Senior Frontend Engineer",
        jd_text="x" * 200,
        company_name="Acme",
        location=None,
        salary_text=None,
        payload=OptimizedPayload(),
    )

    assert result.was_matched is True
    assert result.target.id == "existing"
    # No LLM call inline for the URL flow — neither derive nor fit.
    assert "derive_from_jd" not in stub_llm_helpers.names()
    assert "fit_score" not in stub_llm_helpers.names()
    link_kwargs = stub_crud.by_name("link")[0]
    assert link_kwargs["target_id"] == "existing"
    assert link_kwargs["is_active"] is False
    scheduled = _scheduled(bg)
    # Two bg tasks: the profile derive, then the source registration.
    assert len(scheduled) == 2
    func, kwargs = scheduled[0]
    assert func is from_input.derive_url_target_bg
    assert kwargs["target_id"] == "existing"
    assert kwargs["jd_text"] == "x" * 200
    # Source registration is scheduled AFTER derive, carrying the pasted URL.
    reg_func, reg_kwargs = scheduled[1]
    assert reg_func is from_input.register_source_from_url
    assert reg_kwargs["final_url"] == "https://example.com/jobs/123"


@pytest.mark.asyncio
async def test_from_url_new_creates_deriving_and_schedules(
    monkeypatch: pytest.MonkeyPatch,
    stub_llm_helpers: _Recorder,
    stub_crud: _Recorder,
) -> None:
    """New URL → create in 'deriving', link, schedule derivation."""
    supabase = MagicMock()
    monkeypatch.setattr(from_input, "find_matching_target", lambda _s, _l: None)

    created = _target(id="new", label="Senior Frontend Engineer")
    create_calls: list[TargetCreate] = []
    monkeypatch.setattr(
        crud,
        "create",
        lambda _s, *, payload: create_calls.append(payload) or created,  # type: ignore[func-returns-value]
    )

    bg = BackgroundTasks()
    result = await from_input.from_url(
        supabase,
        MagicMock(),
        bg,
        user_id="user-1",
        final_url="https://example.com/jobs/abc",
        extracted_title="Senior Frontend Engineer",
        jd_text="x" * 200,
        company_name="Acme",
        location=None,
        salary_text=None,
        payload=OptimizedPayload(),
    )

    assert result.was_matched is False
    assert result.target.id == "new"
    assert result.target.activation_status == "deriving"
    assert "derive_from_jd" not in stub_llm_helpers.names()
    assert len(create_calls) == 1
    assert create_calls[0].label == "Senior Frontend Engineer"
    scheduled = _scheduled(bg)
    assert len(scheduled) == 2
    func, kwargs = scheduled[0]
    assert func is from_input.derive_url_target_bg
    assert kwargs["target_id"] == "new"
    reg_func, reg_kwargs = scheduled[1]
    assert reg_func is from_input.register_source_from_url
    assert reg_kwargs["final_url"] == "https://example.com/jobs/abc"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extracted_title", "expected"),
    [
        ("Extracted Title", "Extracted Title"),
        ("  Spaced Title  ", "Spaced Title"),  # trimmed
        (None, "Untitled Target"),
        ("   ", "Untitled Target"),  # whitespace-only → fallback
    ],
)
async def test_from_url_label_resolution(
    monkeypatch: pytest.MonkeyPatch,
    stub_llm_helpers: _Recorder,
    stub_crud: _Recorder,
    extracted_title: str | None,
    expected: str,
) -> None:
    """Label always derives from the posting title (there is no user override) —
    trimmed, with a blank/whitespace-only title falling back to 'Untitled Target'."""
    supabase = MagicMock()
    seen_labels: list[str] = []
    monkeypatch.setattr(
        from_input,
        "find_matching_target",
        lambda _s, label: seen_labels.append(label) or None,  # type: ignore[func-returns-value]
    )
    monkeypatch.setattr(crud, "create", lambda _s, *, payload: _target(id="new", label=expected))

    bg = BackgroundTasks()
    await from_input.from_url(
        supabase,
        MagicMock(),
        bg,
        user_id="user-1",
        final_url="https://example.com/jobs/x",
        extracted_title=extracted_title,
        jd_text="x" * 200,
        company_name="Acme",
        location=None,
        salary_text=None,
        payload=OptimizedPayload(),
    )

    assert seen_labels == [expected]


# ---- derive_url_target_bg: background path -----------------------------------


@pytest.mark.asyncio
async def test_derive_url_target_bg_new_attributes_and_rpc_merges(
    monkeypatch: pytest.MonkeyPatch,
    stub_llm_helpers: _Recorder,
    stub_crud: _Recorder,
) -> None:
    """New URL target: the JD is attributed to the contributor and the shared
    profile is written through the #191 merge RPC (not a raw ``.update()``),
    then the status flips idle and the fit score follows (SEC-1)."""
    supabase = MagicMock()

    add_ref_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        crud,
        "add_reference_jd",
        lambda _s, **kw: (
            add_ref_calls.append(kw) or _ref_jd(target_id=kw["target_id"], jd_url=kw["jd_url"])
        ),  # type: ignore[func-returns-value]
    )
    monkeypatch.setattr(crud, "list_reference_jds", lambda _s, _t: [_ref_jd(target_id="new")])
    monkeypatch.setattr(crud, "get", lambda _s, _id: _target(id="new", profile_version=1))

    rpc_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        from_input,
        "apply_profile_merge_rpc",
        lambda _s, **kw: rpc_calls.append(kw) or ("applied", 2),  # type: ignore[func-returns-value]
    )

    await from_input.derive_url_target_bg(
        supabase,
        MagicMock(),
        user_id="user-1",
        target_id="new",
        jd_text="x" * 200,
        final_url="https://example.com/jobs/abc",
        extracted_title="Senior Frontend Engineer",
        company_name="Acme",
        location=None,
        salary_text=None,
        payload=OptimizedPayload(),
    )

    assert "derive_from_jd" in stub_llm_helpers.names()
    # JD attributed to the contributor (enables the #5 contributor de-bias + #47 cap).
    assert add_ref_calls and add_ref_calls[0]["user_id"] == "user-1"
    # Shared-profile write went through the version-checked merge RPC, not .update().
    assert rpc_calls and rpc_calls[0]["user_id"] == "user-1"
    assert rpc_calls[0]["expected_version"] == 1
    # Status flip (non-shared column) + fit score still happen.
    update_body: TargetUpdate = stub_crud.by_name("update")[0]["body"]
    assert update_body.activation_status == "idle"
    assert stub_crud.by_name("link")[0]["fit_score"] == 82


@pytest.mark.asyncio
async def test_derive_url_target_bg_matched_merges_via_rpc(
    monkeypatch: pytest.MonkeyPatch,
    stub_llm_helpers: _Recorder,
    stub_crud: _Recorder,
) -> None:
    """Matched (corpus-building) URL target: the merge RPC is version-checked
    against the target's current profile_version (SEC-1)."""
    supabase = MagicMock()

    monkeypatch.setattr(crud, "add_reference_jd", lambda _s, **kw: _ref_jd())
    monkeypatch.setattr(
        crud,
        "list_reference_jds",
        lambda _s, _t: [_ref_jd(target_id="existing"), _ref_jd(target_id="existing")],
    )
    monkeypatch.setattr(crud, "get", lambda _s, _id: _target(id="existing", profile_version=4))

    rpc_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        from_input,
        "apply_profile_merge_rpc",
        lambda _s, **kw: rpc_calls.append(kw) or ("applied", 5),  # type: ignore[func-returns-value]
    )

    await from_input.derive_url_target_bg(
        supabase,
        MagicMock(),
        user_id="user-1",
        target_id="existing",
        jd_text="x" * 200,
        final_url="https://example.com/jobs/123",
        extracted_title="Senior Frontend Engineer",
        company_name="Acme",
        location=None,
        salary_text=None,
        payload=OptimizedPayload(),
    )

    assert rpc_calls and rpc_calls[0]["expected_version"] == 4  # version-checked
    update_body: TargetUpdate = stub_crud.by_name("update")[0]["body"]
    assert update_body.activation_status == "idle"


@pytest.mark.asyncio
async def test_derive_url_target_bg_materializes_and_saves_posting(
    monkeypatch: pytest.MonkeyPatch,
    stub_llm_helpers: _Recorder,
    stub_crud: _Recorder,
) -> None:
    """The URL posting itself becomes a real, tailorable job: scored against the
    freshly-derived target (``targets=[target]``) and saved to the user's pipeline
    (``status='saved'``). This is the gap the from-url flow used to have — the JD
    was dissolved into the profile as a reference JD and never became a job, so no
    resume/cover letter could be generated for it."""
    supabase = MagicMock()
    monkeypatch.setattr(crud, "add_reference_jd", lambda _s, **kw: _ref_jd(target_id="new"))
    monkeypatch.setattr(crud, "list_reference_jds", lambda _s, _t: [_ref_jd(target_id="new")])
    monkeypatch.setattr(crud, "get", lambda _s, _id: _target(id="new", profile_version=1))
    monkeypatch.setattr(
        from_input,
        "apply_profile_merge_rpc",
        lambda _s, **kw: ("applied", 2),
    )

    await from_input.derive_url_target_bg(
        supabase,
        MagicMock(),
        user_id="user-1",
        target_id="new",
        jd_text="x" * 200,
        final_url="https://example.com/jobs/abc",
        extracted_title="Senior Frontend Engineer",
        company_name="Acme",
        location=None,
        salary_text=None,
        payload=OptimizedPayload(),
    )

    # Posting scored against the just-derived target (the re-read canonical row).
    mat = stub_llm_helpers.by_name("materialize_job")
    assert mat and mat[0]["target_ids"] == ["new"]
    assert mat[0]["title"] == "Senior Frontend Engineer"
    # ...and saved to the user's pipeline so it shows in /jobs + is tailorable.
    saved = stub_llm_helpers.by_name("user_job")
    assert saved and saved[0]["status"] == "saved"
    assert saved[0]["job_posting_id"] == "posting-1"


@pytest.mark.asyncio
async def test_derive_url_target_bg_over_cap_skips_shared_write(
    monkeypatch: pytest.MonkeyPatch,
    stub_llm_helpers: _Recorder,
    stub_crud: _Recorder,
) -> None:
    """A contributor already at the per-user reference-JD cap makes NO change to
    the shared profile — no JD stored, no merge RPC — but still gets a fit score
    against the existing profile (SEC-1 cap)."""
    supabase = MagicMock()
    monkeypatch.setattr(
        crud,
        "count_user_reference_jds",
        lambda _s, **kw: settings.reference_jd_max_per_user_per_target,
    )
    add_ref_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        crud,
        "add_reference_jd",
        lambda _s, **kw: add_ref_calls.append(kw),  # type: ignore[func-returns-value]
    )
    rpc_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        from_input,
        "apply_profile_merge_rpc",
        lambda _s, **kw: rpc_calls.append(kw) or ("applied", 2),  # type: ignore[func-returns-value]
    )
    monkeypatch.setattr(crud, "get", lambda _s, _id: _target(id="existing", profile_version=4))

    await from_input.derive_url_target_bg(
        supabase,
        MagicMock(),
        user_id="user-1",
        target_id="existing",
        jd_text="x" * 200,
        final_url="https://example.com/jobs/123",
        extracted_title="Senior Frontend Engineer",
        company_name="Acme",
        location=None,
        salary_text=None,
        payload=OptimizedPayload(),
    )

    # Over cap → no shared-profile mutation at all.
    assert add_ref_calls == []
    assert rpc_calls == []
    # But the user still gets their fit score.
    assert stub_crud.by_name("link")[0]["fit_score"] == 82


@pytest.mark.asyncio
async def test_derive_url_target_bg_marks_error_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    stub_llm_helpers: _Recorder,
    stub_crud: _Recorder,
) -> None:
    supabase = MagicMock()

    async def boom(llm, *, jd_text):  # type: ignore[no-untyped-def]
        raise RuntimeError("LLM down")

    monkeypatch.setattr(from_input, "derive_profile_from_jd", boom)

    await from_input.derive_url_target_bg(
        supabase,
        MagicMock(),
        user_id="user-1",
        target_id="new",
        jd_text="x" * 200,
        final_url="https://example.com/jobs/abc",
        extracted_title="Senior Frontend Engineer",
        company_name="Acme",
        location=None,
        salary_text=None,
        payload=OptimizedPayload(),
    )

    update_bodies = [c["body"] for c in stub_crud.by_name("update")]
    assert any(b.activation_status == "error" for b in update_bodies)
    assert stub_crud.by_name("link") == []


@pytest.mark.asyncio
async def test_derive_url_target_bg_marks_error_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
    stub_llm_helpers: _Recorder,
    stub_crud: _Recorder,
) -> None:
    """A JD derive that exceeds the timeout flips the target to 'error'."""
    supabase = MagicMock()
    monkeypatch.setattr(from_input, "DERIVATION_TIMEOUT_S", 0.05)

    async def hang(llm, *, jd_text):  # type: ignore[no-untyped-def]
        await asyncio.sleep(1)
        raise AssertionError("should have timed out")

    monkeypatch.setattr(from_input, "derive_profile_from_jd", hang)

    await from_input.derive_url_target_bg(
        supabase,
        MagicMock(),
        user_id="user-1",
        target_id="new",
        jd_text="x" * 200,
        final_url="https://example.com/jobs/abc",
        extracted_title="Senior Frontend Engineer",
        company_name="Acme",
        location=None,
        salary_text=None,
        payload=OptimizedPayload(),
    )

    update_bodies = [c["body"] for c in stub_crud.by_name("update")]
    assert any(b.activation_status == "error" for b in update_bodies)
    assert all(b.activation_status != "idle" for b in update_bodies)
    assert stub_crud.by_name("link") == []
