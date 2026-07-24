"""Public job search (#467) — ranking logic + the corpus gate + the endpoint.

The crux is the #467 acceptance example: for "frontend developer", a
"Frontend Engineer" must outrank a "Backend Developer" (developer ≈ engineer, but
frontend ≠ backend) — even when the backend role is more recent. And the search
must hit only the LIVE, US corpus and leak no match score / no JD body.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from app.models.job_search import JobSearchResult
from app.services import job_search


def _mock_supabase(rows: list[dict[str, Any]]) -> MagicMock:
    """A supabase stub whose query builder is self-returning; ``.execute().data``
    yields ``rows`` regardless of the filters applied (filters asserted
    separately via call_args)."""
    qb = MagicMock(name="query_builder")
    for meth in ("select", "is_", "or_", "order", "limit", "eq", "gte"):
        getattr(qb, meth).return_value = qb
    qb.not_ = qb  # ``.not_`` is a property, not a call → same builder
    qb.execute.return_value.data = rows
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
        "department": None,
        "salary_text": None,
        "absolute_url": f"https://example.com/{rid}",
        "first_seen_at": ts,
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


def test_frontend_engineer_outranks_backend_developer() -> None:
    # Backend Developer is the MOST RECENT — role match must still beat recency.
    rows = [
        _row("fe-eng", "Frontend Engineer", "2026-01-02"),
        _row("be-dev", "Backend Developer", "2026-01-09"),
        _row("sr-fe", "Senior Frontend Developer", "2026-01-01"),
    ]
    supabase = _mock_supabase(rows)

    results, has_more = job_search.search_jobs(supabase, q="frontend developer")
    assert has_more is False  # only 3 matches, one page
    ids = [r.id for r in results]

    # Both frontend roles (overlap 2) rank above the backend role (overlap 1)...
    assert ids.index("fe-eng") < ids.index("be-dev")
    assert ids.index("sr-fe") < ids.index("be-dev")
    # ...and among equal-overlap, the more recent wins.
    assert ids.index("fe-eng") < ids.index("sr-fe")
    # The off-role match is dead last despite being newest.
    assert ids[-1] == "be-dev"


def test_empty_query_returns_empty_without_hitting_db() -> None:
    supabase = _mock_supabase([_row("x", "Anything", "2026-01-01")])
    assert job_search.search_jobs(supabase, q="   ") == ([], False)
    assert job_search.search_jobs(supabase, q="*,()") == ([], False)
    supabase.table.assert_not_called()


def test_limit_is_clamped_to_max_page_size() -> None:
    rows = [_row(str(i), "Frontend Engineer", "2026-01-15") for i in range(1, 40)]
    supabase = _mock_supabase(rows)
    results, has_more = job_search.search_jobs(supabase, q="frontend", limit=999)
    assert len(results) == job_search.MAX_PAGE_SIZE
    assert has_more is True  # 39 matches > one clamped page of 25


def test_pagination_offset_and_has_more() -> None:
    # 30 identical-overlap matches → deterministic order by recency.
    rows = [
        _row(f"j{i:02d}", "Frontend Engineer", f"2026-01-{i:02d}T00:00:00+00:00")
        for i in range(1, 31)
    ]
    supabase = _mock_supabase(rows)

    page1, more1 = job_search.search_jobs(supabase, q="frontend", limit=20, offset=0)
    page2, more2 = job_search.search_jobs(supabase, q="frontend", limit=20, offset=20)

    assert len(page1) == 20 and more1 is True  # 30 > 20
    assert len(page2) == 10 and more2 is False  # last page, nothing after
    # No overlap between pages, and page 2 continues the ranked order.
    assert {r.id for r in page1}.isdisjoint(r.id for r in page2)


# --- corpus gate + filter --------------------------------------------------


def test_search_applies_live_us_gate_and_synonym_filter() -> None:
    supabase = _mock_supabase([])
    job_search.search_jobs(supabase, q="frontend dev")
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

    from app.dependencies import get_supabase, verify_api_key_or_jwt
    from app.main import app

    rows = [
        _row("fe", "Frontend Engineer", "2026-01-02"),
        _row("be", "Backend Developer", "2026-01-09"),
    ]
    app.dependency_overrides[get_supabase] = lambda: _mock_supabase(rows)
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


def test_search_endpoint_requires_a_query() -> None:
    from fastapi.testclient import TestClient

    from app.dependencies import get_supabase, verify_api_key_or_jwt
    from app.main import app

    app.dependency_overrides[get_supabase] = lambda: MagicMock()
    app.dependency_overrides[verify_api_key_or_jwt] = lambda: "test-user"
    try:
        assert TestClient(app).get("/search").status_code == 422  # q is required
    finally:
        app.dependency_overrides.clear()
