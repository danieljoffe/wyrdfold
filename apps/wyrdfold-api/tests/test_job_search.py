"""Public job search (#467) — ranking logic + the corpus gate + the endpoint.

The crux is the #467 acceptance example: for "frontend developer", a
"Frontend Engineer" must outrank a "Backend Developer" (developer ≈ engineer, but
frontend ≠ backend) — even when the backend role is more recent. And the search
must hit only the LIVE, US corpus and leak no match score / no JD body.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.job_search import JobSearchResult
from app.services import job_search


def _mock_supabase(rows: list[dict[str, Any]]) -> MagicMock:
    """An async supabase stub whose query builder is self-returning; the awaited
    ``.execute()`` yields ``.data == rows`` regardless of the filters applied
    (filters asserted separately via call_args)."""
    qb = MagicMock(name="query_builder")
    for meth in ("select", "is_", "or_", "order", "limit", "eq", "gte"):
        getattr(qb, meth).return_value = qb
    qb.not_ = qb  # ``.not_`` is a property, not a call → same builder
    result = MagicMock()
    result.data = rows
    qb.execute = AsyncMock(return_value=result)
    supabase = MagicMock(name="service_role")
    supabase.table.return_value = qb
    return supabase


def _row(rid: str, title: str, created_at: str) -> dict[str, Any]:
    ts = created_at if "T" in created_at else f"{created_at}T00:00:00+00:00"
    return {
        "id": rid,
        "title": title,
        "company_name": f"Co-{rid}",
        "location": "Remote",
        "salary_text": None,
        "absolute_url": f"https://example.com/{rid}",
        "cataloged_at": ts,
        "created_at": ts,
    }


# --- tokenize / synonyms ---------------------------------------------------


def test_tokenize_sanitizes_and_dedupes() -> None:
    # Punctuation stripped (neutralizes PostgREST metachars), case-folded, deduped.
    assert job_search._tokenize("Frontend, Frontend  *Dev*!") == ["frontend", "dev"]
    assert job_search._tokenize("   ") == []
    assert job_search._tokenize("),(*:") == []


def test_developer_and_engineer_are_synonyms() -> None:
    g = job_search._groups
    assert g(["developer"]) == g(["engineer"]) == {"engineer"}
    assert g(["fe"]) == g(["front-end"]) == {"frontend"}
    # Distinct roles stay distinct.
    assert g(["frontend"]) != g(["backend"])
    # A novel keyword is its own group.
    assert g(["kubernetes"]) == {"kubernetes"}


# --- ranking (the #467 acceptance example) ---------------------------------


async def test_frontend_engineer_outranks_backend_developer() -> None:
    # Backend Developer is the MOST RECENT — role match must still beat recency.
    rows = [
        _row("fe-eng", "Frontend Engineer", "2026-01-02"),
        _row("be-dev", "Backend Developer", "2026-01-09"),
        _row("sr-fe", "Senior Frontend Developer", "2026-01-01"),
    ]
    supabase = _mock_supabase(rows)

    results, has_more = await job_search.search_jobs(supabase, q="frontend developer")
    assert has_more is False  # only 3 matches, one page
    ids = [r.id for r in results]

    # Both frontend roles (overlap 2) rank above the backend role (overlap 1)...
    assert ids.index("fe-eng") < ids.index("be-dev")
    assert ids.index("sr-fe") < ids.index("be-dev")
    # ...and among equal-overlap, the more recent wins.
    assert ids.index("fe-eng") < ids.index("sr-fe")
    # The off-role match is dead last despite being newest.
    assert ids[-1] == "be-dev"


async def test_blank_and_garbage_queries_split_between_browse_and_empty() -> None:
    # #834 changed the blank-q contract: whitespace-only now BROWSES the pool
    # (the browse tests below pin that), while a real query that sanitizes
    # away to nothing still returns empty without a DB round trip.
    supabase = _mock_supabase([_row("x", "Anything", "2026-01-01")])
    assert await job_search.search_jobs(supabase, q="*,()") == ([], False)
    supabase.table.assert_not_called()


async def test_limit_is_clamped_to_max_page_size() -> None:
    rows = [_row(str(i), "Frontend Engineer", "2026-01-15") for i in range(1, 40)]
    supabase = _mock_supabase(rows)
    results, has_more = await job_search.search_jobs(supabase, q="frontend", limit=999)
    assert len(results) == job_search.MAX_PAGE_SIZE
    assert has_more is True  # 39 matches > one clamped page of 25


async def test_pagination_offset_and_has_more() -> None:
    # 30 identical-overlap matches → deterministic order by recency.
    rows = [
        _row(f"j{i:02d}", "Frontend Engineer", f"2026-01-{i:02d}T00:00:00+00:00")
        for i in range(1, 31)
    ]
    supabase = _mock_supabase(rows)

    page1, more1 = await job_search.search_jobs(supabase, q="frontend", limit=20, offset=0)
    page2, more2 = await job_search.search_jobs(supabase, q="frontend", limit=20, offset=20)

    assert len(page1) == 20 and more1 is True  # 30 > 20
    assert len(page2) == 10 and more2 is False  # last page, nothing after
    # No overlap between pages, and page 2 continues the ranked order.
    assert {r.id for r in page1}.isdisjoint(r.id for r in page2)


# --- corpus gate + filter --------------------------------------------------


async def test_search_applies_live_us_gate_and_synonym_filter() -> None:
    supabase = _mock_supabase([])
    await job_search.search_jobs(supabase, q="frontend dev")
    qb = supabase.table.return_value

    # Live + US gate (archived_at IS NULL, purged_at IS NULL, is_us IS NOT FALSE).
    is_calls = {args for args, _ in qb.is_.call_args_list}
    assert ("archived_at", "null") in is_calls
    assert ("purged_at", "null") in is_calls
    assert ("is_us", "false") in is_calls  # applied via .not_.is_(...)

    # The ILIKE OR expands each query token to its synonym forms.
    or_filter = qb.or_.call_args[0][0]
    for form in ("frontend", "front-end", "fe", "developer", "engineer", "swe"):
        assert f"title.ilike.*{form}*" in or_filter


def test_result_row_carries_no_score_and_no_jd_body() -> None:
    # The public model has no score / no description_html field at all.
    fields = set(JobSearchResult.model_fields)
    assert "score" not in fields
    assert "score_breakdown" not in fields
    assert "description_html" not in fields
    # And the DB projection never selects them.
    assert "score" not in job_search._SEARCH_COLS
    assert "description_html" not in job_search._SEARCH_COLS


# --- endpoint --------------------------------------------------------------


def test_search_endpoint_returns_ranked_results() -> None:
    from fastapi.testclient import TestClient

    from app.dependencies import get_async_service_supabase, verify_api_key_or_jwt
    from app.main import app

    rows = [
        _row("fe", "Frontend Engineer", "2026-01-02"),
        _row("be", "Backend Developer", "2026-01-09"),
    ]
    app.dependency_overrides[get_async_service_supabase] = lambda: _mock_supabase(rows)
    app.dependency_overrides[verify_api_key_or_jwt] = lambda: "test-user"
    try:
        resp = TestClient(app).get("/search?q=frontend+developer")
        assert resp.status_code == 200
        body = resp.json()
        assert body["query"] == "frontend developer"
        assert body["count"] == 2
        assert body["results"][0]["id"] == "fe"  # role match ranks first
        # No match score leaks into the public payload.
        assert "score" not in body["results"][0]
    finally:
        app.dependency_overrides.clear()


def test_search_endpoint_requires_auth() -> None:
    """Abuse control (#467): no anonymous access — the endpoint is gated to
    logged-in sessions while the feature is beta-only."""
    from fastapi.testclient import TestClient

    from app.main import app

    # No auth override → the router-level verify_api_key_or_jwt rejects.
    resp = TestClient(app).get("/search?q=frontend")
    assert resp.status_code in (401, 403)


def test_search_endpoint_still_validates_query_length() -> None:
    from fastapi.testclient import TestClient

    from app.dependencies import get_async_service_supabase, verify_api_key_or_jwt
    from app.main import app

    app.dependency_overrides[get_async_service_supabase] = lambda: MagicMock()
    app.dependency_overrides[verify_api_key_or_jwt] = lambda: "test-user"
    try:
        # #834 made a missing/blank q legal (browse mode) — the length cap
        # is the validation that survives.
        assert TestClient(app).get(f"/search?q={'x' * 121}").status_code == 422
    finally:
        app.dependency_overrides.clear()


# --- filters (location + recency) — #467 fast-follow -----------------------


def _row_loc(rid: str, location: str | None) -> dict[str, Any]:
    r = _row(rid, "Frontend Engineer", "2026-01-15")
    r["location"] = location
    return r


async def test_location_filter_is_case_insensitive_substring() -> None:
    rows = [
        _row_loc("remote", "Remote (US)"),
        _row_loc("sf", "San Francisco, CA"),
        _row_loc("ny", "New York, NY"),
        _row_loc("none", None),
    ]
    supabase = _mock_supabase(rows)
    # "remote" matches "Remote (US)" case-insensitively; nothing else.
    results, _ = await job_search.search_jobs(supabase, q="frontend", location="ReMoTe")
    assert {r.id for r in results} == {"remote"}


async def test_blank_location_is_a_noop() -> None:
    rows = [_row_loc("a", "Remote"), _row_loc("b", "NYC"), _row_loc("c", None)]
    supabase = _mock_supabase(rows)
    results, _ = await job_search.search_jobs(supabase, q="frontend", location="   ")
    assert len(results) == 3  # whitespace-only filter keeps every candidate


async def test_recency_filter_applies_a_posted_at_lower_bound() -> None:
    from datetime import UTC, datetime

    supabase = _mock_supabase([])
    await job_search.search_jobs(supabase, q="frontend", posted_within_days=7)
    qb = supabase.table.return_value
    # R2: the bound rides a PostgREST or_ — provider posted date when known,
    # cataloged time as the fallback — in ONE round-trip.
    assert qb.or_.called
    or_arg = qb.or_.call_args[0][0]
    assert "source_posted_at.gte." in or_arg
    assert "and(source_posted_at.is.null,cataloged_at.gte." in or_arg
    cutoff = or_arg.split("source_posted_at.gte.", 1)[1].split(",", 1)[0]
    delta_days = (datetime.now(UTC) - datetime.fromisoformat(cutoff)).days
    assert 6 <= delta_days <= 8  # ~7 days back


async def test_recency_days_are_clamped_to_the_ceiling() -> None:
    from datetime import UTC, datetime

    supabase = _mock_supabase([])
    await job_search.search_jobs(supabase, q="frontend", posted_within_days=99999)
    or_arg = supabase.table.return_value.or_.call_args[0][0]
    cutoff = or_arg.split("source_posted_at.gte.", 1)[1].split(",", 1)[0]
    delta_days = (datetime.now(UTC) - datetime.fromisoformat(cutoff)).days
    assert delta_days <= job_search.MAX_POSTED_WITHIN_DAYS + 1


async def test_no_recency_filter_skips_the_posted_bound() -> None:
    supabase = _mock_supabase([])
    await job_search.search_jobs(supabase, q="frontend")
    # The title ilike matcher also rides or_ — assert no POSTED bound rode one.
    or_args = [c.args[0] for c in supabase.table.return_value.or_.call_args_list]
    assert not any("source_posted_at.gte." in a for a in or_args)


def test_search_endpoint_forwards_filters_to_the_service(
    monkeypatch,
) -> None:
    from fastapi.testclient import TestClient

    from app.dependencies import get_async_service_supabase, verify_api_key_or_jwt
    from app.main import app
    from app.services import job_search as js

    captured: dict[str, Any] = {}

    async def fake_search(
        supabase, *, q, limit, offset, location, posted_within_days, salary_floor, skills=None
    ):
        captured.update(
            q=q,
            location=location,
            posted_within_days=posted_within_days,
            salary_floor=salary_floor,
        )
        return [], False

    monkeypatch.setattr(js, "search_jobs", fake_search)
    app.dependency_overrides[get_async_service_supabase] = lambda: MagicMock()
    app.dependency_overrides[verify_api_key_or_jwt] = lambda: "u"
    try:
        resp = TestClient(app).get(
            "/search?q=frontend&location=Remote&posted_within_days=30&salary_floor=150000"
        )
        assert resp.status_code == 200
        assert captured["location"] == "Remote"
        assert captured["posted_within_days"] == 30
        assert captured["salary_floor"] == 150000
    finally:
        app.dependency_overrides.clear()


# --- snippet helpers (the public-only JD preview) --------------------------


def test_html_to_snippet_strips_tags_and_collapses_whitespace() -> None:
    assert job_search._html_to_snippet("<p>Build   <b>fast</b>\n\n UIs.</p>") == ("Build fast UIs.")


def test_html_to_snippet_truncates_with_ellipsis() -> None:
    s = job_search._html_to_snippet("<p>" + "word " * 100 + "</p>", max_len=20)
    assert s is not None
    assert len(s) <= 21  # ≤ max_len chars + the single ellipsis
    assert s.endswith("…")


def test_html_to_snippet_double_strips_escaped_html() -> None:
    """Rows ingested before the greenhouse unescape fix hold HTML-ESCAPED
    markup; the first get_text pass unescapes it into tag-looking text (the
    2026-07-26 tag-soup-on-cards prod bug). The bounded second pass must
    strip it to clean prose."""
    escaped = (
        "&lt;div class=&quot;content-intro&quot;&gt;&lt;h2&gt;&lt;strong&gt;"
        "About LeafLink&lt;/strong&gt;&lt;/h2&gt; &lt;p&gt;&lt;span style="
        "&quot;font-weight: 400;&quot;&gt;LeafLink is the largest unified "
        "B2B cannabis platform&lt;/span&gt;&lt;/p&gt;"
    )
    out = job_search._html_to_snippet(escaped)
    assert out == "About LeafLink LeafLink is the largest unified B2B cannabis platform"
    assert "<" not in out and ">" not in out


def test_html_to_snippet_keeps_stray_lt_in_prose() -> None:
    """A literal '<' in prose is NOT a tag shape — the second pass must not
    fire and eat legitimate text."""
    out = job_search._html_to_snippet("<p>Comp &lt; $200k, equity &gt; 0.1%</p>")
    assert out == "Comp < $200k, equity > 0.1%"


def test_html_to_snippet_empty_or_blank_is_none() -> None:
    assert job_search._html_to_snippet(None) is None
    assert job_search._html_to_snippet("") is None
    assert job_search._html_to_snippet("<p>   </p>") is None  # tags only → blank


async def test_attach_snippets_populates_from_page_ids_only() -> None:
    results = [
        JobSearchResult(id="a", title="T", company_name="C"),
        JobSearchResult(id="b", title="T", company_name="C"),
    ]
    qb = MagicMock()
    qb.select.return_value = qb
    qb.in_.return_value = qb
    result = MagicMock()
    result.data = [{"id": "a", "description_html": "<p>Alpha role.</p>"}]
    qb.execute = AsyncMock(return_value=result)
    sb = MagicMock()
    sb.table.return_value = qb

    await job_search.attach_snippets(sb, results)

    assert results[0].snippet == "Alpha role."
    assert results[1].snippet is None  # no html row for b → None
    qb.in_.assert_called_once_with("id", ["a", "b"])  # bounded to the page ids


async def test_attach_snippets_is_best_effort_on_fetch_failure() -> None:
    # A transient fetch failure must NOT fail the search — a snippet is an
    # enhancement, so it degrades to None (WITHOUT the try/except this raises).
    results = [JobSearchResult(id="a", title="T", company_name="C")]
    sb = MagicMock()
    sb.table.return_value.select.return_value.in_.return_value.execute = AsyncMock(
        side_effect=RuntimeError("db down")
    )
    await job_search.attach_snippets(sb, results)  # no raise
    assert results[0].snippet is None


async def test_attach_snippets_noop_on_empty_results() -> None:
    sb = MagicMock()
    await job_search.attach_snippets(sb, [])
    sb.table.assert_not_called()  # nothing to enrich → no fetch


async def test_search_jobs_with_snippets_composes_search_and_attach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shared helper (used by BOTH endpoints) = search_jobs + attach_snippets."""
    results = [JobSearchResult(id="a", title="T", company_name="C")]

    async def _fake_search(sb: object, **kw: object) -> tuple[list[JobSearchResult], bool]:
        return results, True

    monkeypatch.setattr(job_search, "search_jobs", _fake_search)

    async def _fake_attach(sb: object, res: list[JobSearchResult], **kw: object) -> None:
        for r in res:
            r.snippet = "preview"

    monkeypatch.setattr(job_search, "attach_snippets", _fake_attach)

    out, has_more = await job_search.search_jobs_with_snippets(MagicMock(), q="x")
    assert has_more is True
    assert out[0].snippet == "preview"  # attach ran on search's results


def test_authed_search_endpoint_now_returns_snippets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The authed /search carries a snippet too (the card-grid UX, #467 §11) —
    it calls the snippet-bearing composition, not bare search_jobs."""
    from fastapi.testclient import TestClient

    from app.dependencies import get_async_service_supabase, verify_api_key_or_jwt
    from app.main import app
    from app.services import job_search as js

    row = JobSearchResult(
        id="a", title="Frontend Engineer", company_name="Co", snippet="Build fast UIs."
    )

    async def _fake_with_snippets(sb: object, **kw: object) -> tuple[list[JobSearchResult], bool]:
        return [row], False

    monkeypatch.setattr(js, "search_jobs_with_snippets", _fake_with_snippets)
    app.dependency_overrides[get_async_service_supabase] = lambda: MagicMock()
    app.dependency_overrides[verify_api_key_or_jwt] = lambda: "u"
    try:
        resp = TestClient(app).get("/search?q=frontend")
        assert resp.status_code == 200
        assert resp.json()["results"][0]["snippet"] == "Build fast UIs."
    finally:
        app.dependency_overrides.clear()


# --- salary floor (structured columns) ---------------------------------------


async def test_salary_floor_predicate_is_db_side() -> None:
    supabase = _mock_supabase([])
    await job_search.search_jobs(supabase, q="engineer", salary_floor=150_000)

    qb = supabase.table.return_value
    eq_calls = {c.args for c in qb.eq.call_args_list}
    assert ("salary_currency", "USD") in eq_calls
    assert ("salary_period", "yearly") in eq_calls
    or_calls = [c.args[0] for c in qb.or_.call_args_list]
    assert "salary_max.gte.150000,and(salary_max.is.null,salary_min.gte.150000)" in or_calls


async def test_salary_floor_is_clamped_to_cap() -> None:
    supabase = _mock_supabase([])
    await job_search.search_jobs(supabase, q="engineer", salary_floor=99_999_999)

    qb = supabase.table.return_value
    or_calls = [c.args[0] for c in qb.or_.call_args_list]
    cap = job_search.MAX_SALARY_FLOOR
    assert f"salary_max.gte.{cap},and(salary_max.is.null,salary_min.gte.{cap})" in or_calls


async def test_no_salary_floor_adds_no_salary_predicate() -> None:
    supabase = _mock_supabase([])
    await job_search.search_jobs(supabase, q="engineer")

    qb = supabase.table.return_value
    assert all(c.args[0] != "salary_currency" for c in qb.eq.call_args_list)
    assert all("salary_max" not in c.args[0] for c in qb.or_.call_args_list)


# --- blank-q browse mode (#834) --------------------------------------------


@pytest.mark.asyncio
async def test_blank_q_browses_without_a_title_clause() -> None:
    # A blank query returns the recency-biased window with the corpus gate and
    # filters intact — but NO title ilike clause (there is nothing to match).
    supabase = _mock_supabase([_row("n1", "Anything At All", "2026-08-30")])
    results, has_more = await job_search.search_jobs(supabase, q="")
    assert [r.id for r in results] == ["n1"]
    assert has_more is False
    qb = supabase.table.return_value
    title_clauses = [
        c.args[0] for c in qb.or_.call_args_list if "title.ilike" in str(c.args[0])
    ]
    assert title_clauses == []


@pytest.mark.asyncio
async def test_punctuation_only_q_still_returns_nothing() -> None:
    # A REAL query that sanitizes away to nothing must NOT fall through to
    # browse — "),(*:" returning the whole pool would be a lie.
    supabase = _mock_supabase([_row("n1", "Anything", "2026-08-30")])
    results, has_more = await job_search.search_jobs(supabase, q="),(*:")
    assert results == []
    assert has_more is False
    supabase.table.assert_not_called()


def test_search_endpoint_accepts_a_blank_q() -> None:
    from fastapi.testclient import TestClient

    from app.dependencies import get_async_service_supabase, verify_api_key_or_jwt
    from app.main import app

    supabase = _mock_supabase([_row("n1", "Newest Role", "2026-08-30")])
    app.dependency_overrides[get_async_service_supabase] = lambda: supabase
    app.dependency_overrides[verify_api_key_or_jwt] = lambda: "test-user"
    try:
        resp = TestClient(app).get("/search?q=")
        assert resp.status_code == 200
        assert [r["title"] for r in resp.json()["results"]] == ["Newest Role"]
    finally:
        app.dependency_overrides.clear()
