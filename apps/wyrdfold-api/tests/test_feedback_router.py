"""Router wiring tests for feedback reads (#79 Phase 2 — job_feedback slice).

The headline assertion: GET /targets/{id}/feedback now reads through the
JWT-bound user client (``get_async_user_supabase``), not the service-role
client, so Postgres RLS is the backstop. The cross-tenant RLS proof itself
lives in ``tests/integration/test_rls_feedback.py`` (needs a live stack).

Since #57 PR-G2d-a the handlers are ``async def`` on the pooled async clients:
the user-path reads/writes run on ``get_async_user_supabase``; the shared-catalog
learner + staged-patch writes stay sync (service-role, #191 RPC) and are driven
off the loop by the ``*_off_loop`` service seams (which acquire the pooled sync
service client themselves), so the router body holds no sync client. The
post-feedback learner is scheduled as a detached task, never a BackgroundTask.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.dependencies import (
    get_async_service_supabase,
    get_async_user_supabase,
    get_current_user_id,
)
from app.main import app
from app.models.feedback import FeedbackRow

_TEST_USER_ID = "00000000-0000-0000-0000-000000000001"
_TARGET_ID = "11111111-1111-1111-1111-111111111111"


async def _atrue(*_a: object, **_k: object) -> bool:
    """Async stand-in for the now-``async`` ownership prechecks."""
    return True


@pytest.fixture
def overrides():
    yield
    app.dependency_overrides.clear()


def test_list_feedback_reads_via_user_client(
    overrides: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_sb = MagicMock(name="user_client")
    service_sb = MagicMock(name="service_client")
    captured: dict[str, object] = {}

    async def fake_list_for_target(supabase, *, user_id, target_id, limit, offset):
        captured["client"] = supabase
        return ([], 0)

    # Pass the ownership gate without touching the DB, and capture which
    # client the read path receives.
    monkeypatch.setattr("app.routers.feedback._target_exists_for_user", _atrue)
    monkeypatch.setattr("app.routers.feedback.list_for_target", fake_list_for_target)
    app.dependency_overrides[get_async_service_supabase] = lambda: service_sb
    app.dependency_overrides[get_async_user_supabase] = lambda: user_sb
    app.dependency_overrides[get_current_user_id] = lambda: _TEST_USER_ID

    r = TestClient(app).get(f"/targets/{_TARGET_ID}/feedback")

    assert r.status_code == 200
    assert r.json() == {"rows": [], "total": 0}
    # The slice: the read must use the RLS-bound user client, not service-role.
    assert captured["client"] is user_sb
    assert captured["client"] is not service_sb


def _make_row() -> FeedbackRow:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return FeedbackRow(
        id="f1",
        user_id=_TEST_USER_ID,
        job_posting_id="job-1",
        target_id=_TARGET_ID,
        signal="irrelevant",
        reason="too senior",
        created_at=now,
        updated_at=now,
    )


def test_create_feedback_upsert_via_user_client_queues_detached_learner(
    overrides: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#79 R1 + #57 PR-G2d-a: the job_feedback upsert runs through the RLS-bound
    user client, and an ``irrelevant`` signal schedules the background learner as
    a DETACHED task (never a starlette BackgroundTask — uvloop pool deadlock). The
    learner's service-role client is acquired inside the off-loop driver now, so
    this asserts the router queued it for the right (user, target)."""
    user_sb = MagicMock(name="user_client")
    captured: dict[str, object] = {}

    monkeypatch.setattr("app.routers.feedback._job_exists", _atrue)
    monkeypatch.setattr("app.routers.feedback._target_exists_for_user", _atrue)

    async def fake_upsert(supabase, **kwargs):
        captured["upsert_client"] = supabase
        return _make_row()

    async def _learner_coro() -> None:
        return None

    def fake_driver(*, user_id, target_id):
        captured["driver_args"] = (user_id, target_id)
        return _learner_coro()

    def fake_spawn(coro, *, name):
        captured["spawn_name"] = name
        coro.close()  # discard without scheduling — avoids "coroutine never awaited"
        return MagicMock()

    monkeypatch.setattr("app.routers.feedback.upsert_feedback", fake_upsert)
    monkeypatch.setattr("app.routers.feedback.run_learner_and_rescore_off_loop", fake_driver)
    monkeypatch.setattr("app.routers.feedback.spawn_detached", fake_spawn)
    app.dependency_overrides[get_async_user_supabase] = lambda: user_sb
    app.dependency_overrides[get_current_user_id] = lambda: _TEST_USER_ID

    r = TestClient(app).post(
        "/jobs/job-1/feedback",
        json={"signal": "irrelevant", "reason": "too senior", "target_id": _TARGET_ID},
    )

    assert r.status_code == 200
    assert r.json()["queued_learn_run"] is True
    assert captured["upsert_client"] is user_sb
    # The learner was scheduled off-loop for this (user, target), via spawn_detached.
    assert captured["driver_args"] == (_TEST_USER_ID, _TARGET_ID)
    assert captured["spawn_name"] == f"feedback-learner:{_TARGET_ID}"


