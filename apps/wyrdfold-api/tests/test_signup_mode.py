"""Signup-mode switch endpoints (Phase 3 slice 5).

The live proof — the switch moving the REAL before-user-created hook —
is in tests/integration/test_signup_perimeter.py. These pin the API
surfaces: the public probe fails safe to 'closed' in every degraded
state, and the operator lever is api-key-gated with a validated value.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.dependencies import get_async_service_supabase, verify_api_key
from app.main import app


@pytest.fixture
def sb() -> Any:
    fake = MagicMock(name="supabase")
    # Both endpoints are async (#57 slice 4): the awaited ``.execute()`` needs
    # AsyncMocks on the select-read and upsert-write chains.
    fake.table.return_value.select.return_value.eq.return_value.execute = AsyncMock(
        return_value=MagicMock(data=[])
    )
    fake.table.return_value.upsert.return_value.execute = AsyncMock(return_value=MagicMock(data=[]))
    app.dependency_overrides[get_async_service_supabase] = lambda: fake
    yield fake
    app.dependency_overrides.clear()


def _mode(sb: MagicMock, value: Any) -> None:
    sb.table.return_value.select.return_value.eq.return_value.execute = AsyncMock(
        return_value=MagicMock(data=[{"value": value}] if value is not None else [])
    )


def test_signup_mode_open_when_set(sb: MagicMock) -> None:
    _mode(sb, "open")
    r = TestClient(app).get("/signup-mode")
    assert r.status_code == 200
    assert r.json() == {"mode": "open"}


@pytest.mark.parametrize("value", [None, "closed", "opeen", "", "OPEN"])
def test_signup_mode_fails_safe_to_closed(sb: MagicMock, value: Any) -> None:
    """Negative: missing row, unknown or wrong-case values → 'closed'.
    Only the exact sentinel opens (mirrors the hook)."""
    _mode(sb, value)
    r = TestClient(app).get("/signup-mode")
    assert r.json() == {"mode": "closed"}


def test_signup_mode_read_error_reports_closed(sb: MagicMock) -> None:
    """Negative: a DB failure must never present an open perimeter."""
    sb.table.return_value.select.return_value.eq.return_value.execute.side_effect = RuntimeError(
        "db down"
    )
    r = TestClient(app).get("/signup-mode")
    assert r.status_code == 200
    assert r.json() == {"mode": "closed"}


def test_set_signup_mode_requires_api_key() -> None:
    """Negative: the flip lever is operator-only."""
    app.dependency_overrides.clear()
    r = TestClient(app).post("/admin/signup-mode", json={"mode": "open"})
    assert r.status_code == 401


def test_set_signup_mode_upserts_and_rejects_junk(sb: MagicMock) -> None:
    app.dependency_overrides[verify_api_key] = lambda: None

    r = TestClient(app).post("/admin/signup-mode", json={"mode": "open"})
    assert r.status_code == 200
    assert r.json() == {"mode": "open"}
    upsert = sb.table.return_value.upsert.call_args
    assert upsert.args[0]["key"] == "signup_mode"
    assert upsert.args[0]["value"] == "open"
    assert upsert.kwargs["on_conflict"] == "key"

    # Negative: only the two literals are accepted.
    r2 = TestClient(app).post("/admin/signup-mode", json={"mode": "wide-open"})
    assert r2.status_code == 422
