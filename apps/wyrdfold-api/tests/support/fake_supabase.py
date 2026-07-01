"""A small, faithful fake of the Supabase/PostgREST query builder for the
``/jobs`` two-query list tests.

Several test files had each hand-rolled the same fluent ``scores``/``jobs``
stub, and they'd already drifted (the Pending floor emulation was added to two
of them separately for #47/#123). This is the single shared version.

``two_query_supabase(scores, jobs)`` returns a ``MagicMock`` whose ``.table``
routes to:

- a **scores** chain that emulates the score floor the way ``_apply_score_floor``
  asks — ``.gte("score", n)`` is a plain floor, and ``.or_(...)`` is the
  Pending-aware floor (rows with ``scoring_status != 'complete'`` are exempt).
- a **jobs** chain that returns the postings for the ids passed to
  ``.in_("id", ids)``, in that id order (``jobs`` is an ``id -> row`` dict).

Both chains treat every other builder method (``select``/``eq``/``order``/…) as
a fluent no-op. Filtering is only what the list path actually exercises; add
more predicates here (once) if a future test needs them.
"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import MagicMock

_SCORE_GTE_RE = re.compile(r"score\.gte\.(\d+)")


class FakeResponse:
    def __init__(self, data: Any, count: int | None = None) -> None:
        self.data = data
        self.count = count


class ScoresQuery:
    """Emulates the ``scores`` table's server-side score floor."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self._floor: int | None = None
        self._exempt_pending = False

    def select(self, *_a: Any, **_kw: Any) -> ScoresQuery:
        return self

    def eq(self, *_a: Any, **_kw: Any) -> ScoresQuery:
        return self

    def in_(self, *_a: Any, **_kw: Any) -> ScoresQuery:
        return self

    def order(self, *_a: Any, **_kw: Any) -> ScoresQuery:
        return self

    def gte(self, _col: str, value: int) -> ScoresQuery:
        # A plain (non-Pending-aware) floor.
        self._floor = value
        return self

    def or_(self, expr: str, *_a: Any, **_kw: Any) -> ScoresQuery:
        # ``_apply_score_floor`` emits "…,score.gte.N" — the floor that exempts
        # Pending (non-``complete``) rows.
        m = _SCORE_GTE_RE.search(expr)
        if m:
            self._floor = int(m.group(1))
            self._exempt_pending = True
        return self

    def execute(self) -> FakeResponse:
        rows = self._rows
        if self._floor is not None:
            if self._exempt_pending:
                rows = [
                    r
                    for r in rows
                    if r.get("scoring_status") != "complete" or r["score"] >= self._floor
                ]
            else:
                rows = [r for r in rows if r["score"] >= self._floor]
        return FakeResponse(list(rows), count=len(rows))


class JobsQuery:
    """Emulates the ``jobs`` re-fetch: return the rows for the requested ids."""

    def __init__(self, postings: dict[str, dict[str, Any]]) -> None:
        self._postings = postings
        self._ids: list[str] = []

    def select(self, *_a: Any, **_kw: Any) -> JobsQuery:
        return self

    def in_(self, _col: str, ids: list[str]) -> JobsQuery:
        self._ids = ids
        return self

    def eq(self, *_a: Any, **_kw: Any) -> JobsQuery:
        return self

    def is_(self, *_a: Any, **_kw: Any) -> JobsQuery:
        return self

    def ilike(self, *_a: Any, **_kw: Any) -> JobsQuery:
        return self

    def or_(self, *_a: Any, **_kw: Any) -> JobsQuery:
        return self

    def execute(self) -> FakeResponse:
        return FakeResponse([self._postings[i] for i in self._ids if i in self._postings])


def two_query_supabase(
    scores: list[dict[str, Any]], jobs: dict[str, dict[str, Any]]
) -> MagicMock:
    """A supabase stub for the two-query list path: ``scores`` rows (floored)
    and ``jobs`` postings (keyed by id, returned in ``.in_`` order)."""
    sb = MagicMock()

    def _table(name: str) -> Any:
        if name == "scores":
            return ScoresQuery(scores)
        if name == "jobs":
            return JobsQuery(jobs)
        return JobsQuery({})

    sb.table.side_effect = _table
    return sb
