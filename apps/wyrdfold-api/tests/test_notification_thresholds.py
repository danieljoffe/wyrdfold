"""Per-target notification thresholds (#15).

Covers ``crud.set_user_target_notification_thresholds`` (snapshot-free
set/clear of the two ``user_targets`` columns) and the
``PATCH /targets/{id}/notification-thresholds`` endpoint that backs the
target-detail "Notification thresholds" section.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.dependencies import get_current_user_id, get_supabase, verify_api_key_or_jwt
from app.main import app
from app.services.targets import crud


def _row(
    *,
    job_score_threshold: int | None = None,
    sms_score_threshold: int | None = None,
) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    return {
        "id": "ut-1",
        "user_id": "user-1",
        "target_id": "target-1",
        "is_active": True,
        "fit_score": None,
        "fit_score_reasoning": None,
        "axis_weights": None,
        "axis_weights_previous": None,
        "job_score_threshold": job_score_threshold,
        "sms_score_threshold": sms_score_threshold,
        "created_at": now,
        "updated_at": now,
    }


def _wire_select(supabase: MagicMock, rows: list[dict[str, Any]]) -> None:
    chain = supabase.table.return_value.select.return_value.eq.return_value.eq.return_value.execute
    chain.return_value.data = rows


def _wire_update(supabase: MagicMock, rows: list[dict[str, Any]]) -> None:
    chain = supabase.table.return_value.update.return_value.eq.return_value.eq.return_value.execute
    chain.return_value.data = rows


# ---- crud -----------------------------------------------------------------


def test_sets_both_thresholds() -> None:
    supabase = MagicMock()
    _wire_select(supabase, [_row()])
    _wire_update(supabase, [_row(job_score_threshold=90, sms_score_threshold=70)])

    result = crud.set_user_target_notification_thresholds(
        supabase,
        user_id="user-1",
        target_id="target-1",
        thresholds={"job_score_threshold": 90, "sms_score_threshold": 70},
    )

    payload = supabase.table.return_value.update.call_args.args[0]
    assert payload["job_score_threshold"] == 90
    assert payload["sms_score_threshold"] == 70
    assert "updated_at" in payload
    assert result is not None
    assert result.job_score_threshold == 90
    assert result.sms_score_threshold == 70


def test_none_resets_channel_to_default() -> None:
    supabase = MagicMock()
    _wire_select(supabase, [_row(job_score_threshold=90, sms_score_threshold=70)])
    _wire_update(supabase, [_row(job_score_threshold=None, sms_score_threshold=70)])

    result = crud.set_user_target_notification_thresholds(
        supabase,
        user_id="user-1",
        target_id="target-1",
        thresholds={"job_score_threshold": None, "sms_score_threshold": 70},
    )

    payload = supabase.table.return_value.update.call_args.args[0]
    assert payload["job_score_threshold"] is None
    assert payload["sms_score_threshold"] == 70
    assert result is not None
    assert result.job_score_threshold is None


def test_omitted_channel_is_not_written() -> None:
    """A partial update touches only the channel present in ``thresholds``;
    the omitted column must NOT appear in the UPDATE payload, so the other
    channel's stored value survives."""
    supabase = MagicMock()
    _wire_select(supabase, [_row(job_score_threshold=50, sms_score_threshold=70)])
    _wire_update(supabase, [_row(job_score_threshold=90, sms_score_threshold=70)])

    result = crud.set_user_target_notification_thresholds(
        supabase,
        user_id="user-1",
        target_id="target-1",
        thresholds={"job_score_threshold": 90},  # sms omitted -> untouched
    )

    payload = supabase.table.return_value.update.call_args.args[0]
    assert payload["job_score_threshold"] == 90
    assert "sms_score_threshold" not in payload
    assert result is not None


def test_empty_thresholds_is_noop_returning_current_row() -> None:
    """An empty body writes nothing and returns the current row unchanged."""
    supabase = MagicMock()
    _wire_select(supabase, [_row(job_score_threshold=50, sms_score_threshold=70)])

    result = crud.set_user_target_notification_thresholds(
        supabase,
        user_id="user-1",
        target_id="target-1",
        thresholds={},
    )

    supabase.table.return_value.update.assert_not_called()
    assert result is not None
    assert result.job_score_threshold == 50
    assert result.sms_score_threshold == 70


def test_returns_none_when_row_missing() -> None:
    supabase = MagicMock()
    _wire_select(supabase, [])

    result = crud.set_user_target_notification_thresholds(
        supabase,
        user_id="user-1",
        target_id="missing",
        thresholds={"job_score_threshold": 90, "sms_score_threshold": None},
    )

    assert result is None
    supabase.table.return_value.update.assert_not_called()


