"""Stateful fakes for the Phase-1 activation backfill (#930).

Written from PostgREST's actual contract, not from what the code under test
happens to call (the mocks-that-can't-fail rule):

- ``jobs`` really applies ``.is_(archived_at, null)``, ``.gte(cataloged_at,
  cutoff)``, the two ``ORDER BY`` specs and the ``.range()`` slice, in that
  order. A test that asserts "the age bound excluded the old rows" or
  "newest first" would be vacuous against a fake that ignored them.
- ``scores`` really applies ``.eq(target_id)``, ``.is_(promising, null)``
  and ``.in_(job_posting_id)``, and its ``upsert(on_conflict=...)`` really
  MERGES onto the stored row — so a graded row stops being a candidate on
  the next read, exactly as in Postgres.
- ``llm_costs`` really counts by ``purpose`` + ``metadata->>target_id`` +
  ``created_at``, and the rows it counts are the ones the code under test
  inserted. That is what makes "backfill and fresh ingestion share one
  cap" a testable claim rather than an assertion about a constant.

Every builder method NOT used by the real code is deliberately absent: if a
query shape changes, these fakes AttributeError loudly instead of fluently
no-op'ing past the change. (``jobs`` has ``.range`` but no ``.limit``/``.gt``
on purpose — the backfill pages by OFFSET over a DESC date sort, which is
the opposite of ``bulk_title_score_for_target``'s keyset contract.)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

from tests.support.fake_phase1_store import FakePhase1RejectionsTable


class Resp:
    """A response usable from both supabase client shapes (see
    ``fake_phase1_store.AwaitableResponse`` — the rejection store reaches its
    table through ``poll_db_read``'s sync fallback while the backfill awaits
    ``execute()`` directly)."""

    def __init__(self, data: list[dict[str, Any]], count: int | None = None) -> None:
        self.data = data
        self.count = count

    def __await__(self):  # type: ignore[no-untyped-def]
        return self
        yield  # pragma: no cover — makes this method a generator


class _JobsQuery:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = list(rows)
        self._orders: list[tuple[str, bool]] = []
        self._range: tuple[int, int] | None = None

    def select(self, *_cols: str, **_kw: object) -> _JobsQuery:
        return self

    def is_(self, column: str, value: str) -> _JobsQuery:
        assert value == "null", f"unsupported is_ value {value!r}"
        self._rows = [r for r in self._rows if r.get(column) is None]
        return self

    def gte(self, column: str, value: str) -> _JobsQuery:
        # ISO-8601 UTC strings on both sides — lexicographic compare matches
        # Postgres' timestamptz ordering. A NULL never satisfies >=.
        self._rows = [r for r in self._rows if r.get(column) is not None and r[column] >= value]
        return self

    def order(self, column: str, *, desc: bool = False) -> _JobsQuery:
        self._orders.append((column, desc))
        return self

    def range(self, lo: int, hi: int) -> _JobsQuery:
        self._range = (lo, hi)
        return self

    def execute(self) -> Resp:
        rows = list(self._rows)
        # Apply ORDER BY specs primary-first: stable-sort from last to first.
        for column, desc in reversed(self._orders):
            rows.sort(key=lambda r: r[column], reverse=desc)
        if self._range is not None:
            lo, hi = self._range
            rows = rows[lo : hi + 1]
        return Resp([dict(r) for r in rows])


class FakeJobsTable:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.select_calls = 0

    def select(self, *_cols: str, **_kw: object) -> _JobsQuery:
        self.select_calls += 1
        return _JobsQuery(self.rows)


class _ScoresQuery:
    def __init__(self, rows: dict[tuple[str, str], dict[str, Any]]) -> None:
        self._rows = rows
        self._target_id: str | None = None
        self._promising_null = False
        self._ids: set[str] | None = None

    def eq(self, column: str, value: object) -> _ScoresQuery:
        assert column == "target_id", f"unsupported scores filter {column!r}"
        self._target_id = str(value)
        return self

    def is_(self, column: str, value: str) -> _ScoresQuery:
        assert column == "promising" and value == "null"
        self._promising_null = True
        return self

    def in_(self, column: str, values: list[str]) -> _ScoresQuery:
        assert column == "job_posting_id"
        self._ids = set(values)
        return self

    def execute(self) -> Resp:
        out: list[dict[str, Any]] = []
        for (job_id, target_id), row in self._rows.items():
            if self._target_id is not None and target_id != self._target_id:
                continue
            if self._promising_null and row.get("promising") is not None:
                continue
            if self._ids is not None and job_id not in self._ids:
                continue
            out.append(dict(row))
        return Resp(out)


class FakeScoresTable:
    """``scores`` keyed on the real unique constraint
    ``(job_posting_id, target_id)``."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows or []:
            self.rows[(row["job_posting_id"], row["target_id"])] = dict(row)
        self.upsert_calls: list[list[dict[str, Any]]] = []

    def select(self, *_cols: str, **_kw: object) -> _ScoresQuery:
        return _ScoresQuery(self.rows)

    def upsert(self, rows: list[dict[str, Any]], on_conflict: str = "", **_kw: object) -> MagicMock:
        assert on_conflict == "job_posting_id,target_id"
        self.upsert_calls.append([dict(r) for r in rows])
        for row in rows:
            key = (row["job_posting_id"], row["target_id"])
            # ON CONFLICT DO UPDATE SET <supplied columns> — merge, don't
            # replace, so untouched columns (score, breakdown) survive.
            self.rows.setdefault(key, {}).update(row)
        handle = MagicMock()
        handle.execute.return_value = Resp([dict(r) for r in rows])
        return handle


