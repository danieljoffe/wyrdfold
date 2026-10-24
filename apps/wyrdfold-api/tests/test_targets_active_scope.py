"""GET /targets/active must be scoped to the caller (tenant isolation).

`crud.get_active` returns the global union of every user's active
targets (the shared `targets.is_active` flag is OR'd across users). The
route must NOT hand that to a JWT caller — it would leak every other
user's roles + scoring profiles. The caller gets their own active targets.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.dependencies import (
    get_async_service_supabase,
    get_current_user_id,
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
        app_active=True,
        created_at=now,
        updated_at=now,
    )


def test_jwt_caller_gets_only_their_active_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #57 slice 3: the handler inlines the crud reads as async helpers. There
    # is no instance-wide read in the router any more, so the per-user one is
    # the only source.
    per_user_spy = AsyncMock(return_value=[_target("t-mine", "My Role")])
    monkeypatch.setattr(router_mod, "_active_targets_for_user", per_user_spy)

    app.dependency_overrides[get_async_service_supabase] = lambda: MagicMock()
    app.dependency_overrides[get_async_service_supabase] = lambda: MagicMock()
    app.dependency_overrides[verify_api_key_or_jwt] = lambda: "jwt"
    app.dependency_overrides[get_current_user_id] = lambda: "user-1"
    try:
        resp = TestClient(app).get("/targets/active")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    labels = [t["label"] for t in resp.json()["targets"]]
    assert labels == ["My Role"]
    per_user_spy.assert_called_once()
    assert per_user_spy.call_args.args[1] == "user-1"
