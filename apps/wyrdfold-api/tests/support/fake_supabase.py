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
_OR_TITLE_RE = re.compile(r"title\.ilike\.\*([^*,)]+)\*")


class FakeResponse:
    def __init__(self, data: Any, count: int | None = None) -> None:
        self.data = data
        self.count = count


def _row_is_graded(row: dict[str, Any]) -> bool:
    """The ``scores.is_graded`` column for a fixture row.

    Honoured when a fixture sets it (so a test can model a row where the
    denormalized column and the grade signal disagree). Otherwise derived from
    ``axis_scores`` / ``fit_reasoning`` — the same signal ``_is_pending`` uses,
    and the invariant the real column upholds: prod carries ZERO rows where
    ``is_graded`` and ``axis_scores IS NOT NULL`` disagree, in either direction
    (verified 2026-08-17 on the 18,972-row target behind #813).
    """
    flag = row.get("is_graded")
    if isinstance(flag, bool):
        return flag
    axes = row.get("axis_scores")
    if isinstance(axes, dict) and axes:
        return True
    reasoning = row.get("fit_reasoning")
    return isinstance(reasoning, str) and bool(reasoning.strip())


def _embedded(row: dict[str, Any], field: str) -> Any:
    embed = row.get("jobs")
    return embed.get(field) if isinstance(embed, dict) else None


def _cell(row: dict[str, Any], column: str) -> Any:
    """Read ``column`` the way PostgREST would — ``jobs(x)`` / ``jobs.x`` name
    the embedded relation, anything else is a scores column."""
    if column.startswith("jobs(") and column.endswith(")"):
        return _embedded(row, column[5:-1])
    if column.startswith("jobs."):
        return _embedded(row, column[5:])
    return row.get(column)


