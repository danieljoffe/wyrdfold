"""Public search endpoint (#467 — V1 slice 1): the security-core surface.

``GET /public/search`` is the unauthenticated counterpart to ``/search``. These
pin the guards that keep it from becoming a corpus-enumeration / scraping hole:

  - **HARD depth cap** (``page_size`` ≤ 20, ``offset`` ≤ 40) — anything deeper is
    a 422, so a logged-out caller can't page through the whole corpus;
  - **BFF-only** — with the secret configured, a direct hit missing the
    ``X-Wyrdfold-BFF`` header is 403 (the router-level ``require_bff_secret``);
  - it reuses the un-personalized ``search_jobs`` and returns **no match score /
    no JD body** — a preview + source link only;
  - the caller's (capped) paging + filters reach the service verbatim.

Per-IP rate-limiting is disabled in tests (``RATE_LIMIT_ENABLED=false``) and the
guard itself is covered in test_bff_secret.py / slowapi; here we prove the caps,
the auth posture, and the wiring.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from app.config import Settings
from app.dependencies import get_async_service_supabase, get_settings
from app.main import app
from app.models.job_search import JobSearchResult
from app.routers import public_search


def teardown_function() -> None:
    app.dependency_overrides.clear()


def _result(rid: str) -> JobSearchResult:
    return JobSearchResult(
        id=rid,
        title=f"Frontend Engineer {rid}",
        company_name=f"Co-{rid}",
        location="Remote",
        absolute_url=f"https://boards.greenhouse.io/{rid}",
    )


def _stub_search(
    monkeypatch,
    *,
    results: list[JobSearchResult] | None = None,
    has_more: bool = False,
    captured: dict[str, Any] | None = None,
) -> None:
    """Patch the service so the endpoint returns controlled rows, and capture the
    (q, limit, offset, filters) it forwarded."""

    async def fake(
        supabase, *, q, limit, offset, location, posted_within_days, salary_floor, skills=None
    ):
        if captured is not None:
            captured.update(
                q=q,
                limit=limit,
                offset=offset,
                location=location,
                posted_within_days=posted_within_days,
                salary_floor=salary_floor,
            )
        return (results if results is not None else [_result("a")], has_more)

    monkeypatch.setattr(public_search.job_search, "search_jobs", fake)

    # No-op the snippet fetch so the cap / BFF / forwarding tests stay focused;
    # the snippet path has its own tests below.
    async def _noop_attach(sb: object, res: object, **kw: object) -> None:
        return None

    monkeypatch.setattr(public_search.job_search, "attach_snippets", _noop_attach)
    app.dependency_overrides[get_async_service_supabase] = lambda: MagicMock()


# ---- depth cap (the anti-scraping guard) -----------------------------------


def test_offset_beyond_public_cap_is_rejected(monkeypatch) -> None:
    _stub_search(monkeypatch)
    r = TestClient(app).get(
        f"/public/search?q=frontend&offset={public_search.PUBLIC_MAX_OFFSET + 1}"
    )
    assert r.status_code == 422  # one past the public offset cap → rejected


def test_page_size_beyond_public_cap_is_rejected(monkeypatch) -> None:
    _stub_search(monkeypatch)
    r = TestClient(app).get(
        f"/public/search?q=frontend&page_size={public_search.PUBLIC_MAX_PAGE_SIZE + 1}"
    )
    assert r.status_code == 422


def test_salary_floor_bounds_are_enforced(monkeypatch) -> None:
    _stub_search(monkeypatch)
    client = TestClient(app)
    # 0 and negatives are meaningless floors; beyond the plausibility cap 422s.
    assert client.get("/public/search?q=x&salary_floor=0").status_code == 422
    assert client.get("/public/search?q=x&salary_floor=-5").status_code == 422
    over = public_search.job_search.MAX_SALARY_FLOOR + 1
    assert client.get(f"/public/search?q=x&salary_floor={over}").status_code == 422
    # Junk types never reach the service.
    assert client.get("/public/search?q=x&salary_floor=lots").status_code == 422


def test_salary_floor_is_forwarded(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    _stub_search(monkeypatch, captured=captured)
    r = TestClient(app).get("/public/search?q=frontend&salary_floor=150000")
    assert r.status_code == 200
    assert captured["salary_floor"] == 150000


def test_at_the_public_cap_boundary_is_allowed(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    _stub_search(monkeypatch, captured=captured)
    r = TestClient(app).get(
        f"/public/search?q=frontend"
        f"&page_size={public_search.PUBLIC_MAX_PAGE_SIZE}"
        f"&offset={public_search.PUBLIC_MAX_OFFSET}"
    )
    assert r.status_code == 200
    # The exact cap is allowed and forwarded (not silently re-clamped down).
    assert captured["limit"] == public_search.PUBLIC_MAX_PAGE_SIZE
    assert captured["offset"] == public_search.PUBLIC_MAX_OFFSET


# ---- reuse + shape (no score, no JD body) ----------------------------------


def test_returns_page_with_no_score_or_jd_body(monkeypatch) -> None:
    _stub_search(monkeypatch, results=[_result("a"), _result("b")], has_more=True)
    r = TestClient(app).get("/public/search?q=frontend+developer")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert body["has_more"] is True
    row = body["results"][0]
    # A preview + source link only — no score, no JD body ever reaches a public
    # caller (JobSearchResult carries neither field).
    assert "score" not in row
    assert "description_html" not in row
    assert row["absolute_url"].startswith("https://")


def test_forwards_query_and_filters_to_the_service(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    _stub_search(monkeypatch, captured=captured)
    TestClient(app).get("/public/search?q=Frontend+Developer&location=Remote&posted_within_days=7")
    assert captured["q"] == "Frontend Developer"  # service does its own normalize
    assert captured["location"] == "Remote"
    assert captured["posted_within_days"] == 7


def test_requires_a_query(monkeypatch) -> None:
    _stub_search(monkeypatch)
    assert TestClient(app).get("/public/search").status_code == 422  # q is required


# ---- snippet (the public-only JD preview) ----------------------------------


def test_populates_snippet_from_page_jd_html(monkeypatch) -> None:
    """The public endpoint runs the real ``attach_snippets`` after ``search_jobs``:
    a page-only ``description_html`` fetch, tag-stripped into a preview."""

    async def fake_search(
        supabase, *, q, limit, offset, location, posted_within_days, salary_floor, skills=None
    ):
        return ([_result("a"), _result("b")], False)

    monkeypatch.setattr(public_search.job_search, "search_jobs", fake_search)

    # supabase whose jobs fetch returns description_html for the page ids only.
    qb = MagicMock()
    qb.select.return_value = qb
    qb.in_.return_value = qb
    result = MagicMock()
    result.data = [
        {"id": "a", "description_html": "<p>Build <b>fast</b> UIs.</p>"},
        {"id": "b", "description_html": ""},  # empty JD → no snippet
    ]
    qb.execute = AsyncMock(return_value=result)
    sb = MagicMock()
    sb.table.return_value = qb
    app.dependency_overrides[get_async_service_supabase] = lambda: sb

    r = TestClient(app).get("/public/search?q=frontend")
    assert r.status_code == 200
    rows = {row["id"]: row for row in r.json()["results"]}
    assert rows["a"]["snippet"] == "Build fast UIs."  # tags stripped, ws collapsed
    assert rows["b"]["snippet"] is None
    # STILL no full JD body in the row — the snippet replaces it, never leaks it.
    assert "description_html" not in rows["a"]
    # Fetched only the page ids (bounded — no corpus-wide description read).
    qb.in_.assert_called_once_with("id", ["a", "b"])


# ---- BFF-only posture (the router carries require_bff_secret) ---------------


def test_direct_hit_without_bff_secret_is_forbidden(monkeypatch) -> None:
    _stub_search(monkeypatch)
    # With the secret configured, a call NOT routed through the BFF (no header) is
    # 403 — this route can't be hit directly on Railway to rotate past the limit.
    app.dependency_overrides[get_settings] = lambda: Settings(wyrdfold_bff_secret="s3cret")
    r = TestClient(app).get("/public/search?q=frontend")
    assert r.status_code == 403


def test_bff_forwarded_call_is_allowed(monkeypatch) -> None:
    _stub_search(monkeypatch)
    app.dependency_overrides[get_settings] = lambda: Settings(wyrdfold_bff_secret="s3cret")
    r = TestClient(app).get("/public/search?q=frontend", headers={"x-wyrdfold-bff": "s3cret"})
    assert r.status_code == 200
