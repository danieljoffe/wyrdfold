"""Router tests for POST /jobs/{job_id}/add-to-target (#467 power-action).

The endpoint scores an EXISTING posting (a search result's ``jobs.id``) against
one of the caller's own targets and saves it to their pipeline. The security
wiring — post the SEC-H2 lockdown (migration 20260718010000):

  * the score write + force-include + user_job save all go on the SERVICE-ROLE
    client — the user_upsert_score / user_set_scores_included RPCs are no longer
    ``authenticated``-executable (their follower-check is insufficient for a
    shared, ownerless target catalog), so with auth.uid() NULL they take the
    service-role-exempt branch — exactly like ``POST /jobs/manual``;
  * OWNERSHIP is enforced in the API: ``get_user_target`` on the caller's
    (RLS-backstopped) client — a target the caller doesn't follow 404s with the
    same message as a missing one (no cross-tenant id leak);
  * a dead / unknown posting 404s;
  * it NEVER calls ``materialize_and_score_job`` (which would duplicate the row
    under the manual pseudo-source).

(The earlier caller-client "gated" path 502'd in prod — the grant was revoked;
this pins the corrected wiring.)
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.dependencies import (
    get_current_user_id,
    get_supabase,
    get_user_supabase,
    verify_api_key_or_jwt,
)
from app.main import app
from app.models.schemas import JobTargetScore

_USER_ID = "00000000-0000-0000-0000-000000000001"
_TARGET_ID = "11111111-1111-1111-1111-111111111111"
_JOB_ID = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def overrides():
    yield
    app.dependency_overrides.clear()


# The router only checks the target is non-None before handing it to the
# (stubbed) scorer, so a sentinel stands in for a fully-built JobTarget.
def _fake_target() -> object:
    return object()


def _fake_score(score: int = 71) -> JobTargetScore:
    from datetime import UTC, datetime

    now = datetime(2026, 1, 1, tzinfo=UTC)
    return JobTargetScore(
        id="s1",
        job_posting_id=_JOB_ID,
        target_id=_TARGET_ID,
        score=score,
        score_breakdown=None,
        matched_keywords=[],
        excluded=False,
        scoring_status="stage2",
        created_at=now,
        updated_at=now,
    )


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    user_sb: MagicMock,
    service_sb: MagicMock,
    job: dict[str, Any] | None,
    follows_target: bool,
    target: object | None,
    captured: dict[str, object],
) -> None:
    """Patch the router's collaborators and capture which client each receives."""
    monkeypatch.setattr("app.routers.jobs._load_live_job", lambda sb, jid: job)

    def fake_get_user_target(sb, uid, tid):
        captured["ownership_client"] = sb
        return object() if follows_target else None

    def fake_get_target(sb, tid):
        return target

    def fake_score_and_upsert(sb, **kwargs):
        captured["score_client"] = sb
        captured["score_kwargs"] = kwargs
        return _fake_score()


    def fake_upsert_user_job(sb, **kwargs):
        captured["userjob_client"] = sb
        captured["userjob_kwargs"] = kwargs

    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("materialize_and_score_job must never run on the add path")

    monkeypatch.setattr("app.routers.jobs.get_user_target", fake_get_user_target)
    monkeypatch.setattr("app.routers.jobs.get_target", fake_get_target)
    monkeypatch.setattr("app.routers.jobs.score_and_upsert", fake_score_and_upsert)
    monkeypatch.setattr("app.routers.jobs.materialize_and_score_job", _boom)
    monkeypatch.setattr("app.routers.jobs.persistence.upsert_user_job", fake_upsert_user_job)
    app.dependency_overrides[verify_api_key_or_jwt] = lambda: "jwt"
    app.dependency_overrides[get_supabase] = lambda: service_sb
    app.dependency_overrides[get_user_supabase] = lambda: user_sb
    app.dependency_overrides[get_current_user_id] = lambda: _USER_ID


