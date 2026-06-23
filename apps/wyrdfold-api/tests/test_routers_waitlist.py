"""Tests for the public waitlist router.

Covers: valid insert (generic success), case-insensitive normalisation,
duplicate-as-success (no enumeration), invalid-shape 422, over-length 422,
DB-failure generic 500 (no detail leak), and the per-IP rate limit (429).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.dependencies import get_supabase
from app.main import app
from app.rate_limit import limiter


class _FakeQuery:
    """Records the upsert args and returns a no-op execute()."""

    def __init__(self, recorder: dict[str, Any]) -> None:
        self._rec = recorder

    def upsert(self, row: dict[str, Any], **kwargs: Any) -> _FakeQuery:
        self._rec["row"] = row
        self._rec["kwargs"] = kwargs
        return self

    def execute(self) -> MagicMock:
        return MagicMock(data=[])


def _supabase_with_recorder() -> tuple[MagicMock, dict[str, Any]]:
    recorder: dict[str, Any] = {}
    supabase = MagicMock()
    supabase.table.return_value = _FakeQuery(recorder)
    return supabase, recorder


def _client(supabase: MagicMock) -> TestClient:
    app.dependency_overrides[get_supabase] = lambda: supabase
    return TestClient(app)


def teardown_function() -> None:
    app.dependency_overrides.clear()
    # Drop any per-test rate-limit buckets so the limiter test can't bleed
    # into a later run (slowapi keeps in-memory state keyed by client IP).
    limiter.reset()


def test_valid_email_inserts_and_returns_generic_success() -> None:
    supabase, rec = _supabase_with_recorder()
    resp = _client(supabase).post("/waitlist", json={"email": "jane@example.com"})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    supabase.table.assert_called_once_with("waitlist_signups")
    assert rec["row"] == {"email": "jane@example.com"}
    # ON CONFLICT DO NOTHING on the email column.
    assert rec["kwargs"]["on_conflict"] == "email"
    assert rec["kwargs"]["ignore_duplicates"] is True


def test_email_is_normalised_lowercase_and_trimmed() -> None:
    supabase, rec = _supabase_with_recorder()
    resp = _client(supabase).post(
        "/waitlist", json={"email": "  Jane.Doe@Example.COM  "}
    )

    assert resp.status_code == 200
    assert rec["row"] == {"email": "jane.doe@example.com"}


def test_duplicate_is_treated_as_success_without_revealing_existence() -> None:
    # ignore_duplicates → no error on conflict; the route must still 200 and
    # return the identical generic envelope (no enumeration).
    supabase, _ = _supabase_with_recorder()
    resp = _client(supabase).post("/waitlist", json={"email": "dup@example.com"})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_invalid_email_shape_is_rejected_before_any_db_call() -> None:
    supabase = MagicMock()
    resp = _client(supabase).post("/waitlist", json={"email": "not-an-email"})

    assert resp.status_code == 422
    supabase.table.assert_not_called()


def test_over_length_email_is_rejected_before_any_db_call() -> None:
    supabase = MagicMock()
    huge = "a" * 400 + "@example.com"
    resp = _client(supabase).post("/waitlist", json={"email": huge})

    # Pydantic max_length → 422 before the handler body runs.
    assert resp.status_code == 422
    supabase.table.assert_not_called()


def test_missing_email_is_rejected() -> None:
    supabase = MagicMock()
    resp = _client(supabase).post("/waitlist", json={})

    assert resp.status_code == 422
    supabase.table.assert_not_called()


def test_non_string_email_is_rejected() -> None:
    supabase = MagicMock()
    resp = _client(supabase).post("/waitlist", json={"email": 12345})

    assert resp.status_code == 422
    supabase.table.assert_not_called()


def test_db_failure_returns_generic_500_without_detail_leak() -> None:
    supabase = MagicMock()
    failing = MagicMock()
    failing.upsert.return_value = failing
    failing.execute.side_effect = RuntimeError(
        "PostgREST: relation waitlist_signups does not exist"
    )
    supabase.table.return_value = failing

    resp = _client(supabase).post("/waitlist", json={"email": "boom@example.com"})

    assert resp.status_code == 500
    detail = resp.json()["detail"]
    assert detail == "Something went wrong. Please try again."
    # The internal exception text must never reach the client.
    assert "PostgREST" not in detail
    assert "waitlist_signups" not in detail


def test_rate_limit_returns_429_after_budget_exhausted() -> None:
    # The limiter is globally disabled in tests (conftest RATE_LIMIT_ENABLED
    # =false). Flip it on for this case so the per-IP brake is exercised.
    supabase, _ = _supabase_with_recorder()
    client = _client(supabase)
    limiter.enabled = True
    try:
        # 5/minute is the tightest bucket — six requests from one IP trip it.
        statuses = [
            client.post("/waitlist", json={"email": f"ok{i}@example.com"}).status_code
            for i in range(6)
        ]
    finally:
        limiter.enabled = False

    assert statuses[:5] == [200, 200, 200, 200, 200]
    assert statuses[5] == 429
