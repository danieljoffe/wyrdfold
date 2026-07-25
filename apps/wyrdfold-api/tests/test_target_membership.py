"""Target-membership endpoint (#467 §11) — the pipeline-state data source.

The security crux (this endpoint reads the ``scores`` table): a caller may only
learn which of **their own** targets contain a listing. These pin that the
``scores`` read is filtered to target ids derived from the caller's
``user_targets`` — never from request input — so no cross-user membership leak.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.dependencies import (
    get_current_user_id,
    get_user_supabase,
    verify_api_key_or_jwt,
)
from app.main import app
from app.routers.target_membership import _membership

_USER = "11111111-1111-1111-1111-111111111111"


def teardown_function() -> None:
    app.dependency_overrides.clear()


def _mock_sb(
    *,
    user_target_ids: list[str],
    labels: dict[str, str],
    score_rows: list[dict[str, Any]],
    capture: dict[str, Any] | None = None,
) -> MagicMock:
    """A supabase stub with a distinct self-returning query builder per table.
    ``capture`` records the ``target_id`` filter applied to the scores read."""

    def _table(name: str) -> MagicMock:
        qb = MagicMock(name=f"qb-{name}")
        qb.select.return_value = qb
        qb.eq.return_value = qb

        def _in(col: str, vals: list[str]) -> MagicMock:
            if name == "scores" and col == "target_id" and capture is not None:
                capture["scores_target_filter"] = list(vals)
            return qb

        qb.in_.side_effect = _in
        data = {
            "user_targets": [{"target_id": t} for t in user_target_ids],
            "targets": [{"id": t, "label": labels.get(t)} for t in user_target_ids],
            "scores": score_rows,
        }[name]
        qb.execute.return_value.data = data
        return qb

    sb = MagicMock(name="supabase")
    sb.table.side_effect = _table
    return sb


def test_membership_maps_listings_to_the_callers_targets() -> None:
    sb = _mock_sb(
        user_target_ids=["t1", "t2"],
        labels={"t1": "Frontend Roles", "t2": "Staff SWE"},
        score_rows=[
            {"job_posting_id": "a", "target_id": "t1"},
            {"job_posting_id": "a", "target_id": "t2"},
            {"job_posting_id": "b", "target_id": "t1"},
        ],
    )
    out = _membership(sb, _USER, ["a", "b", "c"])
    assert {r.target_id for r in out["a"]} == {"t1", "t2"}
    assert out["a"][0].label in {"Frontend Roles", "Staff SWE"}
    assert [r.target_id for r in out["b"]] == ["t1"]
    assert "c" not in out  # not in any of the caller's targets


def test_scores_read_is_scoped_to_the_callers_targets_not_request() -> None:
    """The authz guarantee: the scores read filters on the CALLER's own target
    ids (from user_targets) — a foreign target is never queried, so membership in
    another user's target can't leak, whatever ids the request carries."""
    capture: dict[str, Any] = {}
    sb = _mock_sb(
        user_target_ids=["mine-1"],
        labels={"mine-1": "Mine"},
        score_rows=[{"job_posting_id": "a", "target_id": "mine-1"}],
        capture=capture,
    )
    _membership(sb, _USER, ["a", "b"])
    # The scores read was bounded to the caller's own target ids — nothing else.
    assert capture["scores_target_filter"] == ["mine-1"]


def test_no_targets_or_no_ids_returns_empty() -> None:
    empty_ids = _mock_sb(user_target_ids=["t1"], labels={"t1": "X"}, score_rows=[])
    assert _membership(empty_ids, _USER, []) == {}  # no ids → no query

    no_targets = _mock_sb(user_target_ids=[], labels={}, score_rows=[])
    assert _membership(no_targets, _USER, ["a"]) == {}  # no targets → nothing


def test_endpoint_requires_auth_and_returns_memberships() -> None:
    sb = _mock_sb(
        user_target_ids=["t1"],
        labels={"t1": "Frontend Roles"},
        score_rows=[{"job_posting_id": "a", "target_id": "t1"}],
    )
    app.dependency_overrides[verify_api_key_or_jwt] = lambda: "jwt"
    app.dependency_overrides[get_current_user_id] = lambda: _USER
    app.dependency_overrides[get_user_supabase] = lambda: sb

    r = TestClient(app).post(
        "/jobs/target-membership", json={"job_posting_ids": ["a", "b"]}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["memberships"]["a"][0]["label"] == "Frontend Roles"
    assert "b" not in body["memberships"]