def test_add_to_target_writes_via_service_role_ownership_checked_on_caller(
    overrides: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_sb = MagicMock(name="user_client")
    service_sb = MagicMock(name="service_client")
    captured: dict[str, object] = {}
    _wire(
        monkeypatch,
        user_sb=user_sb,
        service_sb=service_sb,
        job={"id": _JOB_ID, "title": "Frontend Engineer", "description_html": "<p>JD</p>"},
        follows_target=True,
        target=_fake_target(),
        captured=captured,
    )

    r = TestClient(app).post(f"/jobs/{_JOB_ID}/add-to-target", json={"target_id": _TARGET_ID})

    assert r.status_code == 200
    assert r.json() == {
        "success": True,
        "job_posting_id": _JOB_ID,
        "target_id": _TARGET_ID,
        "score": 71,
    }
    # Post-lockdown: ALL score writes go on the service-role client (the RPCs
    # aren't authenticated-executable); ownership was checked on the caller's
    # client. This is the regression the prod 502 exposed.
    assert captured["ownership_client"] is user_sb  # get_user_target on caller
    assert captured["score_client"] is service_sb
    assert captured["score_kwargs"]["gated"] is True
    assert captured["score_kwargs"]["job_posting_id"] == _JOB_ID
    assert captured["userjob_client"] is service_sb
    assert captured["userjob_kwargs"]["status"] == "saved"
    # Force-include goes through the RPC on the service-role client, scoped to
    # this one target; the caller client is NEVER used for a score write.
    service_sb.rpc.assert_called_once_with(
        "user_set_scores_included",
        {"p_job_posting_id": _JOB_ID, "p_target_ids": [_TARGET_ID]},
    )
    user_sb.rpc.assert_not_called()


def test_add_to_target_404s_when_user_does_not_follow_target(
    overrides: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_sb = MagicMock(name="user_client")
    service_sb = MagicMock(name="service_client")
    captured: dict[str, object] = {}
    _wire(
        monkeypatch,
        user_sb=user_sb,
        service_sb=service_sb,
        job={"id": _JOB_ID, "title": "Frontend Engineer", "description_html": "x"},
        follows_target=False,  # caller isn't linked to this target
        target=_fake_target(),
        captured=captured,
    )

    r = TestClient(app).post(f"/jobs/{_JOB_ID}/add-to-target", json={"target_id": _TARGET_ID})

    assert r.status_code == 404
    # Same message a missing target would give — no leak of another user's target.
    assert r.json()["detail"] == "Target not found for user"
    # Nothing was written.
    assert "score_client" not in captured
    user_sb.rpc.assert_not_called()


def test_add_to_target_404s_for_dead_or_unknown_job(
    overrides: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_sb = MagicMock(name="user_client")
    service_sb = MagicMock(name="service_client")
    captured: dict[str, object] = {}
    _wire(
        monkeypatch,
        user_sb=user_sb,
        service_sb=service_sb,
        job=None,  # dead / purged / unknown id → live-predicate returns nothing
        follows_target=True,
        target=_fake_target(),
        captured=captured,
    )

    r = TestClient(app).post(f"/jobs/{_JOB_ID}/add-to-target", json={"target_id": _TARGET_ID})

    assert r.status_code == 404
    assert r.json()["detail"] == "Job not found"
    assert "ownership_client" not in captured  # short-circuits before ownership


def _query_stub(rows: list[dict[str, Any]]) -> tuple[MagicMock, MagicMock]:
    """A supabase mock whose every filter method returns the same query builder,
    so a chained ``.select().eq().is_()...`` lands its calls on one object we can
    assert against. ``.execute().data`` yields ``rows``."""
    q = MagicMock(name="query")
    q.select.return_value = q
    q.eq.return_value = q
    q.is_.return_value = q
    q.not_.is_.return_value = q
    q.limit.return_value = q
    q.execute.return_value.data = rows
    sb = MagicMock(name="supabase")
    sb.table.return_value = q
    return sb, q


def test_load_live_job_applies_the_live_us_predicate() -> None:
    """Only a LIVE, US posting is addable — the loader must mirror search's
    predicate exactly (archived_at IS NULL, purged_at IS NULL, is_us IS NOT
    FALSE) so a dead/purged/non-US id can't be resurrected into a pipeline."""
    from app.routers.jobs import _load_live_job

    sb, q = _query_stub([{"id": _JOB_ID, "title": "T", "description_html": "D"}])
    row = _load_live_job(sb, _JOB_ID)

    assert row == {"id": _JOB_ID, "title": "T", "description_html": "D"}
    sb.table.assert_called_once_with("jobs")
    q.eq.assert_any_call("id", _JOB_ID)
    q.is_.assert_any_call("archived_at", "null")
    q.is_.assert_any_call("purged_at", "null")
    q.not_.is_.assert_any_call("is_us", "false")


def test_load_live_job_returns_none_when_no_row_matches() -> None:
    from app.routers.jobs import _load_live_job

    sb, _q = _query_stub([])
    assert _load_live_job(sb, _JOB_ID) is None