def test_remove_feedback_deletes_via_user_client(
    overrides: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_sb = MagicMock(name="user_client")
    service_sb = MagicMock(name="service_client")
    captured: dict[str, object] = {}

    async def fake_delete(supabase, **kwargs):
        captured["client"] = supabase
        return True

    monkeypatch.setattr("app.routers.feedback.delete_feedback", fake_delete)
    app.dependency_overrides[get_async_service_supabase] = lambda: service_sb
    app.dependency_overrides[get_async_user_supabase] = lambda: user_sb
    app.dependency_overrides[get_current_user_id] = lambda: _TEST_USER_ID

    r = TestClient(app).delete(f"/jobs/job-1/feedback?target_id={_TARGET_ID}")

    assert r.status_code == 204
    assert captured["client"] is user_sb
    assert captured["client"] is not service_sb


def test_learning_log_reads_via_user_client(
    overrides: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_sb = MagicMock(name="user_client")
    # The endpoint reads via ``_learning_log_rows`` on the user client; make the
    # async chain return no rows so the response validates, and assert which
    # client was used.
    chain = (
        user_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.order.return_value.limit.return_value
    )
    chain.execute = AsyncMock(return_value=SimpleNamespace(data=[]))
    monkeypatch.setattr("app.routers.feedback._target_exists_for_user", _atrue)
    app.dependency_overrides[get_async_user_supabase] = lambda: user_sb
    app.dependency_overrides[get_current_user_id] = lambda: _TEST_USER_ID

    r = TestClient(app).get(f"/targets/{_TARGET_ID}/learning-log")

    assert r.status_code == 200
    assert r.json() == []
    user_sb.table.assert_called_once_with("target_learning_log")


# ---- #191 hardening wiring: rate limits + staged-apply conflict ------------


def test_staged_apply_conflict_maps_to_409(
    overrides: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A version_conflict from the #191 RPC surfaces as 409, not a 500 —
    and not a silent 404."""
    from app.services.llm_learner import StagedPatchConflictError

    monkeypatch.setattr("app.routers.feedback._target_exists_for_user", _atrue)

    async def _raise(*_a: object, **_k: object) -> None:
        raise StagedPatchConflictError("profile moved")

    # The apply runs through the async off-loop driver now; patch it to raise.
    monkeypatch.setattr("app.routers.feedback.apply_staged_patch_off_loop", _raise)
    app.dependency_overrides[get_async_service_supabase] = lambda: MagicMock()
    app.dependency_overrides[get_current_user_id] = lambda: _TEST_USER_ID

    r = TestClient(app).post(f"/targets/{_TARGET_ID}/learn/run-1/apply")

    assert r.status_code == 409
    assert "changed while applying" in r.json()["detail"]


def test_learner_trigger_rate_limited_at_10_per_minute(
    overrides: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#191: the learner mutates the SHARED target profile — the trigger
    endpoint must throttle per user. Limiter is globally disabled in tests
    (conftest RATE_LIMIT_ENABLED=false); flip it on for this case."""
    from app.rate_limit import limiter

    monkeypatch.setattr("app.routers.feedback._target_exists_for_user", _atrue)

    # The learner runs through the async off-loop driver now; stub it to a no-op.
    async def _none(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr("app.routers.feedback.run_learner_off_loop", _none)
    app.dependency_overrides[get_async_service_supabase] = lambda: MagicMock()
    app.dependency_overrides[get_current_user_id] = lambda: _TEST_USER_ID

    client = TestClient(app)
    limiter.enabled = True
    try:
        statuses = [client.post(f"/targets/{_TARGET_ID}/learn").status_code for _ in range(11)]
    finally:
        limiter.enabled = False
        limiter.reset()

    assert statuses[:10] == [200] * 10
    assert statuses[10] == 429
