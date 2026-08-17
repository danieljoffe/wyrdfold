"""#813: the two-query list paths rank a BOUNDED WINDOW, and that window has
to be the head of the list rather than a sample of the candidate set.

PostgREST caps any single response at 1,000 rows. These paths used to select
their candidate ``scores`` rows unbounded and rank whatever came back, so on a
target above the cap they ranked, totalled and paginated an arbitrary sample —
prod measured 1,000 of 7,086 live rows on one target, and
``search=frontend&country=US`` returned 66 rows where the fixed path returns 124.

The window is still bounded. What these pin is that it is drawn
  * in the final ranking's order (so it holds the rows that actually rank), and
  * after the selective posting filters (so it isn't spent on rows that cannot
    match).

Each test seeds the candidate rows in an ADVERSARIAL order — worst-first, or
matches-last — so a window that ignored either property would visibly fail.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from app.config import settings
from app.routers import jobs as jobs_mod
from app.routers.jobs import (
    _list_jobs_for_target_two_query,
    _rank_graded_first,
)
from tests.support.fake_supabase import two_query_supabase

pytestmark = pytest.mark.asyncio


def _score_row(
    jid: str,
    *,
    score: int,
    graded: bool,
    first_seen: str,
    title: str = "Engineer",
    company: str = "Acme",
) -> dict[str, Any]:
    """A candidate ``scores`` row shaped like the live select: the denormalized
    tier/ranking columns plus the ``jobs`` embed the window orders through."""
    row: dict[str, Any] = {
        "job_posting_id": jid,
        "score": score,
        "recency_score": score,
        "scoring_status": "complete" if graded else "stage2",
        "score_breakdown": {},
        "is_graded": graded,
        "job_first_seen_at": first_seen,
        "jobs": {
            "id": jid,
            "title": title,
            "company_name": company,
            "cataloged_at": first_seen,
            "source_posted_at": first_seen,
            "country": "US",
        },
    }
    if graded:
        row["axis_scores"] = {"title_fit": score}
    return row


def _posting(row: dict[str, Any]) -> dict[str, Any]:
    embed = row["jobs"]
    return {
        "id": row["job_posting_id"],
        "title": embed["title"],
        "company_name": embed["company_name"],
        "cataloged_at": embed["cataloged_at"],
        "location": None,
    }


def _supabase(rows: list[dict[str, Any]]) -> Any:
    return two_query_supabase(rows, {r["job_posting_id"]: _posting(r) for r in rows})


async def _list(sb: Any, **kwargs: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "target_id": "t-1",
        "cursor": {},
        "page_size": 5,
        "sort": "score",
        "ascending": False,
        "min_score": None,
        "status": None,
        "company": None,
        "search": None,
        "exclude_terms": [],
        "only_terms": [],
    }
    base.update(kwargs)
    return await _list_jobs_for_target_two_query(sb, **base)


@pytest.fixture(autouse=True)
def _no_decay(monkeypatch: pytest.MonkeyPatch) -> None:
    # These pin WHICH rows the window holds; read-time decay would re-rank the
    # display value on top and obscure that.
    monkeypatch.setattr(settings, "recency_decay_enabled", False)


async def test_window_holds_the_top_of_the_ranking_not_the_first_rows_seen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The window is ordered, so a small window still starts the list correctly.

    Rows are seeded WORST-FIRST: an unordered window (the old behaviour) would
    take the ten lowest-scoring rows and the list would open on them.
    """
    monkeypatch.setattr(jobs_mod, "_CANDIDATE_WINDOW", 10)
    rows = [
        _score_row(f"j{n:02d}", score=n, graded=True, first_seen=f"2026-07-{n:02d}")
        for n in range(1, 26)  # scores 1..25, ascending — worst first
    ]

    result = await _list(_supabase(rows))

    assert [p["id"] for p in result["postings"]] == ["j25", "j24", "j23", "j22", "j21"]


