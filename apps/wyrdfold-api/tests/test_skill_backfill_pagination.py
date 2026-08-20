"""Pagination correctness for the dictionary-skill backfill.

The loop shipped with NO test and a livelock. Under ``only_missing`` the filter
is ``skills_required IS NULL``, so a WRITTEN row drops out of the result set
and the rows behind it shift forward — which is why the offset deliberately
did not advance. That reasoning was right about written rows and wrong about
the rest: a row with no dictionary hit is skipped WITHOUT a write, stays NULL,
and stays at the head of the set. With the offset pinned at 0, every later page
re-read those same rows and the scan stopped making progress the moment enough
of them piled up at the front.

Observed on prod minutes after the release: ``scanned 500, written 0`` while
coverage sat at 0%, on the DEFAULT path.

A mock that just returns a canned page cannot catch this — the bug only exists
in the interaction between the filter and the window. So the fake below is a
real (tiny) table: it applies updates, and re-evaluates ``IS NULL`` on every
read, exactly as PostgREST would.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.qualification.skill_growth import _PAGE, backfill_dictionary_skills

_MATCHES = "We use React and TypeScript daily."
_NO_MATCH = "Make coffee, greet guests, keep the bar tidy."


class _FakeJobsTable:
    """Minimal PostgREST stand-in over an in-memory list of rows."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self._only_null = False
        self._range: tuple[int, int] | None = None
        self._update: dict[str, Any] | None = None
        self._eq_id: str | None = None
        self.read_pages: list[list[str]] = []

    # --- query building (all no-ops except the two that matter) ---
    def select(self, *_a: Any, **_k: Any) -> _FakeJobsTable:
        return self

    def order(self, *_a: Any, **_k: Any) -> _FakeJobsTable:
        return self

    def is_(self, col: str, val: str) -> _FakeJobsTable:
        if col == "skills_required" and val == "null":
            self._only_null = True
        return self

    def range(self, lo: int, hi: int) -> _FakeJobsTable:
        self._range = (lo, hi)
        return self

    def update(self, payload: dict[str, Any]) -> _FakeJobsTable:
        self._update = payload
        return self

    def eq(self, col: str, val: str) -> _FakeJobsTable:
        if col == "id":
            self._eq_id = val
        return self

    async def execute(self) -> Any:
        if self._update is not None:
            for r in self.rows:
                if r["id"] == self._eq_id:
                    r.update(self._update)
            self._update, self._eq_id = None, None
            return type("R", (), {"data": []})()

        # Re-evaluate the filter on EVERY read — this is the whole point.
        pool = [r for r in self.rows if not self._only_null or r["skills_required"] is None]
        lo, hi = self._range or (0, len(pool) - 1)
        page = pool[lo : hi + 1]
        self.read_pages.append([r["id"] for r in page])
        self._only_null, self._range = False, None
        return type("R", (), {"data": [dict(r) for r in page]})()


class _FakeClient:
    def __init__(self, table: _FakeJobsTable) -> None:
        self._table = table

    def table(self, _name: str) -> _FakeJobsTable:
        return self._table


def _rows(*specs: tuple[str, str]) -> list[dict[str, Any]]:
    return [
        {"id": rid, "title": "Role", "description_html": body, "skills_required": None}
        for rid, body in specs
    ]


@pytest.mark.asyncio
async def test_unmatched_rows_do_not_block_the_scan() -> None:
    """THE REGRESSION.

    Deliberately spans MORE THAN ONE PAGE (`_PAGE` = 200). The livelock cannot
    show up inside a single page — every row is read once either way — so a
    small fixture would pass against the broken code and prove nothing. Here a
    full page of unmatched rows sits at the head of the `IS NULL` set: with the
    window pinned at 0 the second page re-reads those same rows and the
    matchable ones behind them are never reached.
    """
    rows = _rows(*[(f"skip-{i}", _NO_MATCH) for i in range(_PAGE + 10)])
    rows += _rows(*[(f"hit-{i}", _MATCHES) for i in range(40)])
    table = _FakeJobsTable(rows)

    result = await backfill_dictionary_skills(
        _FakeClient(table), limit=_PAGE * 3, only_missing=True
    )

    assert result["written"] == 40, result
    assert all(r["skills_required"] for r in rows if r["id"].startswith("hit-"))
    # And it genuinely moved the window rather than re-reading page one.
    assert table.read_pages[0] != table.read_pages[1], "window never advanced"


