"""GET /targets/active must be scoped to the caller (tenant isolation).

`crud.get_active` returns the global union of every user's active
targets (the shared `targets.is_active` flag is OR'd across users). The
route must NOT hand that to a JWT caller — it would leak every other
user's roles + scoring profiles. JWT → the caller's own active targets;
api-key/operator → the instance-wide view.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.dependencies import (
    get_current_user_id_optional,
    get_supabase,
    verify_api_key_or_jwt,
)
from app.main import app
from app.models.targets import JobTarget, ScoringProfile
from app.routers import targets as router_mod


def _target(tid: str, label: str) -> JobTarget:
    now = datetime.now(UTC)
    return JobTarget(
        id=tid,
        label=label,
        scoring_profile=ScoringProfile(),
        is_active=True,
        created_at=now,
        updated_at=now,
    )


def test_jwt_caller_gets_only_their_active_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    global_spy = MagicMock(
        return_value=[_target("t-other", "Someone Else's Role")]
    )
    per_user_spy = MagicMock(return_value=[_target("t-mine", "My Role")])
    monkeypatch.setattr(router_mod.crud, "get_active", global_spy)
    monkeypatch.setattr(router_mod.crud, "get_active_for_user", per_user_spy)

    app.dependency_overrides[get_supabase] = lambda: MagicMock()
    app.dependency_overrides[verify_api_key_or_jwt] = lambda: "jwt"
    app.dependency_overrides[get_current_user_id_optional] = lambda: "user-1"
    try:
        resp = TestClient(app).get("/targets/active")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    labels = [t["label"] for t in resp.json()["targets"]]
    assert labels == ["My Role"]
    per_user_spy.assert_called_once()
    # The global, cross-user query must never run for a JWT caller.
    global_spy.assert_not_called()


def test_api_key_caller_gets_instance_wide_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    global_spy = MagicMock(return_value=[_target("t-1", "Role A"), _target("t-2", "Role B")])
    per_user_spy = MagicMock()
    monkeypatch.setattr(router_mod.crud, "get_active", global_spy)
    monkeypatch.setattr(router_mod.crud, "get_active_for_user", per_user_spy)

    app.dependency_overrides[get_supabase] = lambda: MagicMock()
    app.dependency_overrides[verify_api_key_or_jwt] = lambda: "api-key"
    app.dependency_overrides[get_current_user_id_optional] = lambda: None
    try:
        resp = TestClient(app).get("/targets/active")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert len(resp.json()["targets"]) == 2
    global_spy.assert_called_once()
    per_user_spy.assert_not_called()
