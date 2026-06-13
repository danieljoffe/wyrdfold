"""Operator cost-summary endpoint (#26 F4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.config import settings as live_settings
from app.dependencies import get_supabase, verify_api_key
from app.main import app
from app.services.llm import cost_log


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _override(supabase: Any) -> None:
    app.dependency_overrides[get_supabase] = lambda: supabase
    # Skip the constant-time API-key check — the dep tests in
    # test_dependencies cover it; here we're testing the route shape.
    app.dependency_overrides[verify_api_key] = lambda: "test-api-key"


def test_cost_summary_returns_rollup_and_per_purpose_breakdown(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """Operator should see today/24h/7d/30d totals plus per-purpose
    breakdowns scoped to the cross-instance ledger — no per-user
    partition leaks through."""
    sb = MagicMock()
    _override(sb)

    # Stub the cost_log helpers. Each call's `since` lets us return a
    # scope-appropriate amount so the response shape is verifiable.
    def _fake_total_spend_all(_sb: Any, *, since: datetime | None = None) -> float:
        assert since is not None  # endpoint always sets a window
        now = datetime.now(UTC)
        # Approximate which window we're in by inspecting `since`.
        if since >= now - timedelta(hours=24, minutes=1):
            return 1.5  # both "today" and "last 24h" land here
        if since >= now - timedelta(days=7, hours=1):
            return 4.0
        return 12.0

    def _fake_spend_by_purpose_all(
        _sb: Any, *, since: datetime | None = None
    ) -> dict[str, float]:
        if since is None:
            return {}
        # Approximate window
        if since >= datetime.now(UTC) - timedelta(hours=25):
            return {"phase1_triage": 1.0, "phase2_fit": 0.5}
        return {"phase1_triage": 8.0, "phase2_fit": 4.0}

    monkeypatch.setattr(cost_log, "total_spend_all", _fake_total_spend_all)
    monkeypatch.setattr(cost_log, "spend_by_purpose_all", _fake_spend_by_purpose_all)
    monkeypatch.setattr(live_settings, "global_llm_daily_budget_usd", 10.0)

    try:
        resp = client.get("/admin/cost-summary")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert body["global_daily_cap_usd"] == 10.0
    assert body["today_usd"] == 1.5
    assert body["last_24h_usd"] == 1.5
    assert body["last_7d_usd"] == 4.0
    assert body["last_30d_usd"] == 12.0
    assert body["today_usage_pct"] == 15.0  # 1.5 / 10 * 100
    assert body["by_purpose_today"] == {"phase1_triage": 1.0, "phase2_fit": 0.5}
    assert body["by_purpose_30d"] == {"phase1_triage": 8.0, "phase2_fit": 4.0}


def test_cost_summary_usage_pct_none_when_cap_disabled(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    sb = MagicMock()
    _override(sb)

    monkeypatch.setattr(cost_log, "total_spend_all", lambda _s, **_kw: 5.0)
    monkeypatch.setattr(
        cost_log, "spend_by_purpose_all", lambda _s, **_kw: {}
    )
    monkeypatch.setattr(live_settings, "global_llm_daily_budget_usd", 0.0)

    try:
        resp = client.get("/admin/cost-summary")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert body["global_daily_cap_usd"] == 0.0
    assert body["today_usage_pct"] is None


def test_cost_summary_requires_api_key(client: TestClient) -> None:
    """No api-key → 401. Guards against accidentally exposing cross-user
    cost data to a JWT caller."""
    # Don't override verify_api_key — let the real dep reject.
    resp = client.get("/admin/cost-summary")
    assert resp.status_code == 401