@pytest.mark.asyncio
async def test_written_rows_are_not_skipped_when_they_leave_the_set() -> None:
    """The original concern, still handled: a written row drops out of the
    `IS NULL` set, so the rows behind it shift forward and must NOT be jumped
    over. Matches lead here, so a naive `offset += len(page)` would lose rows."""
    rows = _rows(("a", _MATCHES), ("b", _MATCHES), ("c", _MATCHES))
    table = _FakeJobsTable(rows)

    result = await backfill_dictionary_skills(_FakeClient(table), limit=3, only_missing=True)

    assert result["written"] == 3
    assert all(r["skills_required"] for r in rows)


@pytest.mark.asyncio
async def test_terminates_when_only_unmatched_rows_remain() -> None:
    """A catalog with nothing extractable must finish, not spin."""
    rows = _rows(("a", _NO_MATCH), ("b", _NO_MATCH))
    table = _FakeJobsTable(rows)

    result = await backfill_dictionary_skills(_FakeClient(table), limit=50, only_missing=True)

    assert result["written"] == 0
    assert result["scanned"] == 2  # each row read once, not re-read to the cap


@pytest.mark.asyncio
async def test_full_rescan_advances_by_the_whole_page() -> None:
    """`only_missing=false` has no filter, so nothing leaves the set and the
    window advances normally — including over rows that were just written."""
    rows = _rows(("a", _MATCHES), ("b", _NO_MATCH), ("c", _MATCHES))
    table = _FakeJobsTable(rows)

    result = await backfill_dictionary_skills(_FakeClient(table), limit=3, only_missing=False)

    assert result["scanned"] == 3
    assert result["written"] == 2


# --- coverage metric: the 1,000-row clamp ------------------------------------


class _FakeCoverageTable:
    """Models the PostgREST behaviour that broke the metric: a response is
    CLAMPED to 1,000 rows however many were requested."""

    CLAMP = 1000

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self._range: tuple[int, int] | None = None
        self.pages_read = 0

    def select(self, *_a: Any, **_k: Any) -> _FakeCoverageTable:
        return self

    def is_(self, *_a: Any, **_k: Any) -> _FakeCoverageTable:
        return self

    @property
    def not_(self) -> _FakeCoverageTable:
        # Production writes `.not_.is_(...)` — ATTRIBUTE access, then a call.
        # A plain method here would make that chain raise, which the caller's
        # `except Exception` would swallow into a misleading warning.
        return self

    def order(self, *_a: Any, **_k: Any) -> _FakeCoverageTable:
        return self

    def limit(self, n: int) -> _FakeCoverageTable:
        self._range = (0, min(n, self.CLAMP) - 1)
        return self

    def range(self, lo: int, hi: int) -> _FakeCoverageTable:
        self._range = (lo, min(hi, lo + self.CLAMP - 1))
        return self

    async def execute(self) -> Any:
        lo, hi = self._range or (0, self.CLAMP - 1)
        page = self.rows[lo : hi + 1]
        self.pages_read += 1
        self._range = None
        return type("R", (), {"data": page})()


class _CoverageClient:
    def __init__(self, jobs: _FakeCoverageTable) -> None:
        self._jobs = jobs

    def table(self, name: str) -> Any:
        if name == "jobs":
            return self._jobs
        # `scores` / `search_events` feed the other two candidate generators;
        # empty is fine, this test is about coverage.
        return _FakeCoverageTable([])


@pytest.mark.asyncio
async def test_coverage_counts_the_whole_catalog_not_the_first_clamped_page() -> None:
    """THE REGRESSION. `.limit(20000)` returns 1,000 rows, not 20,000.

    Unpaginated, the metric described the first 1,000 jobs of a much larger
    catalog. Here every job PAST that first page has skills and every job
    inside it does not — so a clamped scan reports 0% while the true figure is
    substantial. A blind-spot monitor that always reads zero can never alarm.
    """
    from app.services.qualification.skill_growth import vocabulary_candidates

    rows = [{"role_family": "engineering", "skills_required": None} for _ in range(1000)] + [
        {"role_family": "engineering", "skills_required": ["react"]} for _ in range(1000)
    ]
    jobs = _FakeCoverageTable(rows)

    out = await vocabulary_candidates(_CoverageClient(jobs), limit=5)

    cov = {c["role_family"]: c for c in out["family_coverage"]}
    assert cov["engineering"]["jobs"] == 2000, cov
    assert cov["engineering"]["with_skills_pct"] == 50.0, cov
    assert jobs.pages_read > 1, "never paged past the clamp"
