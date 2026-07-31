"""GET /targets/search — discovery over the shared catalog (feat/target-search).

Targets are shared: a role one user created is discoverable by others so they
can follow it instead of minting a duplicate. These pin the endpoint contract —
matches are marked ``is_linked`` for targets the caller already follows, the
lightweight result never leaks the scoring profile, and a too-short query never
dumps the catalog.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.dependencies import (
    get_current_user_id,
    get_supabase,
    verify_api_key_or_jwt,
)
from app.main import app
from app.models.targets import JobTarget, ScoringProfile


def _target(tid: str, label: str, description: str | None = None) -> JobTarget:
    now = datetime.now(UTC)
    return JobTarget(
        id=tid,
        label=label,
        description=description,
        scoring_profile=ScoringProfile(),
        app_active=True,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def _client(user_id: str = "user-1") -> TestClient:
    app.dependency_overrides[get_supabase] = lambda: MagicMock()
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    app.dependency_overrides[verify_api_key_or_jwt] = lambda: user_id
    return TestClient(app)


def test_search_marks_which_results_the_caller_already_follows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routers import targets as mod

    monkeypatch.setattr(
        mod.crud,
        "search_by_label",
        lambda *_a, **_k: [
            _target("t-1", "Senior Frontend Engineer", "react roles"),
            _target("t-2", "Staff Frontend Engineer"),
        ],
    )
    monkeypatch.setattr(mod.crud, "get_user_target_ids", lambda *_a, **_k: {"t-1"})

    # Resolving at all proves /search is declared before /{target_id} (else it
    # would be captured as get_target("search") and 404 on ownership).
    resp = _client().get("/targets/search", params={"q": "frontend"})

    assert resp.status_code == 200
    results = resp.json()["results"]
    assert [r["id"] for r in results] == ["t-1", "t-2"]
    assert results[0]["is_linked"] is True  # caller already follows t-1
    assert results[1]["is_linked"] is False  # but not t-2
    assert results[0]["description"] == "react roles"
    # The heavy scoring_profile is NOT surfaced in the lightweight result.
    assert "scoring_profile" not in results[0]


def test_search_missing_query_is_422() -> None:
    resp = _client().get("/targets/search")
    assert resp.status_code == 422


def test_search_no_matches_returns_empty_and_skips_link_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routers import targets as mod

    monkeypatch.setattr(mod.crud, "search_by_label", lambda *_a, **_k: [])
    link_spy = MagicMock()
    monkeypatch.setattr(mod.crud, "get_user_target_ids", link_spy)

    resp = _client().get("/targets/search", params={"q": "zznomatch"})

    assert resp.status_code == 200
    assert resp.json()["results"] == []
    link_spy.assert_not_called()  # no matches → don't bother reading the caller's links


# ---- service-level: the too-short-query short-circuit never touches the DB ----


@pytest.mark.parametrize("q", ["", " ", "a", "  x "])
def test_search_by_label_short_query_returns_empty_without_a_db_hit(q: str) -> None:
    from app.services.targets import crud

    supabase = MagicMock()
    assert crud.search_by_label(supabase, q) == []
    supabase.table.assert_not_called()


def test_search_by_label_builds_trimmed_ilike_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.targets import crud

    # Pin the query shape: substring ILIKE on `label`, trimmed + wrapped in %,
    # ordered by label, bounded by limit. Parsing is covered elsewhere, so
    # stub _parse_target to a pass-through and assert the chain.
    monkeypatch.setattr(crud, "_parse_target", lambda row: row)

    execute = MagicMock(return_value=MagicMock(data=[{"id": "t-1"}]))
    limit = MagicMock(return_value=MagicMock(execute=execute))
    order = MagicMock(return_value=MagicMock(limit=limit))
    ilike = MagicMock(return_value=MagicMock(order=order))
    select = MagicMock(return_value=MagicMock(ilike=ilike))
    table = MagicMock(return_value=MagicMock(select=select))
    supabase = MagicMock(table=table)

    result = crud.search_by_label(supabase, "  Frontend  ", limit=15)

    table.assert_called_once_with("targets")
    ilike.assert_called_once_with("label", "%Frontend%")  # trimmed, wrapped
    order.assert_called_once_with("label")
    limit.assert_called_once_with(15)
    assert result == [{"id": "t-1"}]