class ScoresQuery:
    """Emulates the ``scores`` candidate query: the server-side score floor,
    the pushed-down posting filters, ORDER BY and the bounded window (#813).

    Order and range are modelled for real, because the whole point of the
    candidate window is WHICH rows come back — a fake that ignored
    ``.order()``/``.range()`` would report a bounded window as correct no
    matter what order the code asked for.
    """

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self._floor: int | None = None
        self._exempt_pending = False
        self._graded: bool | None = None
        self._company: str | None = None
        self._title_terms: list[str] = []
        self._order: list[tuple[str, bool]] = []
        self._range: tuple[int, int] | None = None

    def select(self, *_a: Any, **_kw: Any) -> ScoresQuery:
        return self

    def eq(self, col: str = "", value: Any = None, *_a: Any, **_kw: Any) -> ScoresQuery:
        # target_id / excluded are modelled by which rows the test seeds; the
        # tier split and the pushed-down company filter are not.
        if col == "is_graded":
            self._graded = bool(value)
        elif col == "jobs.company_name":
            self._company = value
        return self

    def in_(self, *_a: Any, **_kw: Any) -> ScoresQuery:
        return self

    def ilike(self, col: str = "", pattern: str = "", *_a: Any, **_kw: Any) -> ScoresQuery:
        if col == "jobs.title":
            self._title_terms = [pattern.strip("%").lower()]
        return self

    def order(self, column: str, *, desc: bool = False, **_kw: Any) -> ScoresQuery:
        self._order.append((column, desc))
        return self

    def range(self, start: int, end: int) -> ScoresQuery:
        self._range = (start, end)
        return self

    def is_(self, *_a: Any, **_kw: Any) -> ScoresQuery:
        # The scores-layer liveness join (`jobs.archived_at is null`, ...)
        # — a fluent no-op here; liveness outcomes are modelled by which
        # rows the test seeds. See _scores_live_join.
        return self

    @property
    def not_(self) -> ScoresQuery:
        # `.not_.is_("jobs.is_us", "false")` — same no-op treatment.
        return self

    def gte(self, _col: str, value: int) -> ScoresQuery:
        # A plain (non-Pending-aware) floor.
        self._floor = value
        return self

    def or_(self, expr: str, *_a: Any, **kw: Any) -> ScoresQuery:
        if kw.get("reference_table") == "jobs" or (_a and _a[0] == "jobs"):
            # The multi-word search, pushed into the embed as an OR of
            # ``title.ilike.*token*`` legs.
            self._title_terms = [m.lower() for m in _OR_TITLE_RE.findall(expr)]
            return self
        # ``_apply_score_floor`` emits "…,recency_score.gte.N" — the floor that
        # exempts Pending (ungraded) rows.
        m = _SCORE_GTE_RE.search(expr)
        if m:
            self._floor = int(m.group(1))
            self._exempt_pending = True
        return self

    def _ordered(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # Successive stable sorts, least-significant key first. NULLs are
        # partitioned to the end on every key, matching the NULLS LAST the
        # candidate window asks for in both directions.
        for column, desc in reversed(self._order):
            present = [r for r in rows if _cell(r, column) is not None]
            absent = [r for r in rows if _cell(r, column) is None]
            present.sort(key=lambda r: _cell(r, column), reverse=desc)
            rows = present + absent
        return rows

    async def execute(self) -> FakeResponse:
        rows = list(self._rows)
        if self._floor is not None:
            if self._exempt_pending:
                rows = [
                    r
                    for r in rows
                    if not _row_is_graded(r) or (r.get("recency_score") or r["score"]) >= self._floor
                ]
            else:
                rows = [r for r in rows if r["score"] >= self._floor]
        if self._graded is not None:
            rows = [r for r in rows if _row_is_graded(r) is self._graded]
        if self._company is not None:
            rows = [r for r in rows if _embedded(r, "company_name") == self._company]
        if self._title_terms:
            rows = [
                r
                for r in rows
                if any(t in (_embedded(r, "title") or "").lower() for t in self._title_terms)
            ]
        rows = self._ordered(rows)
        if self._range is not None:
            start, end = self._range
            rows = rows[start : end + 1]
        return FakeResponse(rows, count=len(rows))


class JobsQuery:
    """Emulates the ``jobs`` re-fetch: return the rows for the requested ids."""

    def __init__(self, postings: dict[str, dict[str, Any]]) -> None:
        self._postings = postings
        self._ids: list[str] = []
        self._company: str | None = None
        self._title_terms: list[str] = []

    def select(self, *_a: Any, **_kw: Any) -> JobsQuery:
        return self

    def in_(self, _col: str, ids: list[str]) -> JobsQuery:
        self._ids = ids
        return self

    def eq(self, col: str = "", value: Any = None, *_a: Any, **_kw: Any) -> JobsQuery:
        if col == "company_name":
            self._company = value
        return self

    def is_(self, *_a: Any, **_kw: Any) -> JobsQuery:
        return self

    @property
    def not_(self) -> JobsQuery:
        # `.not_.is_("is_us","false")` — the #60 non-US display gate. This fake
        # keys off ids only, so negation is a no-op that keeps the chain fluent.
        return self

    def ilike(self, col: str = "", pattern: str = "", *_a: Any, **_kw: Any) -> JobsQuery:
        # The single-token title search (``_apply_title_search``). Modelled for
        # real so a test can tell "the window never held a match" apart from
        # "the re-fetch dropped it".
        if col == "title":
            self._title_terms = [pattern.strip("%").lower()]
        return self

    def or_(self, expr: str = "", *_a: Any, **_kw: Any) -> JobsQuery:
        # The multi-token title search — OR across ``title.ilike.*token*`` legs.
        terms = _OR_TITLE_RE.findall(expr)
        if terms:
            self._title_terms = [t.lower() for t in terms]
        return self

    async def execute(self) -> FakeResponse:
        rows = [self._postings[i] for i in self._ids if i in self._postings]
        if self._company is not None:
            rows = [r for r in rows if r.get("company_name") == self._company]
        if self._title_terms:
            rows = [
                r
                for r in rows
                if any(t in (r.get("title") or "").lower() for t in self._title_terms)
            ]
        return FakeResponse(rows)


def two_query_supabase(scores: list[dict[str, Any]], jobs: dict[str, dict[str, Any]]) -> MagicMock:
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
