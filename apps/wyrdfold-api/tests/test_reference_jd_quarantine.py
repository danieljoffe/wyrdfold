"""Router wiring for #191 slice 1b — the add_reference_jd cap/quarantine
branch and the RPC-gated merge write.

The live-stack proofs (RPC guards, staged-merge lifecycle) are in
``tests/integration/test_merge_quarantine.py``; these tests pin the ROUTER
logic with everything else mocked: a capped projection quarantines the
contribution (suppressed + staged log row, profile untouched), an uncapped
one writes through the merge RPC, and an unresolvable version conflict
surfaces as 409.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.dependencies import (
    enforce_llm_budget,
    get_async_service_supabase,
    get_current_user_id_optional,
    get_llm_client,
    verify_api_key_or_jwt,
)
from app.main import app
from app.models.learning import RescoreProjection
from app.models.llm import LLMResult, LLMUsage
from app.models.targets import (
    DerivedTarget,
    JobTarget,
    ScoringProfile,
    TargetReferenceJD,
)

_USER = "00000000-0000-0000-0000-000000000001"
_TARGET = "11111111-1111-1111-1111-111111111111"
_JD_TEXT = "golang engineer with distributed systems experience " * 3


def _target() -> JobTarget:
    now = datetime.now(UTC)
    return JobTarget(
        id=_TARGET,
        label="Role",
        scoring_profile=ScoringProfile(),
        app_active=True,
        profile_version=3,
        created_at=now,
        updated_at=now,
    )


def _derived() -> DerivedTarget:
    return DerivedTarget(
        scoring_profile=ScoringProfile(),
        search_keywords=["golang engineer"],
        example_promising_titles=["Golang Engineer"],
        example_unpromising_titles=["Sales Rep"],
    )


def _capped_projection() -> RescoreProjection:
    return RescoreProjection(
        jobs_considered=20,
        jobs_moved=12,
        moved_fraction=0.6,
        max_abs_delta=40,
        move_threshold=20,
        max_moved_fraction=0.30,
        capped=True,
    )


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Mock everything around the merge/cap branch; yield the table mocks.

    #57 PR-G2b: the handler runs on the async service client, so the crud reads
    are the router-inline async helpers (AsyncMock) and the quarantine writes go
    through ``_suppress_reference_jd_async`` / ``_insert_learning_log_row_async``
    on the async client (``.execute`` awaited → AsyncMock at the chain tail).
    """
    ref_mock = MagicMock(name="reference_jds")
    ref_mock.update.return_value.eq.return_value.execute = AsyncMock()
    log_mock = MagicMock(name="target_learning_log")
    log_mock.insert.return_value.execute = AsyncMock()
    sb = MagicMock(name="supabase")
    sb.table.side_effect = lambda name: {
        "reference_jds": ref_mock,
        "target_learning_log": log_mock,
    }[name]

    async def _owns(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr("app.routers.targets._require_user_owns_target_async", _owns)
    monkeypatch.setattr("app.routers.targets._target_get", AsyncMock(return_value=_target()))
    monkeypatch.setattr(
        "app.routers.targets._count_user_reference_jds_async", AsyncMock(return_value=0)
    )
    monkeypatch.setattr(
        "app.routers.targets._add_reference_jd_async",
        AsyncMock(
            return_value=TargetReferenceJD(
                id="rj-1",
                target_id=_TARGET,
                jd_text=_JD_TEXT,
                extracted_profile=ScoringProfile(),
                created_at=datetime.now(UTC),
            )
        ),
    )
    monkeypatch.setattr("app.routers.targets._list_reference_jds_async", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        "app.routers.targets.merge_reference_jds",
        lambda *_a: ScoringProfile(),
    )

    async def _derive(*_a: Any, **_k: Any) -> Any:
        return _derived(), LLMResult(
            content="{}",
            model="claude-sonnet-4-6",
            usage=LLMUsage(input_tokens=1, output_tokens=1),
            cost_usd=0.0,
            latency_ms=1,
        )

    monkeypatch.setattr("app.routers.targets.derive_profile_from_jd", _derive)
    monkeypatch.setattr("app.routers.targets.cost_log.record_async", AsyncMock())
    # #57 PR-G2e-4: the handler runs derive (async cache path) + the projection
    # (``project_profile_impact``) on the injected async service client, so
    # ``sb`` is supplied via the ``get_async_service_supabase`` override below.

    app.dependency_overrides[verify_api_key_or_jwt] = lambda: None
    app.dependency_overrides[get_async_service_supabase] = lambda: sb
    app.dependency_overrides[get_llm_client] = lambda: object()
    app.dependency_overrides[get_current_user_id_optional] = lambda: _USER
    app.dependency_overrides[enforce_llm_budget] = lambda: None
    yield {"ref": ref_mock, "log": log_mock}
    app.dependency_overrides.clear()


def _post(client_body: dict[str, Any] | None = None) -> Any:
    return TestClient(app).post(
        f"/targets/{_TARGET}/reference-jds",
        json=client_body or {"jd_text": _JD_TEXT},
    )


def test_capped_contribution_is_quarantined_not_applied(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    project = AsyncMock(name="project", return_value=_capped_projection())
    monkeypatch.setattr("app.routers.targets.project_profile_impact", project)
    rpc = AsyncMock(name="merge_rpc")
    monkeypatch.setattr("app.routers.targets.apply_profile_merge_rpc_async", rpc)

    r = _post()

    # The projection evaluates the full "after" state: old keywords on the
    # before side, the new JD's derived keywords on the after side.
    assert project.call_args.args[4] == []  # current target keywords
    assert project.call_args.args[5] == ["golang engineer"]  # derived

    assert r.status_code == 201
    # Profile untouched — the response is the target as it stood.
    assert r.json()["profile_version"] == 3
    rpc.assert_not_called()
    # The contribution was suppressed-at-birth…
    wired["ref"].update.assert_called_once_with({"suppressed": True})
    # …and a kind='merge' staged row carries what the apply needs.
    wired["log"].insert.assert_called_once()
    row = wired["log"].insert.call_args[0][0]
    assert row["kind"] == "merge"
    assert row["status"] == "staged"
    assert row["merge_payload"]["ref_jd_id"] == "rj-1"
    assert row["merge_payload"]["search_keywords"] == ["golang engineer"]
    assert "staged merge" in row["rationale"]


def test_uncapped_contribution_writes_through_merge_rpc(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.routers.targets.project_profile_impact", AsyncMock(return_value=None))
    rpc = AsyncMock(name="merge_rpc", return_value=("applied", 4))
    monkeypatch.setattr("app.routers.targets.apply_profile_merge_rpc_async", rpc)

    r = _post()

    assert r.status_code == 201
    rpc.assert_called_once()
    kwargs = rpc.call_args.kwargs
    assert kwargs["user_id"] == _USER
    assert kwargs["expected_version"] == 3
    assert kwargs["search_keywords"] == ["golang engineer"]
    # No quarantine artifacts.
    wired["ref"].update.assert_not_called()
    wired["log"].insert.assert_not_called()


def test_unresolvable_version_conflict_is_409(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.routers.targets.project_profile_impact", AsyncMock(return_value=None))
    rpc = AsyncMock(name="merge_rpc", return_value=("version_conflict", 9))
    monkeypatch.setattr("app.routers.targets.apply_profile_merge_rpc_async", rpc)

    r = _post()

    assert r.status_code == 409
    # Retried once (two attempts) before giving up.
    assert rpc.call_count == 2
    wired["log"].insert.assert_not_called()
