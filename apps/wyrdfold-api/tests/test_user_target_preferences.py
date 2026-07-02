"""Per-user target preferences (#60).

Covers:
* ``crud.get_user_target_preferences`` / ``set_user_target_preferences`` —
  the read/replace of the seven ``pref_*`` columns on ``user_targets``.
* ``GET`` / ``PUT /targets/{id}/preferences`` — JWT-scoped, ownership-checked
  (404 when the (user, target) link is missing, so the service-role client
  can't be steered onto another user's row), with PUT busting the jobs cache.

Preferences are a read-time filter over the SHARED cached score — these tests
assert the persistence + ownership contract; the filter behaviour itself lives
in ``test_jobs_preferences_filter.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.dependencies import get_current_user_id, get_user_supabase, verify_api_key_or_jwt
from app.main import app
from app.models.targets import TargetPreferences
from app.services.targets import crud


def _row(**overrides: Any) -> dict[str, Any]:
    """A user_targets row with the pref columns at their defaults."""
    now = datetime.now(UTC).isoformat()
    base: dict[str, Any] = {
        "id": "ut-1",
        "user_id": "user-1",
        "target_id": "target-1",
        "is_active": True,
        "fit_score": None,
        "fit_score_reasoning": None,
        "axis_weights": None,
        "axis_weights_previous": None,
        "job_score_threshold": None,
        "sms_score_threshold": None,
        "pref_score_cutoff": 40,
        "pref_locations": None,
        "pref_remote_ok": True,
        "pref_seniority_min": None,
        "pref_seniority_max": None,
        "pref_employment_types": None,
        "pref_include_unknown_salary": True,
        "created_at": now,
        "updated_at": now,
    }
    base.update(overrides)
    return base


def _wire_select(supabase: MagicMock, rows: list[dict[str, Any]]) -> None:
    chain = supabase.table.return_value.select.return_value.eq.return_value.eq.return_value.execute
    chain.return_value.data = rows


def _wire_update(supabase: MagicMock, rows: list[dict[str, Any]]) -> None:
    chain = supabase.table.return_value.update.return_value.eq.return_value.eq.return_value.execute
    chain.return_value.data = rows


# ---- crud -----------------------------------------------------------------


def test_get_preferences_returns_defaults_for_fresh_row() -> None:
    supabase = MagicMock()
    _wire_select(supabase, [_row()])

    prefs = crud.get_user_target_preferences(
        supabase, user_id="user-1", target_id="target-1"
    )

    assert prefs is not None
    assert prefs.pref_score_cutoff == 40
    assert prefs.pref_remote_ok is True
    assert prefs.pref_include_unknown_salary is True
    assert prefs.pref_locations is None
    assert prefs.pref_employment_types is None


def test_get_preferences_hydrates_stored_values() -> None:
    supabase = MagicMock()
    _wire_select(
        supabase,
        [
            _row(
                pref_score_cutoff=75,
                pref_locations=["berlin", "remote"],
                pref_remote_ok=False,
                pref_seniority_min="senior",
                pref_seniority_max="director",
                pref_employment_types=["full_time"],
                pref_include_unknown_salary=False,
            )
        ],
    )

    prefs = crud.get_user_target_preferences(
        supabase, user_id="user-1", target_id="target-1"
    )

    assert prefs is not None
    assert prefs.pref_score_cutoff == 75
    assert prefs.pref_locations == ["berlin", "remote"]
    assert prefs.pref_remote_ok is False
    assert prefs.pref_seniority_min == "senior"
    assert prefs.pref_seniority_max == "director"
    assert prefs.pref_employment_types == ["full_time"]
    assert prefs.pref_include_unknown_salary is False


def test_get_preferences_returns_none_when_row_missing() -> None:
    supabase = MagicMock()
    _wire_select(supabase, [])

    prefs = crud.get_user_target_preferences(
        supabase, user_id="user-1", target_id="missing"
    )

    assert prefs is None


def test_get_preferences_tolerates_null_pref_columns() -> None:
    """A row written before the column defaults applied (NULL cutoff /
    remote_ok / include_unknown_salary) must collapse to the model defaults,
    never KeyError or surface None for a non-optional field."""
    supabase = MagicMock()
    _wire_select(
        supabase,
        [_row(pref_score_cutoff=None, pref_remote_ok=None, pref_include_unknown_salary=None)],
    )

    prefs = crud.get_user_target_preferences(
        supabase, user_id="user-1", target_id="target-1"
    )

    assert prefs is not None
    assert prefs.pref_score_cutoff == 40
    assert prefs.pref_remote_ok is True
    assert prefs.pref_include_unknown_salary is True


def test_set_preferences_writes_all_columns() -> None:
    supabase = MagicMock()
    _wire_select(supabase, [_row()])
    updated = _row(
        pref_score_cutoff=80,
        pref_locations=["nyc"],
        pref_remote_ok=False,
        pref_seniority_min="staff",
        pref_employment_types=["contract"],
        pref_include_unknown_salary=False,
    )
    _wire_update(supabase, [updated])

    result = crud.set_user_target_preferences(
        supabase,
        user_id="user-1",
        target_id="target-1",
        preferences=TargetPreferences(
            pref_score_cutoff=80,
            pref_locations=["nyc"],
            pref_remote_ok=False,
            pref_seniority_min="staff",
            pref_employment_types=["contract"],
            pref_include_unknown_salary=False,
        ),
    )

    payload = supabase.table.return_value.update.call_args.args[0]
    # Every pref column is in the PUT payload (full replace), plus updated_at.
    for col in (
        "pref_score_cutoff",
        "pref_locations",
        "pref_remote_ok",
        "pref_seniority_min",
        "pref_seniority_max",
        "pref_employment_types",
        "pref_include_unknown_salary",
    ):
        assert col in payload
    assert payload["pref_score_cutoff"] == 80
    assert payload["pref_locations"] == ["nyc"]
    assert payload["pref_seniority_max"] is None  # omitted → cleared (PUT)
    assert "updated_at" in payload
    assert result is not None
    assert result.pref_score_cutoff == 80


def test_set_preferences_returns_none_when_row_missing() -> None:
    supabase = MagicMock()
    _wire_select(supabase, [])

    result = crud.set_user_target_preferences(
        supabase,
        user_id="user-1",
        target_id="missing",
        preferences=TargetPreferences(),
    )

    assert result is None
    supabase.table.return_value.update.assert_not_called()


# ---- endpoint -------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    app.dependency_overrides[get_user_supabase] = lambda: MagicMock()
    app.dependency_overrides[get_current_user_id] = lambda: "user-1"
    app.dependency_overrides[verify_api_key_or_jwt] = lambda: "user-1"
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_get_endpoint_returns_preferences(client: TestClient) -> None:
    supabase = MagicMock()
    _wire_select(supabase, [_row(pref_score_cutoff=55, pref_locations=["berlin"])])
    app.dependency_overrides[get_user_supabase] = lambda: supabase

    resp = client.get("/targets/target-1/preferences")

    assert resp.status_code == 200
    body = resp.json()
    assert body["pref_score_cutoff"] == 55
    assert body["pref_locations"] == ["berlin"]
    assert body["pref_remote_ok"] is True


def test_get_endpoint_404_when_no_link(client: TestClient) -> None:
    """A user querying a target they never linked gets 404 and no leak of the
    target's existence."""
    supabase = MagicMock()
    _wire_select(supabase, [])
    app.dependency_overrides[get_user_supabase] = lambda: supabase

    resp = client.get("/targets/target-1/preferences")

    assert resp.status_code == 404