class _CostsQuery:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = list(rows)

    def eq(self, column: str, value: object) -> _CostsQuery:
        if column == "metadata->>target_id":
            self._rows = [
                r for r in self._rows if str((r.get("metadata") or {}).get("target_id")) == value
            ]
        else:
            self._rows = [r for r in self._rows if r.get(column) == value]
        return self

    def gte(self, column: str, value: str) -> _CostsQuery:
        self._rows = [r for r in self._rows if r.get(column) is not None and r[column] >= value]
        return self

    def execute(self) -> Resp:
        # head=True → count only, no rows shipped.
        return Resp([], count=len(self._rows))


class FakeLlmCostsTable:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows: list[dict[str, Any]] = [dict(r) for r in rows or []]
        self.count_reads = 0

    def select(self, *_cols: str, **kwargs: object) -> _CostsQuery:
        assert kwargs.get("count") == "exact" and kwargs.get("head") is True
        self.count_reads += 1
        return _CostsQuery(self.rows)

    def insert(self, row: dict[str, Any]) -> MagicMock:
        stored = dict(row)
        stored.setdefault("id", str(uuid.uuid4()))
        stored.setdefault("created_at", datetime.now(UTC).isoformat())
        self.rows.append(stored)
        handle = MagicMock()
        handle.execute.return_value = Resp([stored])
        return handle


def cost_row(*, target_id: str, purpose: str, created_at: str | None = None) -> dict[str, Any]:
    """A pre-existing ``llm_costs`` row, for seeding "the day's cap is
    already partly spent"."""
    return {
        "id": str(uuid.uuid4()),
        "user_id": "u-1",
        "model": "claude-haiku-4-5",
        "purpose": purpose,
        "input_tokens": 10,
        "output_tokens": 10,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cost_usd": 0.0001,
        "latency_ms": 5,
        "metadata": {"target_id": target_id},
        "created_at": created_at or datetime.now(UTC).isoformat(),
    }


def backfill_supabase(
    *,
    jobs: list[dict[str, Any]],
    scores: list[dict[str, Any]],
    costs: list[dict[str, Any]] | None = None,
) -> MagicMock:
    """A supabase mock routing the four tables the backfill touches.

    Each fake table rides along as ``supabase._<table>`` (the convention the
    poller/rejection-store mock builders use) so a test can assert
    preconditions against real stored rows before asserting outcomes.
    """
    jobs_table = FakeJobsTable(jobs)
    scores_table = FakeScoresTable(scores)
    costs_table = FakeLlmCostsTable(costs)
    rejections_table = FakePhase1RejectionsTable()
    tables: dict[str, Any] = {
        "jobs": jobs_table,
        "scores": scores_table,
        "llm_costs": costs_table,
        "phase1_rejections": rejections_table,
    }
    supabase = MagicMock()
    supabase.table.side_effect = lambda name: tables[name]
    supabase._jobs = jobs_table
    supabase._scores = scores_table
    supabase._llm_costs = costs_table
    supabase._phase1_rejections = rejections_table
    return supabase
