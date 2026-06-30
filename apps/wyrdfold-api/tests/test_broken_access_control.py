"""Broken-access-control regression tests (audit #29 round 3).

H1 — DELETE /jobs/{posting_id} must NOT hard-delete the shared catalog row
     (FK CASCADE wiped every other user's scores/feedback/status/user_jobs).
     The user-facing delete is a per-user soft archive of the caller's own
     ``user_jobs`` row; the shared ``jobs`` row is never touched.
M2 — GET /targets/{id}/status must reject callers not linked to the target.
M3 — GET /targets/{id} and GET /targets/{id}/reference-jds must reject
     non-owners (404), and reference-jds must NOT leak contributor user_ids.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from app.dependencies import (
    get_current_user_id,
    get_current_user_id_optional,
    get_supabase,
    verify_api_key_or_jwt,
)
from app.services.targets import crud


def _client_with_overrides(overrides: dict[Any, Any]) -> Any:
    from fastapi.testclient import TestClient

    from app.main import app

    app.dependency_overrides.update(overrides)
    return TestClient(app)


# ---------------------------------------------------------------------------
# H1 — DELETE /jobs/{id} is a per-user archive, never a shared hard-delete
# ---------------------------------------------------------------------------


def _owned_posting_supabase() -> tuple[MagicMock, dict[str, MagicMock]]:
    """A service-role client mock where ``_assert_user_owns_posting`` passes.

    The helper queries ``jobs`` (exists), ``user_targets`` (caller's target
    ids), then ``scores`` (a score row for the posting under one of those
    targets). Return data for each so ownership resolves True. Per-name table
    mocks are cached + returned so the test can inspect whether ``.delete()``
    was ever called on the shared ``jobs`` table.
    """
    supabase = MagicMock()
    tables: dict[str, MagicMock] = {}

    def _table(name: str) -> MagicMock:
        if name in tables:
            return tables[name]
        tbl = MagicMock()
        if name == "jobs":
            (
                tbl.select.return_value.eq.return_value.limit.return_value.execute.return_value.data
            ) = [{"id": "job-1"}]
        elif name == "user_targets":
            (
                tbl.select.return_value.eq.return_value.execute.return_value.data
            ) = [{"target_id": "tgt-a"}]
        elif name == "scores":
            (
                tbl.select.return_value.eq.return_value.in_.return_value.order.return_value.limit.return_value.execute.return_value.data
            ) = [{"target_id": "tgt-a", "score": 90, "score_breakdown": {}}]
        tables[name] = tbl
        return tbl

    supabase.table.side_effect = _table
    return supabase, tables


def test_delete_job_archives_caller_user_jobs_never_deletes_shared_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route soft-archives the caller's own user_jobs row and never
    issues a ``jobs.delete()`` against the shared catalog (audit #29 H1)."""
    from app.routers import jobs as jobs_mod

    supabase, tables = _owned_posting_supabase()

    upsert_calls: list[dict[str, Any]] = []

    def _fake_upsert(_sb: Any, **kwargs: Any) -> None:
        upsert_calls.append(kwargs)

    monkeypatch.setattr(jobs_mod.persistence, "upsert_user_job", _fake_upsert)

    from app.main import app

    tc = _client_with_overrides(
        {
            get_supabase: lambda: supabase,
            get_current_user_id: lambda: "user-a",
            verify_api_key_or_jwt: lambda: "user-a",
        }
    )
    try:
        resp = tc.delete("/jobs/job-1")
        assert resp.status_code == 200
        assert resp.json() == {"success": True, "deleted_id": "job-1"}
    finally:
        app.dependency_overrides.clear()

    # Per-user archive happened, scoped to the caller.
    assert upsert_calls == [
        {"user_id": "user-a", "job_posting_id": "job-1", "status": "archived"}
    ]
    # The shared `jobs` row was NEVER hard-deleted — that was the regression
    # that cascade-wiped every other user's data. `jobs` is still *read* for
    # the ownership check, but `.delete()` must never be issued on it.
    assert "jobs" in tables  # sanity: ownership path did read `jobs`
    assert tables["jobs"].delete.call_count == 0


