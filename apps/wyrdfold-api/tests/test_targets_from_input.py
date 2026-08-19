"""Orchestration tests for ``from_input`` (create-or-link + deferred derive).

The inline path (``from_manual`` / ``from_url``) only runs the normalize
LLM call (manual) or no LLM call (URL) before linking the user and
returning. The expensive ``derive_profile_*`` + ``derive_fit_score`` work
is spawned as a DETACHED loop task — these tests assert both halves:

* the inline path links + schedules the right background function without
  touching derive/fit, and
* the background functions (``derive_manual_target_bg`` /
  ``derive_url_target_bg``) derive the profile, flip the activation status,
  and upsert the fit score — marking the target ``error`` on failure.

#57 PR-G2b: ``from_input`` runs on the pooled async service client. Its crud
reads/writes are module-inline async helpers (``_create_and_link`` / ``_update`` /
``_link`` / ``_get`` / ``_add_reference_jd`` / ``_list_reference_jds`` /
``_count_user_reference_jds``), the cost ledger is ``cost_log.record_async``,
the #191 merge is ``apply_profile_merge_rpc_async``, and the deferred work is
spawned via ``spawn_detached`` (not starlette ``BackgroundTasks``). #57 PR-G2e-4/5:
``derive_profile_from_jd`` (async cache path), ``materialize_and_score_job``, the
``_upsert_user_job_async`` inline, and now ``resolve_current_payload`` (in
``_apply_fit_score``) + ``register_source_from_url`` (in ``from_url``) all ride the
async service client — the module holds no sync client. All are monkeypatched so
the focus stays on orchestration.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

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
from app.services.targets import from_input
from app.services.targets.fit_score import FitScoreResult
from app.services.targets.normalize_posting_title import NormalizedTitle

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
        app_active=False,
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


def _amock(value: Any):  # type: ignore[no-untyped-def]
    """Async no-op returning a fixed value (find_matching_target stand-in)."""

    async def _inner(*_a, **_k):  # type: ignore[no-untyped-def]
        return value

    return _inner


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
    """Stub all LLM-driven helpers, cost_log, and the deep sync services so the
    orchestration runs offline."""

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

    async def fake_normalize_posting_title(llm, *, title, jd_text):  # type: ignore[no-untyped-def]
        recorder.record("normalize_posting_title", title=title, jd_len=len(jd_text))
        # Echo the raw title back by default so the label-resolution tests keep
        # asserting strip/fallback behavior directly; canonicalization is
        # exercised by its own tests, which override this.
        return NormalizedTitle(label=title), _llm_result()

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
        # (payload, prose_doc_id) — the id is the E2 fit-score version marker.
        return OptimizedPayload(), "prose-doc-1"

    async def fake_cost_record_async(supabase, **kwargs):  # type: ignore[no-untyped-def]
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

    async def fake_upsert_user_job(supabase, **kwargs):  # type: ignore[no-untyped-def]
        recorder.record("user_job", **kwargs)

    monkeypatch.setattr(from_input, "normalize_manual_input", fake_normalize)
    monkeypatch.setattr(from_input, "normalize_posting_title", fake_normalize_posting_title)
    monkeypatch.setattr(from_input, "derive_profile_from_label", fake_derive_label)
    monkeypatch.setattr(from_input, "derive_profile_from_jd", fake_derive_jd)
    monkeypatch.setattr(from_input, "derive_fit_score", fake_fit_score)
    monkeypatch.setattr(from_input, "resolve_current_payload", fake_resolve)
    monkeypatch.setattr(from_input, "materialize_and_score_job", fake_materialize)
    # #57 PR-G2e-4: the ``user_jobs`` write is the async ``_upsert_user_job_async``
    # inline now (``persistence.upsert_user_job`` no longer imported here).
    monkeypatch.setattr(from_input, "_upsert_user_job_async", fake_upsert_user_job)
    monkeypatch.setattr(cost_log, "record_async", fake_cost_record_async)
    return recorder


@pytest.fixture
def stub_crud(monkeypatch: pytest.MonkeyPatch, recorder: _Recorder) -> _Recorder:
    """Stub the inline async link/update helpers; specific tests override
    create/get/add-ref/list-ref as needed."""

    async def fake_link(supabase, **kwargs):  # type: ignore[no-untyped-def]
        recorder.record("link", **kwargs)
        return _user_target(
            user_id=kwargs["user_id"],
            target_id=kwargs["target_id"],
            fit_score=kwargs.get("fit_score"),
        )

    async def fake_update(supabase, target_id, body):  # type: ignore[no-untyped-def]
        recorder.record("update", target_id=target_id, body=body)
        return _target(
            id=target_id,
            activation_status=body.activation_status or "idle",
            profile_version=body.profile_version or 1,
        )

    # Default the URL corpus path under the per-user reference-JD cap; the
    # cap-specific test overrides this. Only derive_url_target_bg reads it.
    monkeypatch.setattr(from_input, "_count_user_reference_jds", AsyncMock(return_value=0))

    monkeypatch.setattr(from_input, "_link", fake_link)
    monkeypatch.setattr(from_input, "_update", fake_update)
    return recorder


@pytest.fixture
def sched(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Capture ``spawn_detached`` scheduling for the inline-path tests.

    The deferred entrypoints are replaced with recording stubs (so we can assert
    WHICH was scheduled + with what kwargs) and ``spawn_detached`` is patched to
    swallow the coroutine so nothing actually runs. Only inline-path tests use
    this — the background-path tests exercise the real derive functions.
    """
    calls: dict[str, dict[str, Any]] = {}
    names: list[str] = []

    def _record(fn_name: str):  # type: ignore[no-untyped-def]
        def _inner(*_a: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
            calls[fn_name] = kwargs

            async def _noop() -> None:
                return None

            return _noop()

        return _inner

    for fn_name in (
        "derive_manual_target_bg",
        "derive_url_target_bg",
        "_apply_fit_score",
        "register_source_from_url",
    ):
        monkeypatch.setattr(from_input, fn_name, _record(fn_name))

    def _fake_spawn(coro: Any, *, name: str) -> Any:
        names.append(name)
        coro.close()  # never run — this is the inline-path assertion, not the bg run
        return MagicMock()

    monkeypatch.setattr(from_input, "spawn_detached", _fake_spawn)
    return SimpleNamespace(calls=calls, names=names)


# ---- from_manual: inline path -----------------------------------------------


@pytest.mark.asyncio
async def test_from_manual_matched_links_inline_defers_fit_score(
    monkeypatch: pytest.MonkeyPatch,
    stub_llm_helpers: _Recorder,
    stub_crud: _Recorder,
    sched: Any,
) -> None:
    """Matched → link inline (no fit yet), schedule the fit-score task only."""
    supabase = MagicMock()
    matched = _target(id="existing")
    monkeypatch.setattr(from_input, "find_matching_target", AsyncMock(return_value=matched))

    create_calls: list[TargetCreate] = []
    create_statuses: list[str | None] = []
    create_users: list[str] = []

    async def fake_create_and_link(_s, *, user_id, payload, activation_status=None):  # type: ignore[no-untyped-def]
        create_calls.append(payload)
        return matched, _user_target(target_id=matched.id)

    monkeypatch.setattr(from_input, "_create_and_link", fake_create_and_link)

    result = await from_input.from_manual(
        supabase,
        MagicMock(),
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
    assert "_apply_fit_score" in sched.calls
    assert "derive_manual_target_bg" not in sched.calls
    assert sched.calls["_apply_fit_score"]["target"].id == "existing"
    assert sched.calls["_apply_fit_score"]["user_id"] == "user-1"


@pytest.mark.asyncio
async def test_from_manual_new_creates_deriving_and_schedules_derivation(
    monkeypatch: pytest.MonkeyPatch,
    stub_llm_helpers: _Recorder,
    stub_crud: _Recorder,
    sched: Any,
) -> None:
    """New → create in 'deriving', link inline, schedule full derivation."""
    supabase = MagicMock()
    monkeypatch.setattr(from_input, "find_matching_target", AsyncMock(return_value=None))

    created = _target(id="new", label="Senior Frontend Engineer")
    create_calls: list[TargetCreate] = []
    create_statuses: list[str | None] = []
    create_users: list[str] = []

    async def fake_create_and_link(_s, *, user_id, payload, activation_status=None):  # type: ignore[no-untyped-def]
        create_calls.append(payload)
        create_statuses.append(activation_status)
        create_users.append(user_id)
        # The RPC sets activation_status in the SAME statement as the insert, so
        # the row it returns already carries it — a stub that returned the
        # pre-update row would model a state the DB never emits.
        target = created.model_copy(
            update={"activation_status": activation_status or created.activation_status}
        )
        return target, _user_target(target_id=target.id)

    monkeypatch.setattr(from_input, "_create_and_link", fake_create_and_link)

    result = await from_input.from_manual(
        supabase,
        MagicMock(),
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
    # Created from the canonical suggestion's LABEL ONLY, empty profile.
    assert len(create_calls) == 1
    assert create_calls[0].label == "Senior Frontend Engineer"
    # The suggestion's description is résumé-informed and `targets` is the
    # SHARED catalog — persisting it leaks one user's history to every
    # co-follower and past their own account deletion (#868). Activation
    # fills this from the label alone.
    assert create_calls[0].description is None
    assert create_calls[0].search_keywords == []
    # Status flipped to 'deriving' inline.
    # 'deriving' is requested in the SAME atomic call as the insert — there is
    # no separate status round-trip to assert on any more (#667). Asserting the
    # request (not just the returned row) keeps this honest: the stub echoes
    # what it was given, so only this line proves the caller asked for it.
    assert create_statuses == ["deriving"]
    # Linked inside the same atomic call — no separate link round-trip to
    # inspect. The membership is always is_active False there (following never
    # trips the active cap) and carries no fit score yet; the RPC enforces both,
    # and tests/integration/test_create_target_and_link.py proves it against a
    # real Postgres.
    assert create_users == ["user-1"]
    assert result.user_target.target_id == "new"
    assert result.user_target.is_active is False
    assert result.user_target.fit_score is None
    # Deferred: the manual derivation task.
    assert "derive_manual_target_bg" in sched.calls
    assert "_apply_fit_score" not in sched.calls
    assert sched.calls["derive_manual_target_bg"]["target_id"] == "new"
    assert sched.calls["derive_manual_target_bg"]["label"] == "Senior Frontend Engineer"


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
        AsyncMock(side_effect=AssertionError("should not reach matching on malformed LLM")),
    )

    with pytest.raises(HTTPException) as exc_info:
        await from_input.from_manual(
            MagicMock(),
            MagicMock(),
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

    async def boom(llm, *, label):  # type: ignore[no-untyped-def]
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

    async def hang(llm, *, label):  # type: ignore[no-untyped-def]
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
    sched: Any,
) -> None:
    """New suggestion → create in 'deriving' + link + defer derivation, and —
    unlike from_manual — NO inline normalize LLM call (label already canonical)."""
    supabase = MagicMock()
    monkeypatch.setattr(from_input, "find_matching_target", AsyncMock(return_value=None))

    created = _target(id="new", label="Senior Frontend Engineer")
    create_calls: list[TargetCreate] = []
    create_statuses: list[str | None] = []
    create_users: list[str] = []

    async def fake_create_and_link(_s, *, user_id, payload, activation_status=None):  # type: ignore[no-untyped-def]
        create_calls.append(payload)
        create_statuses.append(activation_status)
        create_users.append(user_id)
        # The RPC sets activation_status in the SAME statement as the insert, so
        # the row it returns already carries it — a stub that returned the
        # pre-update row would model a state the DB never emits.
        target = created.model_copy(
            update={"activation_status": activation_status or created.activation_status}
        )
        return target, _user_target(target_id=target.id)

    monkeypatch.setattr(from_input, "_create_and_link", fake_create_and_link)

    result = await from_input.from_suggestion(
        supabase,
        MagicMock(),
        user_id="user-1",
        label="Senior Frontend Engineer",
        description="Frontend roles.",
        payload=OptimizedPayload(),
    )

    assert result.was_matched is False
    assert result.target.activation_status == "deriving"
    # The distinguishing property: NO normalization LLM call.
    assert "normalize" not in stub_llm_helpers.names()
    # Created straight from the (already-canonical) LABEL — never the
    # résumé-informed description (#868, see the sibling test).
    assert create_calls[0].label == "Senior Frontend Engineer"
    assert create_calls[0].description is None
    # The link is created inside the atomic call now; it is always is_active
    # False there (following never trips the active cap), enforced by the RPC.
    assert create_users == ["user-1"]
    assert create_statuses == ["deriving"]
    assert "derive_manual_target_bg" in sched.calls


@pytest.mark.asyncio
async def test_from_suggestion_matched_dedups_to_existing_row(
    monkeypatch: pytest.MonkeyPatch,
    stub_llm_helpers: _Recorder,
    stub_crud: _Recorder,
    sched: Any,
) -> None:
    """A suggestion whose label matches an existing catalog row LINKS it instead
    of creating a duplicate — the server-side dedup that a stale client
    ``is_new`` can't defeat."""
    supabase = MagicMock()
    matched = _target(id="existing")
    monkeypatch.setattr(from_input, "find_matching_target", AsyncMock(return_value=matched))
    create_spy = AsyncMock()
    monkeypatch.setattr(from_input, "_create_and_link", create_spy)

    result = await from_input.from_suggestion(
        supabase,
        MagicMock(),
        user_id="user-1",
        label="Senior Frontend Engineer",
        description="Frontend roles.",
        payload=OptimizedPayload(),
    )

    assert result.was_matched is True
    assert result.target.id == "existing"
    create_spy.assert_not_called()  # NO duplicate row minted
    # Fit-score deferred (we have a profile).
    assert "_apply_fit_score" in sched.calls


@pytest.mark.asyncio
async def test_from_suggestion_still_fit_scores_when_the_inline_payload_is_none(
    monkeypatch: pytest.MonkeyPatch,
    stub_llm_helpers: _Recorder,
    stub_crud: _Recorder,
    sched: Any,
) -> None:
    """A None INLINE payload must not skip fit scoring.

    `_apply_fit_score` resolves a FRESH payload itself and uses the passed one
    only as a fallback, so the old `if payload is not None` guard tested the
    wrong thing: it skipped users who had a perfectly resolvable profile, and
    left no trace when it did. That silent skip is how a target reached prod
    with a permanently null fit_score (resweep C1).

    Scheduling the task is correct here; whether it produces a score is
    `_apply_fit_score`'s call, covered below."""
    supabase = MagicMock()
    matched = _target(id="existing")
    monkeypatch.setattr(from_input, "find_matching_target", AsyncMock(return_value=matched))

    result = await from_input.from_suggestion(
        supabase,
        MagicMock(),
        user_id="user-1",
        label="Senior Frontend Engineer",
        description=None,
        payload=None,
    )

    assert result.was_matched is True
    assert stub_crud.by_name("link")[0]["is_active"] is False
    # The fit-score task IS scheduled — the fresh resolve is what decides.
    assert any("fit-score" in n for n in sched.names), sched.names


@pytest.mark.asyncio
async def test_derive_manual_target_bg_fit_scores_from_the_fresh_payload(
    monkeypatch: pytest.MonkeyPatch,
    stub_llm_helpers: _Recorder,
    stub_crud: _Recorder,
) -> None:
    """Background derive with a None inline payload still derives the label
    profile (status→idle) AND fit-scores, because `_apply_fit_score` resolves a
    fresh payload of its own. The old guard skipped the score outright."""
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
    assert "fit_score" in names  # ...and scored against the freshly-resolved payload
    update_body: TargetUpdate = stub_crud.by_name("update")[0]["body"]
    assert update_body.activation_status == "idle"
    # ...and the score is written onto the link.
    link = stub_crud.by_name("link")[0]
    assert link["fit_score"] == 82


# ---- from_url: inline path --------------------------------------------------


@pytest.mark.asyncio
async def test_from_url_matched_links_inline_defers_derivation(
    monkeypatch: pytest.MonkeyPatch,
    stub_llm_helpers: _Recorder,
    stub_crud: _Recorder,
    sched: Any,
) -> None:
    """Matched URL → link inline, schedule corpus-building derivation."""
    supabase = MagicMock()
    matched = _target(id="existing", profile_version=4)
    monkeypatch.setattr(from_input, "find_matching_target", AsyncMock(return_value=matched))

    result = await from_input.from_url(
        supabase,
        MagicMock(),
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
    # Two bg tasks: the profile derive, then the source registration.
    assert sched.calls["derive_url_target_bg"]["target_id"] == "existing"
    assert sched.calls["derive_url_target_bg"]["jd_text"] == "x" * 200
    # Source registration is scheduled AFTER derive, carrying the pasted URL.
    assert sched.calls["register_source_from_url"]["final_url"] == "https://example.com/jobs/123"
    assert sched.names == ["derive-url-existing", "register-source-existing"]


@pytest.mark.asyncio
async def test_from_url_new_creates_deriving_and_schedules(
    monkeypatch: pytest.MonkeyPatch,
    stub_llm_helpers: _Recorder,
    stub_crud: _Recorder,
    sched: Any,
) -> None:
    """New URL → create in 'deriving', link, schedule derivation."""
    supabase = MagicMock()
    monkeypatch.setattr(from_input, "find_matching_target", AsyncMock(return_value=None))

    created = _target(id="new", label="Senior Frontend Engineer")
    create_calls: list[TargetCreate] = []
    create_statuses: list[str | None] = []
    create_users: list[str] = []

    async def fake_create_and_link(_s, *, user_id, payload, activation_status=None):  # type: ignore[no-untyped-def]
        create_calls.append(payload)
        create_statuses.append(activation_status)
        create_users.append(user_id)
        # The RPC sets activation_status in the SAME statement as the insert, so
        # the row it returns already carries it — a stub that returned the
        # pre-update row would model a state the DB never emits.
        target = created.model_copy(
            update={"activation_status": activation_status or created.activation_status}
        )
        return target, _user_target(target_id=target.id)

    monkeypatch.setattr(from_input, "_create_and_link", fake_create_and_link)

    result = await from_input.from_url(
        supabase,
        MagicMock(),
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
    assert sched.calls["derive_url_target_bg"]["target_id"] == "new"
    assert sched.calls["register_source_from_url"]["final_url"] == "https://example.com/jobs/abc"
    assert sched.names == ["derive-url-new", "register-source-new"]


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
    sched: Any,
    extracted_title: str | None,
    expected: str,
) -> None:
    """Label always derives from the posting title (there is no user override) —
    trimmed, with a blank/whitespace-only title falling back to 'Untitled Target'."""
    supabase = MagicMock()
    seen_labels: list[str] = []

    async def _match(_s, label):  # type: ignore[no-untyped-def]
        seen_labels.append(label)
        return None

    monkeypatch.setattr(from_input, "find_matching_target", _match)

    async def fake_create_and_link(_s, *, user_id, payload, activation_status=None):  # type: ignore[no-untyped-def]
        target = _target(id="new", label=expected)
        return target, _user_target(target_id=target.id)

    monkeypatch.setattr(from_input, "_create_and_link", fake_create_and_link)

    await from_input.from_url(
        supabase,
        MagicMock(),
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

    async def fake_add(_s, **kw):  # type: ignore[no-untyped-def]
        add_ref_calls.append(kw)
        return _ref_jd(target_id=kw["target_id"], jd_url=kw["jd_url"])

    monkeypatch.setattr(from_input, "_add_reference_jd", fake_add)
    monkeypatch.setattr(
        from_input, "_list_reference_jds", AsyncMock(return_value=[_ref_jd(target_id="new")])
    )
    monkeypatch.setattr(
        from_input, "_get", AsyncMock(return_value=_target(id="new", profile_version=1))
    )

    rpc_calls: list[dict[str, Any]] = []

    async def fake_rpc(_s, **kw):  # type: ignore[no-untyped-def]
        rpc_calls.append(kw)
        return ("applied", 2)

    monkeypatch.setattr(from_input, "apply_profile_merge_rpc_async", fake_rpc)

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

    monkeypatch.setattr(from_input, "_add_reference_jd", AsyncMock(return_value=_ref_jd()))
    monkeypatch.setattr(
        from_input,
        "_list_reference_jds",
        AsyncMock(return_value=[_ref_jd(target_id="existing"), _ref_jd(target_id="existing")]),
    )
    monkeypatch.setattr(
        from_input, "_get", AsyncMock(return_value=_target(id="existing", profile_version=4))
    )

    rpc_calls: list[dict[str, Any]] = []

    async def fake_rpc(_s, **kw):  # type: ignore[no-untyped-def]
        rpc_calls.append(kw)
        return ("applied", 5)

    monkeypatch.setattr(from_input, "apply_profile_merge_rpc_async", fake_rpc)

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
    monkeypatch.setattr(
        from_input, "_add_reference_jd", AsyncMock(return_value=_ref_jd(target_id="new"))
    )
    monkeypatch.setattr(
        from_input, "_list_reference_jds", AsyncMock(return_value=[_ref_jd(target_id="new")])
    )
    monkeypatch.setattr(
        from_input, "_get", AsyncMock(return_value=_target(id="new", profile_version=1))
    )
    monkeypatch.setattr(
        from_input, "apply_profile_merge_rpc_async", AsyncMock(return_value=("applied", 2))
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
        from_input,
        "_count_user_reference_jds",
        AsyncMock(return_value=settings.reference_jd_max_per_user_per_target),
    )
    add_ref_calls: list[dict[str, Any]] = []

    async def fake_add(_s, **kw):  # type: ignore[no-untyped-def]
        add_ref_calls.append(kw)
        return _ref_jd()

    monkeypatch.setattr(from_input, "_add_reference_jd", fake_add)
    rpc_calls: list[dict[str, Any]] = []

    async def fake_rpc(_s, **kw):  # type: ignore[no-untyped-def]
        rpc_calls.append(kw)
        return ("applied", 2)

    monkeypatch.setattr(from_input, "apply_profile_merge_rpc_async", fake_rpc)
    monkeypatch.setattr(
        from_input, "_get", AsyncMock(return_value=_target(id="existing", profile_version=4))
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

    async def boom(llm, *, jd_text, supabase):  # type: ignore[no-untyped-def]
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

    async def hang(llm, *, jd_text, supabase):  # type: ignore[no-untyped-def]
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


# ---- from_url label canonicalization -----------------------------------------


@pytest.mark.asyncio
async def test_from_url_matches_on_the_canonical_label_not_the_raw_title(
    monkeypatch: pytest.MonkeyPatch,
    stub_llm_helpers: _Recorder,
    stub_crud: _Recorder,
    sched: Any,
) -> None:
    """The canonical label is what dedups.

    ``crud.normalize_label`` only lowercases/trims/collapses whitespace, so
    punctuation and comma-suffixes survive into the UNIQUE key. A verbatim
    posting title therefore can never collide with the plain role some other
    user already follows — every URL create minted its own catalog row. So the
    canonicalization has to happen BEFORE ``find_matching_target``.
    """
    raw = "Senior Product Builder (Product Manager), Enterprise Readiness & Admin Platform"
    canonical = "Senior Product Manager"

    async def fake_norm(llm, *, title, jd_text):  # type: ignore[no-untyped-def]
        assert title == raw
        return NormalizedTitle(label=canonical), _llm_result()

    monkeypatch.setattr(from_input, "normalize_posting_title", fake_norm)

    seen_labels: list[str] = []

    async def _match(_s, label):  # type: ignore[no-untyped-def]
        seen_labels.append(label)
        return None

    monkeypatch.setattr(from_input, "find_matching_target", _match)

    created_labels: list[str] = []

    async def fake_create_and_link(_s, *, user_id, payload, activation_status=None):  # type: ignore[no-untyped-def]
        created_labels.append(payload.label)
        target = _target(id="new", label=payload.label)
        return target, _user_target(target_id=target.id)

    monkeypatch.setattr(from_input, "_create_and_link", fake_create_and_link)

    await from_input.from_url(
        MagicMock(),
        MagicMock(),
        user_id="user-1",
        final_url="https://example.com/jobs/x",
        extracted_title=raw,
        jd_text="x" * 200,
        company_name="Acme",
        location=None,
        salary_text=None,
        payload=OptimizedPayload(),
    )

    # Matched on the canonical form...
    assert seen_labels == [canonical]
    # ...and the catalog row was created under it, so the dedup key is canonical.
    assert created_labels == [canonical]
    assert raw not in created_labels


@pytest.mark.asyncio
async def test_from_url_links_an_existing_target_when_canonicalization_collides(
    monkeypatch: pytest.MonkeyPatch,
    stub_llm_helpers: _Recorder,
    stub_crud: _Recorder,
    sched: Any,
) -> None:
    """The payoff: two differently-worded postings now converge on one target."""

    async def fake_norm(llm, *, title, jd_text):  # type: ignore[no-untyped-def]
        return NormalizedTitle(label="Senior Product Manager"), _llm_result()

    monkeypatch.setattr(from_input, "normalize_posting_title", fake_norm)

    existing = _target(id="t-existing", label="Senior Product Manager")

    async def _match(_s, label):  # type: ignore[no-untyped-def]
        assert label == "Senior Product Manager"
        return existing

    monkeypatch.setattr(from_input, "find_matching_target", _match)

    async def _no_create(*_a, **_k):  # type: ignore[no-untyped-def]
        raise AssertionError("must link the existing target, not mint a new row")

    monkeypatch.setattr(from_input, "_create_and_link", _no_create)

    result = await from_input.from_url(
        MagicMock(),
        MagicMock(),
        user_id="user-1",
        final_url="https://example.com/jobs/y",
        extracted_title="Sr. Product Builder (PM), Growth Platform — Remote",
        jd_text="x" * 200,
        company_name="Acme",
        location=None,
        salary_text=None,
        payload=OptimizedPayload(),
    )

    assert result.was_matched is True
    assert result.target.id == "t-existing"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boom",
    [
        RuntimeError("provider 5xx"),
        ValueError("schema violation"),
        TimeoutError("provider hung"),
    ],
)
async def test_from_url_falls_back_to_the_raw_title_when_the_normalizer_fails(
    monkeypatch: pytest.MonkeyPatch,
    stub_llm_helpers: _Recorder,
    stub_crud: _Recorder,
    sched: Any,
    boom: Exception,
) -> None:
    """Cosmetics must never cost the user the whole flow.

    The normalizer improves the NAME; it is not what makes the target work. A
    provider outage, malformed JSON, or schema violation must degrade to
    today's exact behavior (the raw posting title) rather than 502 the create.
    """

    async def fake_norm(llm, *, title, jd_text):  # type: ignore[no-untyped-def]
        raise boom

    monkeypatch.setattr(from_input, "normalize_posting_title", fake_norm)

    seen_labels: list[str] = []

    async def _match(_s, label):  # type: ignore[no-untyped-def]
        seen_labels.append(label)
        return None

    monkeypatch.setattr(from_input, "find_matching_target", _match)

    async def fake_create_and_link(_s, *, user_id, payload, activation_status=None):  # type: ignore[no-untyped-def]
        target = _target(id="new", label=payload.label)
        return target, _user_target(target_id=target.id)

    monkeypatch.setattr(from_input, "_create_and_link", fake_create_and_link)

    result = await from_input.from_url(
        MagicMock(),
        MagicMock(),
        user_id="user-1",
        final_url="https://example.com/jobs/x",
        extracted_title="  Staff Backend Engineer  ",
        jd_text="x" * 200,
        company_name="Acme",
        location=None,
        salary_text=None,
        payload=OptimizedPayload(),
    )

    # Degraded, not failed — and the raw title is still trimmed as before.
    assert seen_labels == ["Staff Backend Engineer"]
    assert result.was_matched is False


@pytest.mark.asyncio
async def test_from_url_falls_back_when_the_normalizer_returns_a_blank_label(
    monkeypatch: pytest.MonkeyPatch,
    stub_llm_helpers: _Recorder,
    stub_crud: _Recorder,
    sched: Any,
) -> None:
    """A whitespace label would render a blank card and a useless dedup key."""

    async def fake_norm(llm, *, title, jd_text):  # type: ignore[no-untyped-def]
        return NormalizedTitle(label="   "), _llm_result()

    monkeypatch.setattr(from_input, "normalize_posting_title", fake_norm)

    seen_labels: list[str] = []

    async def _match(_s, label):  # type: ignore[no-untyped-def]
        seen_labels.append(label)
        return None

    monkeypatch.setattr(from_input, "find_matching_target", _match)

    async def fake_create_and_link(_s, *, user_id, payload, activation_status=None):  # type: ignore[no-untyped-def]
        target = _target(id="new", label=payload.label)
        return target, _user_target(target_id=target.id)

    monkeypatch.setattr(from_input, "_create_and_link", fake_create_and_link)

    await from_input.from_url(
        MagicMock(),
        MagicMock(),
        user_id="user-1",
        final_url="https://example.com/jobs/x",
        extracted_title="Data Engineer",
        jd_text="x" * 200,
        company_name="Acme",
        location=None,
        salary_text=None,
        payload=OptimizedPayload(),
    )

    assert seen_labels == ["Data Engineer"]


@pytest.mark.asyncio
async def test_from_url_still_falls_back_to_untitled_for_a_missing_title(
    monkeypatch: pytest.MonkeyPatch,
    stub_llm_helpers: _Recorder,
    stub_crud: _Recorder,
    sched: Any,
) -> None:
    """The pre-existing no-title contract survives canonicalization."""
    seen_titles: list[str] = []

    async def fake_norm(llm, *, title, jd_text):  # type: ignore[no-untyped-def]
        seen_titles.append(title)
        raise RuntimeError("normalizer unavailable")

    monkeypatch.setattr(from_input, "normalize_posting_title", fake_norm)

    seen_labels: list[str] = []

    async def _match(_s, label):  # type: ignore[no-untyped-def]
        seen_labels.append(label)
        return None

    monkeypatch.setattr(from_input, "find_matching_target", _match)

    async def fake_create_and_link(_s, *, user_id, payload, activation_status=None):  # type: ignore[no-untyped-def]
        target = _target(id="new", label=payload.label)
        return target, _user_target(target_id=target.id)

    monkeypatch.setattr(from_input, "_create_and_link", fake_create_and_link)

    await from_input.from_url(
        MagicMock(),
        MagicMock(),
        user_id="user-1",
        final_url="https://example.com/jobs/x",
        extracted_title=None,
        jd_text="x" * 200,
        company_name="Acme",
        location=None,
        salary_text=None,
        payload=OptimizedPayload(),
    )

    assert seen_titles == ["Untitled Target"]
    assert seen_labels == ["Untitled Target"]


@pytest.mark.asyncio
async def test_from_url_records_the_canonicalization_cost(
    monkeypatch: pytest.MonkeyPatch,
    stub_llm_helpers: _Recorder,
    stub_crud: _Recorder,
    sched: Any,
) -> None:
    """The inline canonicalization is billed, so it must reach the ledger.

    An unlogged LLM call makes the cost ledger under-report real spend, which
    loosens ``enforce_llm_budget`` (it reads that ledger) and makes the usage
    card lie. The sibling manual path has always recorded its normalize call.
    """
    monkeypatch.setattr(from_input, "find_matching_target", _amock(None))

    async def fake_create_and_link(_s, *, user_id, payload, activation_status=None):  # type: ignore[no-untyped-def]
        target = _target(id="new", label=payload.label)
        return target, _user_target(target_id=target.id)

    monkeypatch.setattr(from_input, "_create_and_link", fake_create_and_link)

    await from_input.from_url(
        MagicMock(),
        MagicMock(),
        user_id="user-1",
        final_url="https://example.com/jobs/x",
        extracted_title="Staff Backend Engineer",
        jd_text="x" * 200,
        company_name="Acme",
        location=None,
        salary_text=None,
        payload=OptimizedPayload(),
    )

    logged = [
        kw
        for kw in stub_llm_helpers.by_name("cost_log")
        if kw.get("purpose") == "target.normalize_posting_title"
    ]
    assert logged, (
        "the canonicalization LLM call was not recorded in the cost ledger; "
        f"purposes seen: {[kw.get('purpose') for kw in stub_llm_helpers.by_name('cost_log')]}"
    )
    assert logged[0]["user_id"] == "user-1"


@pytest.mark.asyncio
async def test_from_url_survives_a_cost_ledger_failure(
    monkeypatch: pytest.MonkeyPatch,
    stub_llm_helpers: _Recorder,
    stub_crud: _Recorder,
    sched: Any,
) -> None:
    """A ledger write must never cost the user the create.

    By the time we record, the canonical label is already in hand — failing the
    whole flow over bookkeeping would trade a real user outcome for an
    accounting row.
    """

    async def boom_cost(*_a, **_k):  # type: ignore[no-untyped-def]
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(from_input.cost_log, "record_async", boom_cost)
    monkeypatch.setattr(from_input, "find_matching_target", _amock(None))

    async def fake_create_and_link(_s, *, user_id, payload, activation_status=None):  # type: ignore[no-untyped-def]
        target = _target(id="new", label=payload.label)
        return target, _user_target(target_id=target.id)

    monkeypatch.setattr(from_input, "_create_and_link", fake_create_and_link)

    result = await from_input.from_url(
        MagicMock(),
        MagicMock(),
        user_id="user-1",
        final_url="https://example.com/jobs/x",
        extracted_title="Staff Backend Engineer",
        jd_text="x" * 200,
        company_name="Acme",
        location=None,
        salary_text=None,
        payload=OptimizedPayload(),
    )
    assert result.was_matched is False


@pytest.mark.asyncio
async def test_apply_fit_score_logs_when_there_is_no_payload_at_all(
    monkeypatch: pytest.MonkeyPatch,
    stub_llm_helpers: _Recorder,
    stub_crud: _Recorder,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Neither a fresh nor an inline payload: skip, but SAY SO.

    This is the state that produced a target stuck with a null fit_score for a
    full day. Skipping is fine — `fit_refresh` heals the link on a later view
    once the user has a profile — but skipping in silence left nothing to
    diagnose it by. The warning is the whole fix for this half.
    """

    async def no_payload(supabase, llm, *, cost_supabase, user_id):  # type: ignore[no-untyped-def]
        return None, None

    monkeypatch.setattr(from_input, "resolve_current_payload", no_payload)

    with caplog.at_level("WARNING"):
        await from_input._apply_fit_score(
            MagicMock(),
            MagicMock(),
            user_id="user-1",
            target=_target(id="t-unscored"),
            payload=None,
        )

    assert "fit_score" not in stub_llm_helpers.names()  # no LLM spend
    assert stub_crud.by_name("link") == []  # nothing written
    messages = [r.getMessage() for r in caplog.records]
    assert any("t-unscored" in m for m in messages), messages