async def test_search_narrows_the_window_instead_of_emptying_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The title search is pushed into the candidate query.

    THE PROD SHAPE (#813): the matches score below the window's cut, so a
    window drawn before the search holds only non-matching rows and the list
    renders empty — the real target returned 66 rows where it should return
    124. Pushed down, the window is drawn from matches and the list is whole.
    """
    monkeypatch.setattr(jobs_mod, "_CANDIDATE_WINDOW", 10)
    rows = [
        _score_row(f"b{n:02d}", score=90 - n, graded=True, first_seen="2026-07-01", title="Backend Engineer")
        for n in range(20)
    ] + [
        _score_row(f"f{n}", score=5 + n, graded=True, first_seen="2026-07-02", title="Frontend Engineer")
        for n in range(6)
    ]

    result = await _list(_supabase(rows), search="frontend", page_size=20)

    assert len(result["postings"]) == 6
    assert all("Frontend" in p["title"] for p in result["postings"])
    assert result["total"] == 6


async def test_graded_rows_get_their_own_window_and_cannot_be_crowded_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Graded and Pending rank on different columns, so each tier is drawn as
    its own window.

    Here every Pending row carries a higher ``recency_score`` than any graded
    row (a keyword placeholder outscoring a real fit is exactly the #47 case).
    A single window ordered by ``recency_score`` would be all-Pending and the
    graded rows — which must rank FIRST — would vanish off the list entirely.
    """
    monkeypatch.setattr(jobs_mod, "_CANDIDATE_WINDOW", 5)
    graded = [
        _score_row(f"g{n}", score=20 + n, graded=True, first_seen="2026-07-01")
        for n in range(3)
    ]
    pending = [
        _score_row(f"p{n:02d}", score=90, graded=False, first_seen=f"2026-07-{n + 1:02d}")
        for n in range(20)
    ]

    result = await _list(_supabase(pending + graded), page_size=5)

    ids = [p["id"] for p in result["postings"]]
    assert ids[:3] == ["g2", "g1", "g0"], f"graded rows lost their window: {ids}"
    assert all(p["pending"] for p in result["postings"][3:])


async def test_pending_window_is_drawn_by_freshness_not_by_placeholder_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Pending tier ranks on posted date, so its window is drawn that way.

    ``recency_score`` and freshness are made to disagree: the freshest rows
    carry the LOWEST placeholder. A window ordered by ``recency_score`` would
    hold the stale ones and the list would open on them.
    """
    monkeypatch.setattr(jobs_mod, "_CANDIDATE_WINDOW", 3)
    rows = [
        # stale but high placeholder
        _score_row("old-hi-a", score=99, graded=False, first_seen="2026-01-01"),
        _score_row("old-hi-b", score=98, graded=False, first_seen="2026-01-02"),
        _score_row("old-hi-c", score=97, graded=False, first_seen="2026-01-03"),
        # fresh but low placeholder
        _score_row("new-lo-a", score=11, graded=False, first_seen="2026-08-15"),
        _score_row("new-lo-b", score=10, graded=False, first_seen="2026-08-16"),
        _score_row("new-lo-c", score=9, graded=False, first_seen="2026-08-17"),
    ]

    result = await _list(_supabase(rows), page_size=3)

    assert [p["id"] for p in result["postings"]] == ["new-lo-c", "new-lo-b", "new-lo-a"]


async def test_non_score_sort_draws_its_window_by_the_sorted_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A company_name sort orders the window through the jobs embed, so page 1
    is the true alphabetical head — not the alphabetical head of a sample."""
    monkeypatch.setattr(jobs_mod, "_CANDIDATE_WINDOW", 3)
    names = ["Zeta", "Yankee", "Xray", "Alpha", "Bravo", "Charlie"]
    rows = [
        _score_row(f"j{i}", score=50, graded=True, first_seen="2026-07-01", company=name)
        for i, name in enumerate(names)
    ]

    result = await _list(_supabase(rows), sort="company_name", ascending=True, page_size=3)

    assert [p["company_name"] for p in result["postings"]] == ["Alpha", "Bravo", "Charlie"]


async def test_saturated_window_is_logged_not_silent(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A bounded window still drops the tail; the point is that it says so.
    The old behaviour was a silent 1,000-row truncation nothing could observe."""
    monkeypatch.setattr(jobs_mod, "_CANDIDATE_WINDOW", 2)
    rows = [
        _score_row(f"j{n}", score=50 - n, graded=True, first_seen="2026-07-01")
        for n in range(5)
    ]

    with caplog.at_level(logging.WARNING):
        await _list(_supabase(rows))

    assert any("candidate window saturated" in r.message for r in caplog.records)


async def test_window_ordering_matches_the_python_ranking_it_feeds() -> None:
    """The window's order and ``_rank_graded_first`` must agree, or the window
    holds rows the ranking then pushes off the page. Graded-before-Pending, and
    within each tier the key the window is drawn by."""
    graded = [_score_row("g-lo", score=30, graded=True, first_seen="2026-07-01")]
    pending = [_score_row("p-hi", score=95, graded=False, first_seen="2026-07-02")]

    ranked = _rank_graded_first(
        graded + pending,
        value=lambda r: (r["recency_score"], r["job_posting_id"]),
        ascending=False,
        pending_value=lambda r: (r["job_first_seen_at"], r["job_posting_id"]),
    )

    # Graded first even though the Pending placeholder is far higher — the same
    # tier split the candidate window draws with ``is_graded``.
    assert [r["job_posting_id"] for r in ranked] == ["g-lo", "p-hi"]
