"""A stateful, faithful fake of the ``phase1_rejections`` Postgres table.

Shared by the poller-path tests (``test_poller.py``) and the rejection-store
unit tests (``test_rejection_store.py``). Row storage is real — a dict keyed
like the table's PK — so a second poll cycle, or a simulated process restart
(which rebuilds every in-process object but not the DB), sees the first
cycle's writes exactly as prod does. The in-process dict this store replaced
could not be faked this way, which is precisely how its restart-amnesia went
untested (docs/plan-phase1-rejection-persistence.md).

Fixture behavior is written from PostgREST's actual contract (the
mocks-that-can't-fail rule):

- ``upsert(rows, on_conflict=...)`` merges on the conflict key — a re-judged
  rejection REPLACES its row (refreshing ``judged_at``), never duplicates.
- ``select`` applies the exact filter chain ``fetch_rejected_titles`` builds:
  ``.eq(target_id).eq(profile_version).gt(judged_at).in_(title_norm)``.
- ``judged_at`` comparison is lexicographic on ISO-8601 UTC strings, which
  matches Postgres' timestamptz ordering for the fixed-format strings both
  sides produce.

Every other builder method is deliberately ABSENT: if the store's query shape
changes, these tests fail loudly instead of fluently no-op'ing past the
change.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock


class AwaitableResponse:
    """A response usable from BOTH supabase client shapes.

    The rejection store reaches this fake through the ``poll_db_read``
    sync fallback (calls ``execute()`` and reads ``.data``); the
    near-miss insight path awaits ``execute()`` directly on the async
    client. Awaiting resolves to the same object, so one fake serves
    both without duplicating row semantics.
    """

    def __init__(self, data: list[dict[str, Any]]) -> None:
        self.data = data

    def __await__(self):
        return self
        yield  # pragma: no cover — makes this method a generator


class FakePhase1RejectionsQuery:
    """One built SELECT against :class:`FakePhase1RejectionsTable`.

    Covers the two real query shapes against the table:

    - the rejection store's membership read —
      ``.eq(target_id).eq(profile_version).gt(judged_at).in_(title_norm)``
    - the near-miss insight read —
      ``.eq(target_id).eq(profile_version).lt(confidence).gte(judged_at)
      .order(confidence).order(judged_at, desc=True).limit(n)``
    """

    def __init__(self, rows: dict[tuple[str, int, str], dict]) -> None:
        self._rows = rows
        self._target_id: str | None = None
        self._profile_version: int | None = None
        self._judged_after: str | None = None
        self._judged_after_inclusive = False
        self._confidence_below: int | None = None
        self._titles: set[str] | None = None
        self._orders: list[tuple[str, bool]] = []
        self._limit: int | None = None

    def eq(self, column: str, value: object) -> FakePhase1RejectionsQuery:
        if column == "target_id":
            self._target_id = str(value)
        elif column == "profile_version":
            self._profile_version = int(value)  # type: ignore[arg-type]
        return self

    def gt(self, _column: str, value: str) -> FakePhase1RejectionsQuery:
        self._judged_after = value
        self._judged_after_inclusive = False
        return self

    def gte(self, _column: str, value: str) -> FakePhase1RejectionsQuery:
        self._judged_after = value
        self._judged_after_inclusive = True
        return self

    def lt(self, _column: str, value: int) -> FakePhase1RejectionsQuery:
        self._confidence_below = value
        return self

    def in_(self, _column: str, values: list[str]) -> FakePhase1RejectionsQuery:
        self._titles = set(values)
        return self

    def order(self, column: str, *, desc: bool = False) -> FakePhase1RejectionsQuery:
        self._orders.append((column, desc))
        return self

    def limit(self, count: int) -> FakePhase1RejectionsQuery:
        self._limit = count
        return self

    def _matches(self, key: tuple[str, int, str], row: dict) -> bool:
        if key[0] != self._target_id or key[1] != self._profile_version:
            return False
        if self._titles is not None and key[2] not in self._titles:
            return False
        if self._judged_after is not None:
            # ISO-8601 UTC strings on both sides — lexicographic compare
            # matches Postgres' timestamptz ordering.
            ok = (
                row["judged_at"] >= self._judged_after
                if self._judged_after_inclusive
                else row["judged_at"] > self._judged_after
            )
            if not ok:
                return False
        if self._confidence_below is not None:
            conf = row.get("confidence")
            # SQL semantics: NULL never satisfies a < comparison.
            if conf is None or conf >= self._confidence_below:
                return False
        return True

    def execute(self) -> AwaitableResponse:
        matched = [dict(row) for key, row in self._rows.items() if self._matches(key, row)]
        # Apply order specs primary-first: stable-sort from the last spec
        # to the first, mirroring SQL's ORDER BY a, b.
        for column, desc in reversed(self._orders):
            matched.sort(key=lambda r: r[column], reverse=desc)
        if self._limit is not None:
            matched = matched[: self._limit]
        return AwaitableResponse(matched)


class FakePhase1RejectionsTable:
    """Stateful stand-in for the ``phase1_rejections`` table.

    ``rows`` is exposed for precondition asserts (a skip assertion is
    vacuous unless the row it relies on provably exists) and for poisoning
    (expired ``judged_at``, wrong ``profile_version``) in sabotage tests.
    """

    def __init__(self) -> None:
        self.rows: dict[tuple[str, int, str], dict] = {}
        self.select_calls = 0
        self.upsert_calls = 0

    def upsert(self, rows: list[dict], on_conflict: str = "", **_kwargs: object) -> MagicMock:
        assert on_conflict == "target_id,profile_version,title_norm"
        self.upsert_calls += 1
        for row in rows:
            key = (row["target_id"], row["profile_version"], row["title_norm"])
            self.rows[key] = dict(row)
        handle = MagicMock()
        handle.execute.return_value.data = []
        return handle

    def select(self, *_columns: str) -> FakePhase1RejectionsQuery:
        self.select_calls += 1
        return FakePhase1RejectionsQuery(self.rows)


def phase1_store_supabase() -> MagicMock:
    """A supabase mock that routes ONLY ``phase1_rejections`` — for unit
    tests of the rejection store itself. The fake table rides along as
    ``supabase._phase1_rejections`` (same convention as the poller mock
    builders)."""
    table = FakePhase1RejectionsTable()
    supabase = MagicMock()
    supabase.table.side_effect = lambda name: {"phase1_rejections": table}[name]
    supabase._phase1_rejections = table
    return supabase
