"""Tests for GET /targets/{target_id}/user-target.

The FE needs the per-(user, target) row when rendering a target settings
page. The pre-existing endpoints either return the shared JobTarget only
(GET /targets/{id}) or the whole list (GET /targets/mine). This endpoint
returns just the user's row for a given target, paired with the shared
target data.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.dependencies import (
    get_async_service_supabase,
    get_current_user_id,
    get_current_user_id_optional,
    get_llm_client,
    get_supabase,
    verify_api_key_or_jwt,
)
from app.main import app
from app.models.targets import (
    JobTarget,
    JobTargetSummary,
    ScoringProfile,
    UserTarget,
    UserTargetWithSummary,
)
from app.services.targets import fit_refresh


def _job_target() -> JobTarget:
    now = datetime.now(UTC)
    return JobTarget(
        id="target-1",
        label="Director of CX Operations",
        scoring_profile=ScoringProfile(),
        app_active=True,
        created_at=now,
        updated_at=now,
    )


def _user_target() -> UserTarget:
    now = datetime.now(UTC)
    return UserTarget(
        id="ut-1",
        user_id="user-1",
        target_id="target-1",
        is_active=True,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def client() -> TestClient:
    app.dependency_overrides[get_supabase] = lambda: MagicMock()
    app.dependency_overrides[get_async_service_supabase] = lambda: MagicMock()
    app.dependency_overrides[get_current_user_id] = lambda: "user-1"
    # GET /targets/{id} now resolves the caller via the optional dep and
    # ownership-checks it (#29 round 3 / M3), so the fixture must supply it.
    app.dependency_overrides[get_current_user_id_optional] = lambda: "user-1"
    app.dependency_overrides[verify_api_key_or_jwt] = lambda: "user-1"
    app.dependency_overrides[get_llm_client] = lambda: MagicMock()
    yield TestClient(app)
    app.dependency_overrides.clear()


def _summary_item() -> UserTargetWithSummary:
    now = datetime.now(UTC)
    return UserTargetWithSummary(
        user_target=_user_target(),
        target=JobTargetSummary(
            id="target-1", label="X", app_active=True, created_at=now, updated_at=now
        ),
    )


def test_mine_schedules_lazy_refresh_when_user_has_targets(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /targets/mine returns the cached summaries immediately AND schedules a
    SINGLE background refresh task (E2) — the whole staleness scan runs in the
    background, off the response path.

    #57 PR-G2e-5: the refresh rides the async pool now, so /mine spawns it DETACHED
    (``spawn_detached``, not a starlette ``BackgroundTask``). Patch spawn_detached to
    capture the scheduled coroutine synchronously — the recording stub sets the
    scheduled marker when the router builds the coro to hand off."""
    from app.routers import targets as router_mod

    monkeypatch.setattr(
        router_mod,
        "_list_user_targets_with_summary_async",
        AsyncMock(return_value=[_summary_item()]),
    )
    scheduled: dict[str, object] = {}

    def spy_refresh(_s, _llm, *, user_id):  # type: ignore[no-untyped-def]
        scheduled["user_id"] = user_id

        async def _noop() -> None:
            return None

        return _noop()

    monkeypatch.setattr(fit_refresh, "refresh_stale_for_user", spy_refresh)
    monkeypatch.setattr(router_mod, "spawn_detached", lambda coro, *, name: coro.close())

    resp = client.get("/targets/mine")

    assert resp.status_code == 200
    assert scheduled == {"user_id": "user-1"}


def test_mine_skips_refresh_when_no_targets(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No linked targets → nothing to refresh → no background task scheduled at
    all (the staleness/no-profile short-circuits now live inside the task)."""
    from app.routers import targets as router_mod

    monkeypatch.setattr(
        router_mod, "_list_user_targets_with_summary_async", AsyncMock(return_value=[])
    )
    called = {"n": 0}

    async def spy_refresh(_s, _llm, *, user_id):  # type: ignore[no-untyped-def]
        called["n"] += 1

    monkeypatch.setattr(fit_refresh, "refresh_stale_for_user", spy_refresh)

    resp = client.get("/targets/mine")

    assert resp.status_code == 200
    assert called["n"] == 0


def test_returns_user_target_with_target_data(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.routers import targets as router_mod

    monkeypatch.setattr(router_mod, "_get_user_target", AsyncMock(return_value=_user_target()))
    monkeypatch.setattr(router_mod, "_target_get", AsyncMock(return_value=_job_target()))

    resp = client.get("/targets/target-1/user-target")

    assert resp.status_code == 200
    body = resp.json()
    assert body["user_target"]["user_id"] == "user-1"
    assert body["user_target"]["target_id"] == "target-1"
    assert body["target"]["id"] == "target-1"
    assert body["target"]["label"] == "Director of CX Operations"


def test_404_when_no_user_target_row(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The user might query a target they've never linked to. 404."""
    from app.routers import targets as router_mod

    monkeypatch.setattr(router_mod, "_get_user_target", AsyncMock(return_value=None))

    resp = client.get("/targets/target-1/user-target")

    assert resp.status_code == 404
    assert "user_targets" in resp.json()["detail"]


def test_404_when_user_target_exists_but_target_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Data-integrity edge: junction row exists but shared target was
    deleted. Surface as 404 rather than a 500."""
    from app.routers import targets as router_mod

    monkeypatch.setattr(router_mod, "_get_user_target", AsyncMock(return_value=_user_target()))
    monkeypatch.setattr(router_mod, "_target_get", AsyncMock(return_value=None))

    resp = client.get("/targets/target-1/user-target")

    assert resp.status_code == 404
    assert "Target not found" in resp.json()["detail"]


def test_does_not_collide_with_get_target_route(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /targets/{target_id} is declared above /targets/{target_id}/user-target
    in the router. Make sure FastAPI dispatches by full path, not greedy
    match — a 'user-target' segment should never get swallowed by the
    {target_id} placeholder."""
    from app.routers import targets as router_mod

    # Stub both endpoints; the test passes as long as the right handler is hit.
    monkeypatch.setattr(router_mod, "_target_get", AsyncMock(return_value=_job_target()))
    monkeypatch.setattr(router_mod, "_get_user_target", AsyncMock(return_value=_user_target()))
    # GET /targets/{id} ownership-checks the caller (#29 round 3 / M3); the
    # fixture user owns target-1.
    monkeypatch.setattr(router_mod, "_user_target_ids", AsyncMock(return_value={"target-1"}))

    plain = client.get("/targets/target-1")
    assert plain.status_code == 200
    assert "user_target" not in plain.json()  # bare JobTarget shape

    paired = client.get("/targets/target-1/user-target")
    assert paired.status_code == 200
    assert "user_target" in paired.json()
    assert "target" in paired.json()