def test_delete_job_unowned_posting_is_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller with no scored target for the posting → 404, no archive."""
    from app.routers import jobs as jobs_mod

    supabase = MagicMock()

    def _table(name: str) -> MagicMock:
        tbl = MagicMock()
        if name == "jobs":
            (
                tbl.select.return_value.eq.return_value.limit.return_value.execute.return_value.data
            ) = [{"id": "job-1"}]
        elif name == "user_targets":
            # Caller follows no targets → unowned.
            tbl.select.return_value.eq.return_value.execute.return_value.data = []
        return tbl

    supabase.table.side_effect = _table

    upsert = MagicMock()
    monkeypatch.setattr(jobs_mod.persistence, "upsert_user_job", upsert)

    from app.main import app

    tc = _client_with_overrides(
        {
            get_supabase: lambda: supabase,
            get_current_user_id: lambda: "user-b",
            verify_api_key_or_jwt: lambda: "user-b",
        }
    )
    try:
        resp = tc.delete("/jobs/job-1")
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()

    upsert.assert_not_called()
    assert supabase.table.return_value.delete.call_count == 0


# ---------------------------------------------------------------------------
# M2 — GET /targets/{id}/status ownership gate
# ---------------------------------------------------------------------------


def test_target_status_unowned_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(crud, "get_user_target_ids", lambda *_a, **_kw: {"tgt-mine"})
    crud_get = MagicMock()
    monkeypatch.setattr(crud, "get", crud_get)

    from app.main import app

    tc = _client_with_overrides(
        {
            get_supabase: lambda: MagicMock(),
            verify_api_key_or_jwt: lambda: "jwt",
            get_current_user_id_optional: lambda: "user-a",
        }
    )
    try:
        resp = tc.get("/targets/tgt-not-mine/status")
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()

    # 404'd on ownership BEFORE reading the target row.
    crud_get.assert_not_called()


def test_target_status_operator_bypasses(monkeypatch: pytest.MonkeyPatch) -> None:
    """api-key/operator path (user_id None) bypasses ownership."""

    def _get_user_target_ids(*_a: Any, **_kw: Any) -> set[str]:
        raise AssertionError("ownership check should be skipped for operators")

    monkeypatch.setattr(crud, "get_user_target_ids", _get_user_target_ids)

    target = MagicMock()
    target.activation_status = "ready"
    monkeypatch.setattr(crud, "get", lambda *_a, **_kw: target)

    supabase = MagicMock()
    (
        supabase.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value.count
    ) = 3

    from app.main import app

    tc = _client_with_overrides(
        {
            get_supabase: lambda: supabase,
            verify_api_key_or_jwt: lambda: None,
            get_current_user_id_optional: lambda: None,
        }
    )
    try:
        resp = tc.get("/targets/any-target/status")
        assert resp.status_code == 200
        assert resp.json()["jobs_count"] == 3
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# M3 — GET /targets/{id} ownership gate
# ---------------------------------------------------------------------------


def test_get_target_unowned_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(crud, "get_user_target_ids", lambda *_a, **_kw: {"tgt-mine"})
    crud_get = MagicMock()
    monkeypatch.setattr(crud, "get", crud_get)

    from app.main import app

    tc = _client_with_overrides(
        {
            get_supabase: lambda: MagicMock(),
            verify_api_key_or_jwt: lambda: "jwt",
            get_current_user_id_optional: lambda: "user-a",
        }
    )
    try:
        resp = tc.get("/targets/tgt-not-mine")
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()

    crud_get.assert_not_called()


# ---------------------------------------------------------------------------
# M3 — GET /targets/{id}/reference-jds ownership gate + user_id anonymization
# ---------------------------------------------------------------------------


def test_reference_jds_unowned_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(crud, "get_user_target_ids", lambda *_a, **_kw: {"tgt-mine"})
    list_ref = MagicMock()
    monkeypatch.setattr(crud, "list_reference_jds", list_ref)

    from app.main import app

    tc = _client_with_overrides(
        {
            get_supabase: lambda: MagicMock(),
            verify_api_key_or_jwt: lambda: "jwt",
            get_current_user_id_optional: lambda: "user-a",
        }
    )
    try:
        resp = tc.get("/targets/tgt-not-mine/reference-jds")
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()

    list_ref.assert_not_called()


def test_reference_jds_strips_contributor_user_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Owner can read the list, but contributor user_ids are stripped so the
    otherwise-anonymous contribution graph isn't deanonymized (audit #29 M3)."""
    from datetime import UTC, datetime

    from app.models.targets import ScoringProfile, TargetReferenceJD

    monkeypatch.setattr(crud, "get_user_target_ids", lambda *_a, **_kw: {"tgt-mine"})
    ref = TargetReferenceJD(
        id="ref-1",
        target_id="tgt-mine",
        user_id="someone-else-private-id",
        jd_url=None,
        jd_text="JD body",
        extracted_profile=ScoringProfile(),
        suppressed=False,
        created_at=datetime.now(UTC),
    )
    monkeypatch.setattr(crud, "list_reference_jds", lambda *_a, **_kw: [ref])

    from app.main import app

    tc = _client_with_overrides(
        {
            get_supabase: lambda: MagicMock(),
            verify_api_key_or_jwt: lambda: "jwt",
            get_current_user_id_optional: lambda: "user-a",
        }
    )
    try:
        resp = tc.get("/targets/tgt-mine/reference-jds")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["reference_jds"]) == 1
        jd = body["reference_jds"][0]
        assert jd["jd_text"] == "JD body"
        # The private contributor id must NOT appear in the response.
        assert jd["user_id"] is None
        assert "someone-else-private-id" not in resp.text
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Contribution cap (#47) — bound a single user's footprint on a shared target
# ---------------------------------------------------------------------------


async def test_reference_jd_contribution_cap_rejects_over_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An over-cap reference-JD add is rejected 409 before any LLM derive (#47)."""
    from fastapi import HTTPException

    from app.config import settings
    from app.models.targets import ReferenceJDAdd
    from app.routers import targets

    monkeypatch.setattr(targets, "_require_user_owns_target", lambda *_a, **_kw: None)
    monkeypatch.setattr(crud, "get", lambda *_a, **_kw: MagicMock())
    # The caller is already at the cap.
    monkeypatch.setattr(
        crud,
        "count_user_reference_jds",
        lambda *_a, **_kw: settings.reference_jd_max_per_user_per_target,
    )

    with pytest.raises(HTTPException) as exc:
        await targets.add_reference_jd(
            request=MagicMock(),
            target_id="tgt-1",
            body=ReferenceJDAdd(jd_text="x" * 60),
            supabase=MagicMock(),
            llm=MagicMock(),
            user_id="user-a",
        )
    assert exc.value.status_code == 409
    assert "limit" in str(exc.value.detail).lower()
