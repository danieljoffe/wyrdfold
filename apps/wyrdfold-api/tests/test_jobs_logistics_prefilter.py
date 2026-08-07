"""#654 — logistics filtering moves ahead of pagination.

remote/salary/country used to be POST-fetch, which forced the list path to
hydrate every candidate (`page_ids = list(by_id.keys())`) just to return one
page: ~11,966 rows over dozens of chunked IN() reads to serve 20, i.e. the
2.1-2.7s `/jobs?...&min_salary=` requests measured on prod.

The risk in moving it is a SEMANTIC one — the scores-layer predicate must
select exactly what the posting-layer predicate selected. These tests pin
that equivalence directly rather than arguing it.
"""

from __future__ import annotations

import itertools

from app.routers.jobs import (
    _logistics_passes,
    _LogisticsFilter,
    _score_row_passes_logistics,
)


def _pair(*, log: dict | None, salary: dict | None) -> tuple[dict, dict]:
    """The same job expressed as (hydrated posting, scores row).

    The posting carries salary as top-level jobs columns; the scores row
    carries them on the embedded `jobs` relation. Same underlying data, the
    two shapes the two layers see.
    """
    posting = {"id": "j1", "logistics_filters": log, **(salary or {})}
    row = {
        "job_posting_id": "j1",
        "logistics_filters": log,
        "jobs": {"id": "j1", **(salary or {})},
    }
    return posting, row


# Every meaningful combination of the inputs the predicate branches on.
_LOGS = [
    None,
    {},
    {"remote_status": "remote"},
    {"remote_status": "onsite"},
    {"salary_max": 120_000},
    {"salary_max": 250_000},
    {"remote_status": "remote", "location_country": "US"},
    {"location_country": "CA"},
    {"remote_status": "remote", "salary_max": 90_000, "location_country": "US"},
]
_SALARIES = [
    None,
    {"salary_currency": "USD", "salary_period": "yearly", "salary_max": 300_000},
    {"salary_currency": "USD", "salary_period": "yearly", "salary_max": 100_000},
    # min-only: the predicate falls back to salary_min when max is absent.
    {"salary_currency": "USD", "salary_period": "yearly", "salary_min": 180_000},
    # Non-USD / non-yearly must NOT be read as a yearly-USD bound.
    {"salary_currency": "EUR", "salary_period": "yearly", "salary_max": 300_000},
    {"salary_currency": "USD", "salary_period": "hourly", "salary_max": 300_000},
]
_FILTERS = [
    _LogisticsFilter(remote_only=True),
    _LogisticsFilter(min_salary=150_000),
    _LogisticsFilter(country="US"),
    _LogisticsFilter(remote_only=True, min_salary=150_000, country="US"),
]


def test_scores_layer_predicate_matches_posting_layer_exactly() -> None:
    """The differential gate: across the full matrix of (filter x logistics
    payload x salary shape x include_unknown), both layers must agree on
    every single row. A divergence here would silently change which jobs a
    filter returns depending on which code path a request happened to take."""
    checked = 0
    for f, log, salary, include_unknown in itertools.product(
        _FILTERS, _LOGS, _SALARIES, (True, False)
    ):
        posting, row = _pair(log=log, salary=salary)
        old = _logistics_passes(posting, f, include_unknown_salary=include_unknown)
        new = _score_row_passes_logistics(row, f, include_unknown_salary=include_unknown)
        assert old == new, (
            f"DIVERGENCE: filter={f} log={log} salary={salary} "
            f"include_unknown={include_unknown} -> posting={old} scores={new}"
        )
        checked += 1
    # Guard the guard: a matrix that silently collapsed would pass vacuously.
    assert checked == len(_FILTERS) * len(_LOGS) * len(_SALARIES) * 2 == 432


def test_matrix_actually_exercises_both_outcomes() -> None:
    """A predicate stuck at True (or False) would satisfy the equivalence test
    trivially. Prove the matrix produces a real mix."""
    results = [
        _score_row_passes_logistics(
            _pair(log=log, salary=salary)[1], f, include_unknown_salary=inc
        )
        for f, log, salary, inc in itertools.product(_FILTERS, _LOGS, _SALARIES, (True, False))
    ]
    assert any(results) and not all(results)


def test_deterministic_columns_win_over_grader_logistics() -> None:
    """Structured jobs.salary_* is authoritative; the grader's
    logistics_filters.salary_max is only the fallback. Pinned because the
    scores layer reads them from a DIFFERENT place (the embedded relation)
    than the posting layer does."""
    f = _LogisticsFilter(min_salary=150_000)
    # Deterministic says 300k (pass), grader says 90k (would fail) -> pass.
    _, row = _pair(
        log={"salary_max": 90_000},
        salary={"salary_currency": "USD", "salary_period": "yearly", "salary_max": 300_000},
    )
    assert _score_row_passes_logistics(row, f, include_unknown_salary=False) is True

    # No structured salary -> grader's value is used -> 90k fails the floor.
    _, row2 = _pair(log={"salary_max": 90_000}, salary=None)
    assert _score_row_passes_logistics(row2, f, include_unknown_salary=False) is False


def test_unknown_salary_respects_the_include_pref() -> None:
    """The one case where the same row legitimately flips on a preference."""
    f = _LogisticsFilter(min_salary=150_000)
    _, row = _pair(log={}, salary=None)
    assert _score_row_passes_logistics(row, f, include_unknown_salary=False) is False
    assert _score_row_passes_logistics(row, f, include_unknown_salary=True) is True


def test_missing_embed_is_not_silently_treated_as_no_salary() -> None:
    """A scores row fetched WITHOUT the jobs embed (archived view) has no
    deterministic columns. The caller must keep such rows on the post-fetch
    path — this test documents why that branch exists: read here, the row
    looks like 'unknown salary' and would be dropped under the strict
    default."""
    f = _LogisticsFilter(min_salary=150_000)
    bare = {"job_posting_id": "j1", "logistics_filters": {}}
    assert _score_row_passes_logistics(bare, f, include_unknown_salary=False) is False


def test_mixed_embed_shapes_must_not_engage_the_prefilter() -> None:
    """Guard on the guard (found in self-review of #655).

    If a result set ever mixes rows that carry the jobs embed with rows that
    don't, the pre-filter must NOT engage: an embed-less row has no
    deterministic salary columns, so evaluating it at the scores layer reads
    as 'unknown salary' and the strict default would silently drop a job the
    post-fetch path would have kept. The engage-condition is therefore ALL,
    not ANY — the cost of being wrong is a slower request, not a lost row.
    """
    from app.routers.jobs import _embedded_jobs_field

    with_embed = {"job_posting_id": "a", "jobs": {"id": "a"}, "logistics_filters": {}}
    without_embed = {"job_posting_id": "b", "logistics_filters": {}}
    rows = [with_embed, without_embed]

    engages = all(_embedded_jobs_field(r, "id") is not None for r in rows)
    assert engages is False, "mixed shapes must fall back to post-fetch"

    # And the reason it matters: read at the scores layer, the embed-less row
    # would be dropped under the strict default.
    f = _LogisticsFilter(min_salary=150_000)
    assert _score_row_passes_logistics(without_embed, f, include_unknown_salary=False) is False
