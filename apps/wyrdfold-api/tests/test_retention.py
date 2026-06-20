"""Retention purge for append-only operational logs (#29 P3).

Pins the contract:

* rows older than the window are deleted from ``llm_costs`` (by
  ``created_at``) and ``notifications_sent`` (by ``sent_at``);
* recent rows survive;
* a window of 0 days retains that table indefinitely (no delete issued);
* the purge is idempotent;
* the operator endpoint is api-key gated and returns the per-table counts.

Uses an in-memory fake supabase whose ``delete().lt()`` filters by ISO
timestamp string comparison (valid for same-format UTC timestamps).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.services.retention import purge_expired_records

# Clearly-old (always purged) and far-future (always kept) sentinels, so
# the assertions don't depend on the wall clock.
_OLD = "2000-01-01T00:00:00+00:00"
_FUTURE = "2999-01-01T00:00:00+00:00"


class _FakeDeleteQuery:
    def __init__(self, name: str, rows: list[dict[str, Any]], log: list) -> None:
        self.name = name
        self._rows = rows
        self._log = log
        self._lt: tuple[str, str] | None = None

    def delete(self, *, count: str | None = None, returning: str | None = None):
        self._log.append(("delete", self.name, count, returning))
        return self

    def lt(self, col: str, val: str):
        self._lt = (col, val)
        return self

    def execute(self) -> SimpleNamespace:
        assert self._lt is not None, "purge must filter with .lt(ts_col, cutoff)"
        col, val = self._lt
        matched = [r for r in self._rows if str(r.get(col)) < val]
        self._rows[:] = [r for r in self._rows if not (str(r.get(col)) < val)]
        return SimpleNamespace(count=len(matched), data=[])


class _FakeSupabase:
    def __init__(self, tables: dict[str, list[dict[str, Any]]]) -> None:
        self.tables = tables
        self.log: list = []

    def table(self, name: str) -> _FakeDeleteQuery:
        return _FakeDeleteQuery(name, self.tables.setdefault(name, []), self.log)


def _seeded() -> _FakeSupabase:
    return _FakeSupabase(
        {
            "llm_costs": [
                {"created_at": _OLD},
                {"created_at": _OLD},
                {"created_at": _FUTURE},
            ],
            "notifications_sent": [
                {"sent_at": _OLD},
                {"sent_at": _FUTURE},
            ],
        }
    )


def test_purges_old_rows_and_keeps_recent() -> None:
    sb = _seeded()
    report = purge_expired_records(sb, llm_costs_days=365, notifications_sent_days=180)
    assert report == {"llm_costs": 2, "notifications_sent": 1}
    # Only the future-dated rows survive.
    assert sb.tables["llm_costs"] == [{"created_at": _FUTURE}]
    assert sb.tables["notifications_sent"] == [{"sent_at": _FUTURE}]


def test_filters_on_the_right_timestamp_column() -> None:
    sb = _seeded()
    cols = {}

    # Capture the column each table was filtered on.
    orig_table = sb.table

    def _tracking_table(name: str) -> _FakeDeleteQuery:
        q = orig_table(name)
        real_lt = q.lt

        def _lt(col: str, val: str):
            cols[name] = col
            return real_lt(col, val)

        q.lt = _lt  # type: ignore[method-assign]
        return q

    sb.table = _tracking_table  # type: ignore[method-assign]
    purge_expired_records(sb, llm_costs_days=30, notifications_sent_days=30)
    assert cols == {"llm_costs": "created_at", "notifications_sent": "sent_at"}


def test_zero_window_retains_table_indefinitely() -> None:
    sb = _seeded()
    report = purge_expired_records(sb, llm_costs_days=0, notifications_sent_days=180)
    # llm_costs untouched (no delete issued), notifications purged.
    assert report["llm_costs"] == 0
    assert len(sb.tables["llm_costs"]) == 3
    deleted_tables = [name for op, name, *_ in sb.log if op == "delete"]
    assert "llm_costs" not in deleted_tables
    assert "notifications_sent" in deleted_tables


def test_uses_minimal_return_to_avoid_large_payloads() -> None:
    sb = _seeded()
    purge_expired_records(sb, llm_costs_days=365, notifications_sent_days=180)
    deletes = [(count, returning) for op, _, count, returning in sb.log if op == "delete"]
    assert deletes, "expected delete calls"
    for count, returning in deletes:
        assert count == "exact"
        assert returning == "minimal"


def test_idempotent_second_run_purges_nothing() -> None:
    sb = _seeded()
    purge_expired_records(sb, llm_costs_days=365, notifications_sent_days=180)
    second = purge_expired_records(sb, llm_costs_days=365, notifications_sent_days=180)
    assert second == {"llm_costs": 0, "notifications_sent": 0}


def test_purge_endpoint_is_api_key_gated_and_returns_counts() -> None:
    from fastapi.testclient import TestClient

    from app.dependencies import get_supabase, verify_api_key
    from app.main import app

    sb = _seeded()
    app.dependency_overrides[verify_api_key] = lambda: None
    app.dependency_overrides[get_supabase] = lambda: sb
    try:
        client = TestClient(app)
        resp = client.post("/admin/retention/purge")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    # Defaults (365 / 180 days) purge the year-2000 sentinels.
    assert resp.json() == {"llm_costs": 2, "notifications_sent": 1}
