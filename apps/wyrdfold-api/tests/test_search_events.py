"""Search-funnel instrumentation (#467 §10 PR6).

Pins the contract:

* PRIVACY — an enqueued row never carries a user id or an IP, whatever the
  caller does (the builders have no such parameters; this is the ledger's
  core promise);
* both search endpoints emit a ``search`` event, on the cache-hit path too
  (a cached response is still a user search — volume must count it);
* the beacon accepts only the two funnel ticks with a strictly-typed
  payload (junk → 422, never a row) and answers 204;
* the buffer instance targets the ``search_events`` table (the
  parameterized ``CostLogBuffer`` writes where it's told to);
* instrumentation is fire-and-forget: an enqueue blow-up must not fail
  the search request.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.dependencies import (
    get_async_service_supabase,
    require_bff_secret,
    verify_api_key_or_jwt,
)
from app.main import app
from app.services import search_events
from app.services.search_events import record_funnel_event, record_search

_FORBIDDEN_KEYS = {"user_id", "ip", "client_ip", "x_forwarded_for"}


def teardown_function() -> None:
    app.dependency_overrides.clear()


@pytest.fixture()
def captured_rows(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(search_events.buffer, "enqueue", rows.append)
    return rows


# ---- row builders (the privacy contract) ------------------------------------


def test_search_row_is_normalized_and_carries_no_identity(
    captured_rows: list[dict[str, Any]],
) -> None:
    record_search(
        surface="public",
        q="  Senior ENGINEER " + "x" * 300,  # oversized, messy
        result_count=7,
        has_more=True,
        offset=20,
        location="  Remote (US) ",
        posted_within_days=7,
    )
    (row,) = captured_rows
    assert row["event_type"] == "search"
    assert row["surface"] == "public"
    # Normalized exactly like the response-cache key: strip/lower/cap.
    assert row["query"].startswith("senior engineer ")
    assert len(row["query"]) <= 120
    assert row["location"] == "remote (us)"
    assert row["result_count"] == 7 and row["has_more"] is True
    assert row["page_offset"] == 20 and row["posted_within_days"] == 7
    # The core promise: no identity, ever.
    assert not (_FORBIDDEN_KEYS & row.keys())


def test_funnel_row_carries_no_identity(captured_rows: list[dict[str, Any]]) -> None:
    record_funnel_event(
        event_type="card_open",
        surface="authed",
        job_posting_id="11111111-1111-1111-1111-111111111111",
    )
    (row,) = captured_rows
    assert row["event_type"] == "card_open"
    assert not (_FORBIDDEN_KEYS & row.keys())


def test_empty_location_becomes_null_not_empty_string(
    captured_rows: list[dict[str, Any]],
) -> None:
    record_search(
        surface="authed",
        q="engineer",
        result_count=0,
        has_more=False,
        offset=0,
        location="   ",
        posted_within_days=None,
    )
    assert captured_rows[0]["location"] is None


# ---- endpoint emission (incl. the cache-hit path) ---------------------------


def _search_supabase_stub() -> MagicMock:
    """Supabase stub for the search service: empty pages everywhere."""
    qb = MagicMock()
    for meth in ("select", "eq", "in_", "ilike", "gte", "is_", "or_", "order", "range", "limit"):
        getattr(qb, meth).return_value = qb
    qb.not_ = qb  # ``.not_`` is a property, not a call → same builder
    result = MagicMock()
    result.data = []
    qb.execute = AsyncMock(return_value=result)
    sb = MagicMock()
    sb.table.return_value = qb
    return sb


def _client():  # late import so dependency overrides apply
    from fastapi.testclient import TestClient

    return TestClient(app)


def test_authed_search_emits_search_event_even_on_cache_hit(
    captured_rows: list[dict[str, Any]],
) -> None:
    app.dependency_overrides[verify_api_key_or_jwt] = lambda: "jwt"
    app.dependency_overrides[get_async_service_supabase] = _search_supabase_stub

    client = _client()
    r1 = client.get("/search", params={"q": "cache-hit-probe-authed"})
    assert r1.status_code == 200
    r2 = client.get("/search", params={"q": "cache-hit-probe-authed"})  # cached
    assert r2.status_code == 200

    searches = [r for r in captured_rows if r["event_type"] == "search"]
    assert len(searches) == 2, "the cache-hit search must be instrumented too"
    assert all(r["surface"] == "authed" for r in searches)


def test_public_search_emits_search_event_with_public_surface(
    captured_rows: list[dict[str, Any]],
) -> None:
    app.dependency_overrides[require_bff_secret] = lambda: None
    app.dependency_overrides[get_async_service_supabase] = _search_supabase_stub

    r = _client().get("/public/search", params={"q": "public-emit-probe"})
    assert r.status_code == 200
    (row,) = [r for r in captured_rows if r["event_type"] == "search"]
    assert row["surface"] == "public"
    assert row["query"] == "public-emit-probe"
    assert row["result_count"] == 0  # zero-result rows are the coverage-gap signal


def test_enqueue_failure_never_fails_the_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """Analytics must be strictly fire-and-forget: if the buffer blows up,
    the search request still succeeds."""

    def _boom(_row: dict[str, Any]) -> None:
        raise RuntimeError("buffer exploded")

    monkeypatch.setattr(search_events.buffer, "enqueue", _boom)
    app.dependency_overrides[require_bff_secret] = lambda: None
    app.dependency_overrides[get_async_service_supabase] = _search_supabase_stub

    r = _client().get("/public/search", params={"q": "resilience-probe"})
    assert r.status_code == 200


# ---- the beacon endpoint ----------------------------------------------------


def test_beacon_records_card_open_and_returns_204(
    captured_rows: list[dict[str, Any]],
) -> None:
    app.dependency_overrides[require_bff_secret] = lambda: None
    r = _client().post(
        "/search-events",
        json={
            "event_type": "card_open",
            "surface": "public",
            "job_posting_id": "11111111-1111-1111-1111-111111111111",
        },
    )
    assert r.status_code == 204
    (row,) = captured_rows
    assert row["event_type"] == "card_open"
    assert row["job_posting_id"] == "11111111-1111-1111-1111-111111111111"


def test_beacon_rejects_unknown_event_type_and_junk_uuid(
    captured_rows: list[dict[str, Any]],
) -> None:
    app.dependency_overrides[require_bff_secret] = lambda: None
    client = _client()
    # `search` is server-stamped only — the beacon must refuse to forge it.
    r1 = client.post("/search-events", json={"event_type": "search", "surface": "public"})
    assert r1.status_code == 422
    r2 = client.post(
        "/search-events",
        json={
            "event_type": "card_open",
            "surface": "public",
            "job_posting_id": "not-a-uuid'; drop table scores;--",
        },
    )
    assert r2.status_code == 422
    r3 = client.post(
        "/search-events", json={"event_type": "card_open", "surface": "operator"}
    )
    assert r3.status_code == 422
    assert captured_rows == []  # junk never becomes a row


def test_beacon_is_bff_gated() -> None:
    """With the secret configured, a direct hit (no ``X-Wyrdfold-BFF``
    header) is 403 — same posture as /public/search. (Unset secret fails
    open by documented launch-gate design; test_bff_secret.py owns that.)"""
    from app.config import Settings
    from app.dependencies import get_settings

    app.dependency_overrides[get_settings] = lambda: Settings(wyrdfold_bff_secret="s3cret")
    r = _client().post(
        "/search-events", json={"event_type": "card_open", "surface": "public"}
    )
    assert r.status_code == 403


# ---- buffer wiring ----------------------------------------------------------


def test_buffer_targets_the_search_events_table() -> None:
    from app.services.llm.cost_log_buffer import TABLE as COST_TABLE

    assert search_events.buffer._table == "search_events"
    # And the cost-log singleton still points at llm_costs (no cross-wiring).
    from app.services.llm.cost_log_buffer import buffer as cost_buffer

    assert cost_buffer._table == COST_TABLE == "llm_costs"


@pytest.mark.asyncio
async def test_parameterized_buffer_inserts_into_its_own_table() -> None:
    from app.services.llm.cost_log_buffer import CostLogBuffer

    buf = CostLogBuffer(table="search_events", label="search-events")
    buf.enqueue({"event_type": "search", "surface": "public"})
    sb = MagicMock()
    written = await buf.flush(sb)
    assert written == 1
    sb.table.assert_called_once_with("search_events")
