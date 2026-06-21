"""Router wiring tests for feedback reads (#79 Phase 2 — job_feedback slice).

The headline assertion: GET /targets/{id}/feedback now reads through the
JWT-bound user client (``get_user_supabase``), not the service-role client,
so Postgres RLS is the backstop. The cross-tenant RLS proof itself lives in
``tests/integration/test_rls_feedback.py`` (needs a live stack).
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.dependencies import get_current_user_id, get_supabase, get_user_supabase
from app.main import app
from app.models.feedback import FeedbackRow

_TEST_USER_ID = "00000000-0000-0000-0000-000000000001"
_TARGET_ID = "11111111-1111-1111-1111-111111111111"


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

    def fake_list_for_target(supabase, *, user_id, target_id, limit, offset):
        captured["client"] = supabase
        return ([], 0)

    # Pass the ownership gate without touching the DB, and capture which
    # client the read path receives.
    monkeypatch.setattr(
        "app.routers.feedback._target_exists_for_user", lambda *a, **k: True
    )
    monkeypatch.setattr(
        "app.routers.feedback.list_for_target", fake_list_for_target
    )
    app.dependency_overrides[get_supabase] = lambda: service_sb
    app.dependency_overrides[get_user_supabase] = lambda: user_sb
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


def test_create_feedback_upsert_via_user_client_learner_via_service(
    overrides: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#79 R1: the job_feedback upsert runs through the RLS-bound user client,
    but the background learner (which writes the shared `targets` catalog)
    stays on the service-role client."""
    user_sb = MagicMock(name="user_client")
    service_sb = MagicMock(name="service_client")
    captured: dict[str, object] = {}

    monkeypatch.setattr("app.routers.feedback._job_exists", lambda *a, **k: True)
    monkeypatch.setattr(
        "app.routers.feedback._target_exists_for_user", lambda *a, **k: True
    )

    def fake_upsert(supabase, **kwargs):
        captured["upsert_client"] = supabase
        return _make_row()

    def fake_safe_run(supabase, user_id, target_id):
        captured["learner_client"] = supabase

    monkeypatch.setattr("app.routers.feedback.upsert_feedback", fake_upsert)
    monkeypatch.setattr("app.routers.feedback._safe_run_learner", fake_safe_run)
    app.dependency_overrides[get_supabase] = lambda: service_sb
    app.dependency_overrides[get_user_supabase] = lambda: user_sb
    app.dependency_overrides[get_current_user_id] = lambda: _TEST_USER_ID

    r = TestClient(app).post(
        "/jobs/job-1/feedback",
        json={"signal": "irrelevant", "reason": "too senior", "target_id": _TARGET_ID},
    )

    assert r.status_code == 200
    assert r.json()["queued_learn_run"] is True
    assert captured["upsert_client"] is user_sb
    assert captured["learner_client"] is service_sb


def test_remove_feedback_deletes_via_user_client(
    overrides: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_sb = MagicMock(name="user_client")
    service_sb = MagicMock(name="service_client")
    captured: dict[str, object] = {}

    def fake_delete(supabase, **kwargs):
        captured["client"] = supabase
        return True

    monkeypatch.setattr("app.routers.feedback.delete_feedback", fake_delete)
    app.dependency_overrides[get_supabase] = lambda: service_sb
    app.dependency_overrides[get_user_supabase] = lambda: user_sb
    app.dependency_overrides[get_current_user_id] = lambda: _TEST_USER_ID

    r = TestClient(app).delete(f"/jobs/job-1/feedback?target_id={_TARGET_ID}")

    assert r.status_code == 204
    assert captured["client"] is user_sb
    assert captured["client"] is not service_sb


def test_learning_log_reads_via_user_client(
    overrides: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_sb = MagicMock(name="user_client")
    service_sb = MagicMock(name="service_client")
    # The endpoint queries supabase.table(...) directly; make the chain return
    # no rows so the response validates, and assert which client was used.
    (
        user_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value.data
    ) = []
    monkeypatch.setattr(
        "app.routers.feedback._target_exists_for_user", lambda *a, **k: True
    )
    app.dependency_overrides[get_supabase] = lambda: service_sb
    app.dependency_overrides[get_user_supabase] = lambda: user_sb
    app.dependency_overrides[get_current_user_id] = lambda: _TEST_USER_ID

    r = TestClient(app).get(f"/targets/{_TARGET_ID}/learning-log")

    assert r.status_code == 200
    assert r.json() == []
    user_sb.table.assert_called_once_with("target_learning_log")
    service_sb.table.assert_not_called()
