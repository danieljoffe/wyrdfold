"""Cross-tenant authz on the shared ``targets`` catalog (2026-07-16 security audit).

``targets`` is a shared resource: ``find_matching_target`` dedups new targets by
normalized label across users, so distinct users routinely co-follow one row. Two
router routes mishandled that:

- **SEC-1 (HIGH)** ``DELETE /targets/{id}`` hard-deleted the shared row for a JWT
  caller — cascading away every co-follower's scores/feedback/analyses/learning.
  A JWT caller must only drop THEIR ``user_targets`` link; operators keep the hard
  delete.
- **SEC-4 (MED)** ``POST /targets/{id}/deactivate`` was missing the
  ``_require_user_owns_target`` guard every sibling route has, so any JWT user could
  read back any target's full metadata.
- **SEC-2 (HIGH)** ``PATCH /targets/{id}`` let any co-follower overwrite the shared
  scoring profile / label — uncapped, bypassing the #191 merge machinery. Direct
  field edits are now operator-only; end users tune their own ``user_targets`` view
  or contribute through the bounded #191 path (reference JDs, learner review).
- **SEC-3 (MED)** ``POST /targets/{id}/derive-profile`` re-derived and overwrote the
  shared profile from the caller's experience. It had no legitimate caller and is
  removed entirely.

These pin all four fixes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.dependencies import (
    get_current_user_id,
    get_current_user_id_optional,
    get_supabase,
    verify_api_key_or_jwt,
)
from app.main import app
from app.models.targets import JobTarget, ScoringProfile


def _job_target() -> JobTarget:
    now = datetime.now(UTC)
    return JobTarget(
        id="target-1",
        label="Software Engineer",
        scoring_profile=ScoringProfile(),
        is_active=True,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def _client(user_id: str | None) -> TestClient:
    app.dependency_overrides[get_supabase] = lambda: MagicMock()
    app.dependency_overrides[get_current_user_id_optional] = lambda: user_id
    # get_current_user_id is JWT-required; the deactivate route uses it. When a
    # test exercises the operator path (user_id is None) it only touches DELETE,
    # which uses the optional dep, so a dummy here is fine.
    app.dependency_overrides[get_current_user_id] = lambda: user_id or "operator"
    app.dependency_overrides[verify_api_key_or_jwt] = lambda: user_id or "operator"
    return TestClient(app)


# --------------------------------------------------------------------------
# SEC-1 — DELETE unlinks for a JWT caller, never hard-deletes the shared row
# --------------------------------------------------------------------------


def test_delete_by_jwt_owner_unlinks_and_never_hard_deletes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routers import targets as mod

    monkeypatch.setattr(mod.crud, "get_user_target_ids", lambda *_a, **_k: {"target-1"})
    unlink = MagicMock(return_value=True)
    hard_delete = MagicMock(return_value=True)
    monkeypatch.setattr(mod.crud, "unlink_user_from_target", unlink)
    monkeypatch.setattr(mod.crud, "delete", hard_delete)

    resp = _client("user-1").delete("/targets/target-1")

    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
    # THE regression: the shared-row hard delete must NOT run for a JWT caller.
    hard_delete.assert_not_called()
    # Only the caller's own link is dropped (positional: supabase, user_id, target_id).
    unlink.assert_called_once()
    assert unlink.call_args.args[1:] == ("user-1", "target-1")


def test_delete_by_jwt_caller_unlinks_missing_link_is_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routers import targets as mod

    # Owns the target (passes the guard) but the unlink deletes zero rows
    # (already gone) → 404, still no hard delete.
    monkeypatch.setattr(mod.crud, "get_user_target_ids", lambda *_a, **_k: {"target-1"})
    hard_delete = MagicMock(return_value=True)
    monkeypatch.setattr(mod.crud, "unlink_user_from_target", lambda *_a, **_k: False)
    monkeypatch.setattr(mod.crud, "delete", hard_delete)

    resp = _client("user-1").delete("/targets/target-1")

    assert resp.status_code == 404
    hard_delete.assert_not_called()


def test_delete_by_non_owner_jwt_is_404_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routers import targets as mod

    monkeypatch.setattr(mod.crud, "get_user_target_ids", lambda *_a, **_k: set())
    unlink = MagicMock(return_value=True)
    hard_delete = MagicMock(return_value=True)
    monkeypatch.setattr(mod.crud, "unlink_user_from_target", unlink)
    monkeypatch.setattr(mod.crud, "delete", hard_delete)

    resp = _client("attacker").delete("/targets/target-1")

    assert resp.status_code == 404
    unlink.assert_not_called()
    hard_delete.assert_not_called()


def test_delete_by_operator_still_hard_deletes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routers import targets as mod

    # Operator path (user_id is None): _require_user_owns_target early-returns,
    # and the shared-row hard delete is the intended behavior.
    unlink = MagicMock(return_value=True)
    hard_delete = MagicMock(return_value=True)
    monkeypatch.setattr(mod.crud, "unlink_user_from_target", unlink)
    monkeypatch.setattr(mod.crud, "delete", hard_delete)

    resp = _client(None).delete("/targets/target-1")

    assert resp.status_code == 200
    hard_delete.assert_called_once()
    unlink.assert_not_called()


# --------------------------------------------------------------------------
# SEC-4 — deactivate is ownership-gated (no metadata leak to non-followers)
# --------------------------------------------------------------------------


def test_deactivate_non_owner_is_404_and_reads_no_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routers import targets as mod

    monkeypatch.setattr(mod.crud, "get_user_target_ids", lambda *_a, **_k: set())
    get_spy = MagicMock(return_value=_job_target())
    inactivate = MagicMock()
    monkeypatch.setattr(mod.crud, "get", get_spy)
    monkeypatch.setattr(mod.crud, "set_user_target_inactive", inactivate)

    resp = _client("attacker").post("/targets/target-1/deactivate")

    assert resp.status_code == 404
    # The guard must fire BEFORE the shared row is read/returned — no leak.
    get_spy.assert_not_called()
    inactivate.assert_not_called()


def test_deactivate_owner_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.routers import targets as mod

    monkeypatch.setattr(mod.crud, "get_user_target_ids", lambda *_a, **_k: {"target-1"})
    monkeypatch.setattr(mod.crud, "get", lambda *_a, **_k: _job_target())
    inactivate = MagicMock()
    monkeypatch.setattr(mod.crud, "set_user_target_inactive", inactivate)

    resp = _client("user-1").post("/targets/target-1/deactivate")

    assert resp.status_code == 200
    assert resp.json()["id"] == "target-1"
    inactivate.assert_called_once()


# --------------------------------------------------------------------------
# SEC-2 — the shared row is view-only for end users; edits are operator-only
# --------------------------------------------------------------------------


def test_update_target_by_jwt_user_is_403_and_never_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routers import targets as mod

    update = MagicMock(return_value=_job_target())
    monkeypatch.setattr(mod.crud, "update", update)

    resp = _client("user-1").patch("/targets/target-1", json={"label": "hacked"})

    assert resp.status_code == 403
    # THE regression: a signed-in user must never write the shared catalog row,
    # even one they follow. The operator gate fires before any write.
    update.assert_not_called()


def test_update_target_by_operator_still_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routers import targets as mod

    # Operator path (user_id is None): curation via the api-key is preserved.
    update = MagicMock(return_value=_job_target())
    monkeypatch.setattr(mod.crud, "update", update)

    resp = _client(None).patch("/targets/target-1", json={"label": "Renamed"})

    assert resp.status_code == 200
    assert resp.json()["id"] == "target-1"
    update.assert_called_once()


# --------------------------------------------------------------------------
# SEC-3 — the user-triggered shared-profile re-derive route is gone
# --------------------------------------------------------------------------


def test_derive_profile_route_is_removed() -> None:
    # The route overwrote the shared profile from the caller's experience and
    # had no FE/internal caller — removed rather than gated.
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/targets/{target_id}/derive-profile" not in paths
    # And it is unreachable over HTTP.
    resp = _client("user-1").post("/targets/target-1/derive-profile")
    assert resp.status_code == 404