# ---- endpoint -------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    app.dependency_overrides[get_supabase] = lambda: MagicMock()
    app.dependency_overrides[get_current_user_id] = lambda: "user-1"
    app.dependency_overrides[verify_api_key_or_jwt] = lambda: "user-1"
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_patch_sets_thresholds(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.routers import targets as router_mod
    from app.services.targets.crud import _parse_user_target

    captured: dict[str, Any] = {}

    def _fake(supabase: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return _parse_user_target(_row(job_score_threshold=90, sms_score_threshold=70))

    monkeypatch.setattr(router_mod.crud, "set_user_target_notification_thresholds", _fake)

    resp = client.patch(
        "/targets/target-1/notification-thresholds",
        json={"job_score_threshold": 90, "sms_score_threshold": 70},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["job_score_threshold"] == 90
    assert body["sms_score_threshold"] == 70
    assert captured["thresholds"] == {"job_score_threshold": 90, "sms_score_threshold": 70}


def test_patch_reset_sends_nulls(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.routers import targets as router_mod
    from app.services.targets.crud import _parse_user_target

    captured: dict[str, Any] = {}

    def _fake(supabase: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return _parse_user_target(_row())

    monkeypatch.setattr(router_mod.crud, "set_user_target_notification_thresholds", _fake)

    resp = client.patch(
        "/targets/target-1/notification-thresholds",
        json={"job_score_threshold": None, "sms_score_threshold": None},
    )

    assert resp.status_code == 200
    assert captured["thresholds"] == {"job_score_threshold": None, "sms_score_threshold": None}


def test_patch_omitted_channel_not_forwarded(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a body with only ``job_score_threshold`` must forward a
    ``thresholds`` dict WITHOUT ``sms_score_threshold`` — the fix for the
    destructive partial-PATCH (omitting SMS used to null it)."""
    from app.routers import targets as router_mod
    from app.services.targets.crud import _parse_user_target

    captured: dict[str, Any] = {}

    def _fake(supabase: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return _parse_user_target(_row(job_score_threshold=90, sms_score_threshold=70))

    monkeypatch.setattr(router_mod.crud, "set_user_target_notification_thresholds", _fake)

    resp = client.patch(
        "/targets/target-1/notification-thresholds",
        json={"job_score_threshold": 90},
    )

    assert resp.status_code == 200
    assert captured["thresholds"] == {"job_score_threshold": 90}
    assert "sms_score_threshold" not in captured["thresholds"]


def test_patch_partial_body_preserves_other_channel_end_to_end(
    client: TestClient,
) -> None:
    """Drive the REAL crud: a partial PATCH must leave the omitted channel's
    column out of the UPDATE entirely (regression for the wipe footgun)."""
    supabase = MagicMock()
    _wire_select(supabase, [_row(job_score_threshold=50, sms_score_threshold=70)])
    _wire_update(supabase, [_row(job_score_threshold=90, sms_score_threshold=70)])
    app.dependency_overrides[get_supabase] = lambda: supabase

    resp = client.patch(
        "/targets/target-1/notification-thresholds",
        json={"job_score_threshold": 90},
    )

    assert resp.status_code == 200
    payload = supabase.table.return_value.update.call_args.args[0]
    assert payload["job_score_threshold"] == 90
    assert "sms_score_threshold" not in payload


def test_patch_empty_body_is_noop(client: TestClient) -> None:
    """An empty PATCH body issues no UPDATE and 200s with the current row."""
    supabase = MagicMock()
    _wire_select(supabase, [_row(job_score_threshold=50, sms_score_threshold=70)])
    app.dependency_overrides[get_supabase] = lambda: supabase

    resp = client.patch("/targets/target-1/notification-thresholds", json={})

    assert resp.status_code == 200
    supabase.table.return_value.update.assert_not_called()
    assert resp.json()["sms_score_threshold"] == 70


def test_patch_idor_other_users_link_404(client: TestClient) -> None:
    """A user whose (user, target) link doesn't exist gets 404 and no write —
    the service-role client can't be steered onto another user's row."""
    supabase = MagicMock()
    _wire_select(supabase, [])  # no link for this (user, target)
    app.dependency_overrides[get_supabase] = lambda: supabase

    resp = client.patch(
        "/targets/target-1/notification-thresholds",
        json={"job_score_threshold": 10, "sms_score_threshold": 10},
    )

    assert resp.status_code == 404
    supabase.table.return_value.update.assert_not_called()


@pytest.mark.parametrize("value,expected", [(0, 200), (200, 200), (-1, 422), (201, 422)])
def test_patch_boundary_validation(client: TestClient, value: int, expected: int) -> None:
    supabase = MagicMock()
    _wire_select(supabase, [_row()])
    _wire_update(supabase, [_row(job_score_threshold=value if value in (0, 200) else None)])
    app.dependency_overrides[get_supabase] = lambda: supabase

    resp = client.patch(
        "/targets/target-1/notification-thresholds",
        json={"job_score_threshold": value},
    )
    assert resp.status_code == expected


def test_patch_404_when_no_link(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.routers import targets as router_mod

    monkeypatch.setattr(
        router_mod.crud, "set_user_target_notification_thresholds", lambda *_a, **_kw: None
    )

    resp = client.patch(
        "/targets/target-1/notification-thresholds",
        json={"job_score_threshold": 90, "sms_score_threshold": 70},
    )
    assert resp.status_code == 404


def test_patch_rejects_out_of_range(client: TestClient) -> None:
    resp = client.patch(
        "/targets/target-1/notification-thresholds",
        json={"job_score_threshold": 250, "sms_score_threshold": 70},
    )
    assert resp.status_code == 422
