"""Public single-listing endpoint (#467 §11.2 fast-follow — shareable URLs).

``GET /public/listings/{listing_id}`` is the hard-load / deep-link counterpart
to ``GET /public/search`` (same router, same BFF-only + per-IP-limited posture,
same preview projection). These mirror ``test_public_search.py`` and pin:

  - **422 on junk** — the path param is a typed UUID, so a non-UUID id is
    rejected at the edge and never reaches the service (nor the DB);
  - **404 without an existence leak** — a missing id and a no-longer-eligible
    row (archived / purged / non-US) return the SAME 404 (the service applies
    the identical live/US gate as ``search_jobs`` and yields ``None`` for both);
  - the **preview shape** — no match score, no JD body; snippet attached via
    the existing helper;
  - **BFF-only** — with the secret configured, a direct hit is 403;
  - **no search_events row** — opening a shared link is not a search.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

from fastapi.testclient import TestClient

from app.cache import job_list_cache
from app.config import Settings
from app.dependencies import get_settings, get_supabase
from app.main import app
from app.models.job_search import JobSearchResult
from app.routers import public_search
from app.services import job_search


def teardown_function() -> None:
    app.dependency_overrides.clear()
    # The listing cache is a module singleton — flush our namespace so a cached
    # 200 from one test can't satisfy (and silently skip) the next test's stub.
    job_list_cache.invalidate(prefix="publiclisting:")


def _result(rid: str, *, snippet: str | None = None) -> JobSearchResult:
    return JobSearchResult(
        id=rid,
        title="Frontend Engineer",
        company_name="Acme",
        location="Remote",
        absolute_url="https://boards.greenhouse.io/acme/1",
        snippet=snippet,
    )


def _stub_get_listing(
    monkeypatch,
    *,
    result: JobSearchResult | None,
    calls: list[str] | None = None,
) -> None:
    """Patch the service so the endpoint returns a controlled row (or the
    None → 404 path), recording each forwarded id."""

    def fake(supabase, listing_id: str) -> JobSearchResult | None:
        if calls is not None:
            calls.append(listing_id)
        return result

    monkeypatch.setattr(public_search.job_search, "get_listing", fake)
    app.dependency_overrides[get_supabase] = lambda: MagicMock()


# ---- shape (a preview: no score, no JD body; snippet riding along) ----------


def test_returns_the_listing_preview_shape(monkeypatch) -> None:
    rid = str(uuid4())
    _stub_get_listing(monkeypatch, result=_result(rid, snippet="Build fast UIs."))
    r = TestClient(app).get(f"/public/listings/{rid}")
    assert r.status_code == 200
    row = r.json()
    assert row["id"] == rid
    assert row["snippet"] == "Build fast UIs."
    # A preview + source link only — no score, no JD body ever reaches a public
    # caller (JobSearchResult carries neither field).
    assert "score" not in row
    assert "description_html" not in row


def test_forwards_the_id_to_the_service(monkeypatch) -> None:
    rid = str(uuid4())
    calls: list[str] = []
    _stub_get_listing(monkeypatch, result=_result(rid), calls=calls)
    TestClient(app).get(f"/public/listings/{rid}")
    assert calls == [rid]


def test_repeat_hit_is_served_from_cache(monkeypatch) -> None:
    rid = str(uuid4())
    calls: list[str] = []
    _stub_get_listing(monkeypatch, result=_result(rid), calls=calls)
    client = TestClient(app)
    assert client.get(f"/public/listings/{rid}").status_code == 200
    assert client.get(f"/public/listings/{rid}").status_code == 200
    assert calls == [rid]  # second response came from job_list_cache


# ---- 404: missing AND delisted are indistinguishable ------------------------


def test_missing_id_is_404(monkeypatch) -> None:
    _stub_get_listing(monkeypatch, result=None)
    r = TestClient(app).get(f"/public/listings/{uuid4()}")
    assert r.status_code == 404


def test_delisted_row_is_the_same_404_via_the_eligibility_gate(monkeypatch) -> None:
    """An archived/purged/non-US row must 404 exactly like a missing one.

    The gate lives in the SERVICE query (the same live/US predicate as
    ``search_jobs``), so prove it there: the select carries every eligibility
    filter, and an excluded row (empty result set) yields ``None`` — which the
    endpoint test above maps to 404. No 'exists but delisted' signal escapes.
    """
    qb = MagicMock()
    qb.select.return_value = qb
    qb.is_.return_value = qb
    qb.not_.is_.return_value = qb
    qb.eq.return_value = qb
    qb.limit.return_value = qb
    qb.execute.return_value.data = []  # filtered out (or absent) → no rows
    sb = MagicMock()
    sb.table.return_value = qb

    rid = str(uuid4())
    assert job_search.get_listing(sb, rid) is None

    qb.is_.assert_any_call("archived_at", "null")
    qb.is_.assert_any_call("purged_at", "null")
    qb.not_.is_.assert_called_once_with("is_us", "false")
    qb.eq.assert_called_once_with("id", rid)


def test_service_attaches_the_snippet_to_a_live_row(monkeypatch) -> None:
    """``get_listing`` runs the existing snippet helper over the single row."""
    rid = str(uuid4())
    qb = MagicMock()
    qb.select.return_value = qb
    qb.is_.return_value = qb
    qb.not_.is_.return_value = qb
    qb.eq.return_value = qb
    qb.limit.return_value = qb
    qb.execute.return_value.data = [
        {"id": rid, "title": "Frontend Engineer", "company_name": "Acme"}
    ]
    sb = MagicMock()
    sb.table.return_value = qb

    seen: dict[str, Any] = {}

    def fake_attach(supabase, results, **kw) -> None:
        seen["ids"] = [r.id for r in results]
        results[0].snippet = "Preview."

    monkeypatch.setattr(job_search, "attach_snippets", fake_attach)

    out = job_search.get_listing(sb, rid)
    assert out is not None
    assert out.snippet == "Preview."
    assert seen["ids"] == [rid]  # page-only fetch: exactly this row


# ---- junk id (typed UUID path param) ----------------------------------------


def test_junk_id_is_rejected_422(monkeypatch) -> None:
    calls: list[str] = []
    _stub_get_listing(monkeypatch, result=_result(str(uuid4())), calls=calls)
    r = TestClient(app).get("/public/listings/not-a-uuid")
    assert r.status_code == 422
    assert calls == []  # junk never reaches the service


# ---- BFF-only posture (router-level require_bff_secret) ----------------------


def test_direct_hit_without_bff_secret_is_forbidden(monkeypatch) -> None:
    rid = str(uuid4())
    _stub_get_listing(monkeypatch, result=_result(rid))
    app.dependency_overrides[get_settings] = lambda: Settings(wyrdfold_bff_secret="s3cret")
    r = TestClient(app).get(f"/public/listings/{rid}")
    assert r.status_code == 403


def test_bff_forwarded_call_is_allowed(monkeypatch) -> None:
    rid = str(uuid4())
    _stub_get_listing(monkeypatch, result=_result(rid))
    app.dependency_overrides[get_settings] = lambda: Settings(wyrdfold_bff_secret="s3cret")
    r = TestClient(app).get(
        f"/public/listings/{rid}", headers={"x-wyrdfold-bff": "s3cret"}
    )
    assert r.status_code == 200


# ---- no funnel row (browsing a shared link is not a search) ------------------


def test_records_no_search_event(monkeypatch) -> None:
    rid = str(uuid4())
    _stub_get_listing(monkeypatch, result=_result(rid))
    recorded: list[Any] = []
    monkeypatch.setattr(
        public_search.search_events,
        "record_search",
        lambda **kw: recorded.append(kw),
    )
    r = TestClient(app).get(f"/public/listings/{rid}")
    assert r.status_code == 200
    assert recorded == []
