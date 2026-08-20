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


# ── The window's order IS the list's order (#815) ───────────────────────────
# Postgres and Python do not collate text the same way: glibc ignores leading
# punctuation at the primary level, Python's codepoint order does not. So the
# two-query path re-sorting the window in Python meant `/jobs?sort=title`
# ordered differently depending only on whether a chip forced this path instead
# of the RPC. Measured on prod (title ASC, 1,000-row window): the two agreed for
# 21 rows, then `.NET & React Full Stack Developer` and `(1508) Senior
# Fullstack Engineer` swapped.
#
# `__db_order` pins the order Postgres returns — the thing Python can't
# reproduce, and without which the fake would agree with the old code by
# construction.


def _collation_rows() -> list[dict[str, Any]]:
    """Titles whose Postgres order is NOT any Python sort of the same values."""
    titles = [
        ".NET & React Full Stack Developer",       # glibc: punctuation ignored
        "(1508) Senior Fullstack Engineer",
        "abridge Platform Engineer",
        "Accenture Delivery Engineer",
    ]
    rows = []
    for i, title in enumerate(titles):
        row = _score_row(f"j{i}", score=50, graded=True, first_seen="2026-07-01", title=title)
        row["__db_order"] = i          # the DB's collation order
        rows.append(row)
    return rows


async def test_title_sort_preserves_the_windows_order_not_pythons() -> None:
    """The page must come out in the order the DB returned, byte for byte."""
    rows = _collation_rows()
    db_order = [r["jobs"]["title"] for r in rows]
    # Python would order these differently — that is the whole point.
    assert sorted(db_order, key=str.casefold) != db_order

    result = await _list(_supabase(rows), sort="title", ascending=True, page_size=10)

    assert [p["title"] for p in result["postings"]] == db_order


async def test_window_order_survives_a_post_fetch_filter_dropping_rows() -> None:
    """Post-fetch filters only DROP rows, so the surviving order still holds —
    this is what makes preserving the window index safe."""
    rows = _collation_rows()
    sb = _supabase(rows)
    result = await _list(
        sb, sort="title", ascending=True, page_size=10, only_terms=["remote"]
    )
    kept = [p["title"] for p in result["postings"]]
    expected = [r["jobs"]["title"] for r in rows if r["jobs"]["title"] in kept]
    assert kept == expected


async def test_archived_view_still_sorts_in_python() -> None:
    """The archived view selects without the jobs embed, so its window can only
    be ordered by id — there the Python key IS the sort, and must stay."""
    rows = [
        _score_row("j0", score=50, graded=True, first_seen="2026-07-01", company="Zeta"),
        _score_row("j1", score=50, graded=True, first_seen="2026-07-01", company="alpha"),
    ]
    for r in rows:
        r.pop("jobs")  # archived view: no embed
    # ``archived_at`` is what makes _fetch_jobs_chunked keep a row in the
    # archived view — it overwrites ``status`` from user_jobs, so the flag on
    # the posting is what counts (the 30d sweep's output, UX/IA §5 Stage 1).
    postings = {
        "j0": {"id": "j0", "title": "a", "company_name": "Zeta",
               "archived_at": "2026-07-20T00:00:00+00:00"},
        "j1": {"id": "j1", "title": "b", "company_name": "alpha",
               "archived_at": "2026-07-21T00:00:00+00:00"},
    }
    sb = two_query_supabase(rows, postings)
    result = await _list_jobs_for_target_two_query(
        sb, target_id="t-1", cursor={}, page_size=10, sort="company_name",
        ascending=True, min_score=None, status="archived", company=None,
        search=None, exclude_terms=[], only_terms=[],
    )
    # casefold order: alpha before Zeta (codepoint order would invert it).
    assert [p["company_name"] for p in result["postings"]] == ["alpha", "Zeta"]


# ── The Posted sort orders the PROVIDER'S date, nulls last ──────────────────
# The column displays ``source_posted_at`` (the employer's own date) but the
# sort keyed on ``cataloged_at`` (when WE catalogued the listing). Measured on
# prod: sorting Posted descending, 281 of 999 adjacent pairs were out of order
# by the date on screen, and page 1 read 07-30, 08-03, 08-14, 02-10, 08-11.
#
# ~4% of live listings carry no provider date. Those sort LAST in BOTH
# directions — "unknown" must not lead the list just because the arrow flipped.


def _posted_row(jid: str, *, posted: str | None, cataloged: str) -> dict[str, Any]:
    row = _score_row(jid, score=50, graded=True, first_seen=cataloged)
    row["jobs"]["source_posted_at"] = posted
    row["jobs"]["cataloged_at"] = cataloged
    return row


