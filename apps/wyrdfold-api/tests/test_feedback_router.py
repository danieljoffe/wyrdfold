"""Router wiring tests for feedback reads (#79 Phase 2 — job_feedback slice).

The headline assertion: GET /targets/{id}/feedback now reads through the
JWT-bound user client (``get_user_supabase``), not the service-role client,
so Postgres RLS is the backstop. The cross-tenant RLS proof itself lives in
``tests/integration/test_rls_feedback.py`` (needs a live stack).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.dependencies import get_current_user_id, get_supabase, get_user_supabase
from app.main import app

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
