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

from unittest.mock import MagicMock


class FakePhase1RejectionsQuery:
    """One built SELECT against :class:`FakePhase1RejectionsTable`."""

    def __init__(self, rows: dict[tuple[str, int, str], dict]) -> None:
        self._rows = rows
        self._target_id: str | None = None
        self._profile_version: int | None = None
        self._cutoff: str | None = None
        self._titles: set[str] = set()

    def eq(self, column: str, value: object) -> FakePhase1RejectionsQuery:
        if column == "target_id":
            self._target_id = str(value)
        elif column == "profile_version":
            self._profile_version = int(value)  # type: ignore[arg-type]
        return self

    def gt(self, _column: str, value: str) -> FakePhase1RejectionsQuery:
        self._cutoff = value
        return self

    def in_(self, _column: str, values: list[str]) -> FakePhase1RejectionsQuery:
        self._titles = set(values)
        return self

    def execute(self) -> MagicMock:
        resp = MagicMock()
        resp.data = [
            {"title_norm": key[2]}
            for key, row in self._rows.items()
            if key[0] == self._target_id
            and key[1] == self._profile_version
            and key[2] in self._titles
            and (self._cutoff is None or row["judged_at"] > self._cutoff)
        ]
        return resp


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

    def upsert(
        self, rows: list[dict], on_conflict: str = "", **_kwargs: object
    ) -> MagicMock:
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
