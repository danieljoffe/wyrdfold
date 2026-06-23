"""Reliability hardening regressions (audit #29 Option B).

Covers two of the three fixes:
  - the shared httpx pool is sized to the real poll-cycle fan-out and
    carries an explicit per-phase timeout (Fix 2), and
  - the new ``/ready`` readiness probe checks Supabase and 503s when the
    dependency is down, while ``/health`` stays pure liveness (Fix 3).

The third fix (CostLogBuffer bounded memory + chunked INSERT) lives in
``test_llm_cost_log_buffer.py`` alongside the rest of the buffer suite.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main_mod
from app.http_client import (
    MAX_CONNECTIONS,
    MAX_KEEPALIVE_CONNECTIONS,
    get_http_client,
)
from app.main import app
from app.services import smartrecruiters, workday
from app.services.poller import POLL_CONCURRENCY

# ---------------------------------------------------------------------------
# Fix 2 — shared httpx pool sized for the real fan-out
# ---------------------------------------------------------------------------


def test_pool_ceiling_covers_worst_case_detail_fanout() -> None:
    """The pool must hold the worst-case concurrent detail fetches:
    every poll worker (POLL_CONCURRENCY) being an SR/Workday source that
    each fans out _DETAIL_CONCURRENCY per-posting fetches through the
    SAME client. The old ceiling of 20 starved ~30 of those 50 → drops.
    """
    worst_case = POLL_CONCURRENCY * max(
        smartrecruiters._DETAIL_CONCURRENCY, workday._DETAIL_CONCURRENCY
    )
    assert worst_case == 50  # pins the assumption the sizing rests on
    assert worst_case <= MAX_CONNECTIONS, (
        f"pool ceiling {MAX_CONNECTIONS} < worst-case fan-out {worst_case}: "
        "detail fetches will queue and time out"
    )
    # Headroom for the scheduler tick / user-paste fetches on top.
    assert worst_case + 10 <= MAX_CONNECTIONS


def test_client_built_with_configured_limits_and_explicit_timeout() -> None:
    """Construction wires the sized limits AND an explicit per-phase
    Timeout (so a pool-acquisition stall surfaces as PoolTimeout instead
    of silently eating the read budget)."""
    client = get_http_client()

    # The effective ceiling lives on the transport's connection pool.
    # httpx (0.28) doesn't expose limits on the client publicly, so reach
    # through to the pool the client actually built — this verifies the
    # configured value took effect end-to-end, not just that we passed it.
    pool = client._transport._pool  # type: ignore[attr-defined]
    assert pool._max_connections == MAX_CONNECTIONS
    assert pool._max_keepalive_connections == MAX_KEEPALIVE_CONNECTIONS

    timeout = client.timeout  # public attribute
    assert isinstance(timeout, httpx.Timeout)
    # All four phases set independently — in particular ``pool`` is its
    # own budget, the crux of the fix (a pool-acquisition stall raises
    # PoolTimeout instead of bleeding into the read deadline).
    assert timeout.pool is not None
    assert timeout.connect is not None
    assert timeout.read is not None
    assert timeout.pool <= timeout.read  # pool wait shouldn't dominate


# ---------------------------------------------------------------------------
# Fix 3 — /ready readiness probe (healthy + dependency-down → 503)
# ---------------------------------------------------------------------------


def _ping_ok_supabase() -> MagicMock:
    sb = MagicMock()
    sb.table.return_value.select.return_value.limit.return_value.execute.return_value = (
        MagicMock(data=[{"id": "s1"}])
    )
    return sb


def _ping_failing_supabase() -> MagicMock:
    sb = MagicMock()
    sb.table.return_value.select.return_value.limit.return_value.execute.side_effect = (
        Exception("supabase down")
    )
    return sb


def test_health_is_pure_liveness(monkeypatch: pytest.MonkeyPatch) -> None:
    """/health never touches the dependency — stays 200 even with Supabase
    unconfigured, so a DB blip can't trigger a container restart loop."""
    monkeypatch.setattr(main_mod, "get_supabase_pool", lambda: None)
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_ready_200_when_supabase_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_mod, "get_supabase_pool", _ping_ok_supabase)
    client = TestClient(app)
    resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"


def test_ready_503_when_supabase_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_mod, "get_supabase_pool", lambda: None)
    client = TestClient(app)
    resp = client.get("/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
    assert body["dependency"] == "supabase"


def test_ready_503_when_supabase_ping_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The crux: a reachable-but-broken Supabase (ping raises) returns 503
    so the LB stops routing readiness-gated traffic to this instance."""
    monkeypatch.setattr(main_mod, "get_supabase_pool", _ping_failing_supabase)
    client = TestClient(app)
    resp = client.get("/ready")
    assert resp.status_code == 503
    assert resp.json()["status"] == "not_ready"