def _posted_supabase(rows: list[dict[str, Any]]) -> Any:
    postings = {}
    for r in rows:
        p = _posting(r)
        p["source_posted_at"] = r["jobs"]["source_posted_at"]
        p["cataloged_at"] = r["jobs"]["cataloged_at"]
        postings[r["job_posting_id"]] = p
    return two_query_supabase(rows, postings)


def _posted_fixture() -> list[dict[str, Any]]:
    """Catalogue order deliberately disagrees with posted order — the exact
    shape that made the column look scrambled."""
    return [
        _posted_row("newest", posted="2026-08-16", cataloged="2026-08-01"),
        _posted_row("middle", posted="2026-06-01", cataloged="2026-08-17"),
        _posted_row("oldest", posted="2026-02-10", cataloged="2026-08-10"),
        _posted_row("unknown", posted=None, cataloged="2026-08-15"),
    ]


async def test_posted_sort_orders_by_the_provider_date_not_our_catalog_date() -> None:
    rows = _posted_fixture()
    result = await _list(_posted_supabase(rows), sort="posted_at", page_size=10)
    # Catalogue order would be: middle, unknown, oldest, newest — nothing like it.
    assert [p["id"] for p in result["postings"]][:3] == ["newest", "middle", "oldest"]


async def test_posted_sort_puts_undated_listings_last_descending() -> None:
    rows = _posted_fixture()
    result = await _list(_posted_supabase(rows), sort="posted_at", ascending=False, page_size=10)
    assert [p["id"] for p in result["postings"]][-1] == "unknown"


async def test_posted_sort_puts_undated_listings_last_ascending_too() -> None:
    """The direction flips the dates, never the unknowns — an empty value is
    not "oldest", and a user reversing the sort must not land on 700 blanks."""
    rows = _posted_fixture()
    result = await _list(_posted_supabase(rows), sort="posted_at", ascending=True, page_size=10)
    ids = [p["id"] for p in result["postings"]]
    assert ids[0] == "oldest", ids
    assert ids[-1] == "unknown", ids


# ── Undated listings get their own window (#825) ────────────────────────────
# They sort last in both directions by design. But the window is bounded, so on
# a target whose DATED candidates already fill it the undated ones fall outside
# entirely and become unreachable on this sort rather than merely last — 293
# such rows on the measured prod target, whose 7,037 candidates overflow a
# 1,000-row window. Their own window puts them at the tail AND keeps them
# reachable, exactly like the graded/Pending split.


async def test_undated_listings_survive_a_window_full_of_dated_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE prod shape: more dated rows than the window holds, plus a few
    undated. Before, the undated were simply gone from this sort."""
    monkeypatch.setattr(jobs_mod, "_CANDIDATE_WINDOW", 5)
    dated = [
        _posted_row(f"d{n:02d}", posted=f"2026-07-{n + 1:02d}", cataloged="2026-08-01")
        for n in range(12)  # more than twice the window
    ]
    undated = [
        _posted_row(f"u{n}", posted=None, cataloged=f"2026-08-{n + 10:02d}") for n in range(3)
    ]

    result = await _list(_posted_supabase(dated + undated), sort="posted_at", page_size=20)

    ids = [p["id"] for p in result["postings"]]
    assert [i for i in ids if i.startswith("u")] == ["u2", "u1", "u0"], f"undated lost: {ids}"
    # …and still after every dated row, not interleaved.
    assert ids.index("u0") > max(ids.index(i) for i in ids if i.startswith("d"))


async def test_undated_stay_last_when_the_direction_flips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(jobs_mod, "_CANDIDATE_WINDOW", 5)
    dated = [
        _posted_row(f"d{n:02d}", posted=f"2026-07-{n + 1:02d}", cataloged="2026-08-01")
        for n in range(12)
    ]
    undated = [_posted_row("u0", posted=None, cataloged="2026-08-10")]

    result = await _list(
        _posted_supabase(dated + undated), sort="posted_at", ascending=True, page_size=20
    )

    ids = [p["id"] for p in result["postings"]]
    assert ids[-1] == "u0", ids


async def test_other_sorts_do_not_split_on_the_posted_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The split belongs to posted_at alone — a company sort must return each
    row once, not once per tier."""
    monkeypatch.setattr(jobs_mod, "_CANDIDATE_WINDOW", 50)
    rows = [
        _posted_row("a", posted="2026-07-01", cataloged="2026-08-01"),
        _posted_row("b", posted=None, cataloged="2026-08-02"),
    ]
    result = await _list(
        _posted_supabase(rows), sort="company_name", ascending=True, page_size=20
    )
    ids = [p["id"] for p in result["postings"]]
    assert sorted(ids) == ["a", "b"], ids
    assert len(ids) == len(set(ids))