def test_put_endpoint_persists_and_busts_cache(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.routers import targets as router_mod

    supabase = MagicMock()
    _wire_select(supabase, [_row()])
    _wire_update(supabase, [_row(pref_score_cutoff=90, pref_locations=["nyc"])])
    app.dependency_overrides[get_user_supabase] = lambda: supabase

    busted: list[str] = []
    monkeypatch.setattr(
        router_mod, "_invalidate_jobs_cache_for_target", lambda tid: busted.append(tid)
    )

    resp = client.put(
        "/targets/target-1/preferences",
        json={"pref_score_cutoff": 90, "pref_locations": ["nyc"]},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["pref_score_cutoff"] == 90
    assert body["pref_locations"] == ["nyc"]
    # PUT replaced the row, so the jobs-list cache for this target was busted.
    assert busted == ["target-1"]


def test_put_endpoint_404_when_no_link(client: TestClient) -> None:
    supabase = MagicMock()
    _wire_select(supabase, [])
    app.dependency_overrides[get_user_supabase] = lambda: supabase

    resp = client.put(
        "/targets/target-1/preferences",
        json={"pref_score_cutoff": 90},
    )

    assert resp.status_code == 404
    supabase.table.return_value.update.assert_not_called()


def test_put_idor_other_users_link_404(client: TestClient) -> None:
    """The (user, target) link doesn't exist for this caller → 404, no write —
    the service-role client can't be steered onto another user's row."""
    supabase = MagicMock()
    _wire_select(supabase, [])  # no link for this (user, target)
    app.dependency_overrides[get_user_supabase] = lambda: supabase

    resp = client.put(
        "/targets/target-1/preferences",
        json={"pref_score_cutoff": 10, "pref_locations": ["nowhere"]},
    )

    assert resp.status_code == 404
    supabase.table.return_value.update.assert_not_called()


@pytest.mark.parametrize(
    "value,expected", [(0, 200), (200, 200), (40, 200), (-1, 422), (201, 422)]
)
def test_put_score_cutoff_boundary_validation(
    client: TestClient, value: int, expected: int
) -> None:
    supabase = MagicMock()
    _wire_select(supabase, [_row()])
    _wire_update(supabase, [_row(pref_score_cutoff=value if 0 <= value <= 200 else 40)])
    app.dependency_overrides[get_user_supabase] = lambda: supabase

    resp = client.put(
        "/targets/target-1/preferences",
        json={"pref_score_cutoff": value},
    )
    assert resp.status_code == expected


def test_put_rejects_inverted_seniority_range(client: TestClient) -> None:
    """min ranking above max would silently match nothing — reject at the
    boundary (422) instead of storing a confusing no-op."""
    supabase = MagicMock()
    _wire_select(supabase, [_row()])
    app.dependency_overrides[get_user_supabase] = lambda: supabase

    resp = client.put(
        "/targets/target-1/preferences",
        json={"pref_seniority_min": "director", "pref_seniority_max": "ic"},
    )

    assert resp.status_code == 422
    supabase.table.return_value.update.assert_not_called()


def test_put_rejects_unknown_seniority_level(client: TestClient) -> None:
    resp = client.put(
        "/targets/target-1/preferences",
        json={"pref_seniority_min": "principal"},  # not on the ladder
    )
    assert resp.status_code == 422


def test_put_empty_body_uses_defaults(client: TestClient) -> None:
    """PUT with an empty body is a valid full-replace to defaults."""
    supabase = MagicMock()
    _wire_select(supabase, [_row(pref_score_cutoff=99)])
    _wire_update(supabase, [_row()])  # reset to defaults
    app.dependency_overrides[get_user_supabase] = lambda: supabase

    resp = client.put("/targets/target-1/preferences", json={})

    assert resp.status_code == 200
    payload = supabase.table.return_value.update.call_args.args[0]
    assert payload["pref_score_cutoff"] == 40  # default applied
    assert payload["pref_locations"] is None
    assert payload["pref_remote_ok"] is True


def test_preferences_route_does_not_collide_with_get_target(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GET /targets/{id}`` is declared above ``/{id}/preferences``; the
    'preferences' suffix must dispatch to the preferences handler, not get
    swallowed by the {target_id} placeholder."""
    from app.routers import targets as router_mod

    supabase = MagicMock()
    _wire_select(supabase, [_row(pref_score_cutoff=33)])
    app.dependency_overrides[get_user_supabase] = lambda: supabase
    # GET /targets/{id} ownership-checks the caller; the fixture user owns it.
    monkeypatch.setattr(
        router_mod.crud, "get_user_target_ids", lambda *_a, **_kw: {"target-1"}
    )
    monkeypatch.setattr(
        router_mod.crud,
        "get",
        lambda *_a, **_kw: _job_target(),
    )

    prefs = client.get("/targets/target-1/preferences")
    assert prefs.status_code == 200
    assert prefs.json()["pref_score_cutoff"] == 33


def _job_target() -> Any:
    from app.models.targets import JobTarget, ScoringProfile

    now = datetime.now(UTC)
    return JobTarget(
        id="target-1",
        label="Some Target",
        scoring_profile=ScoringProfile(),
        is_active=True,
        created_at=now,
        updated_at=now,
    )
