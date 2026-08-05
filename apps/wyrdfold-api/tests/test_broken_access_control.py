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
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.dependencies import (
    get_async_service_supabase,
    get_async_user_supabase,
    get_current_user_id,
    get_current_user_id_optional,
    verify_api_key_or_jwt,
)


def _client_with_overrides(overrides: dict[Any, Any]) -> Any:
    from fastapi.testclient import TestClient

    from app.main import app

    app.dependency_overrides.update(overrides)
    return TestClient(app)


# ---------------------------------------------------------------------------
# H1 — DELETE /jobs/{id} is a per-user archive, never a shared hard-delete
# ---------------------------------------------------------------------------


def _owned_posting_supabase() -> tuple[MagicMock, dict[str, MagicMock]]:
    """An async RLS client mock where ``_assert_user_owns_posting_async`` passes.

    The probe awaits ``jobs`` (exists), ``user_targets`` (caller's target ids),
    then ``scores`` (a score row for the posting under one of those targets), so
    each terminal ``.execute`` is an ``AsyncMock``; the archive write awaits a
    ``user_jobs`` upsert. Per-name table mocks are cached + returned so the test
    can inspect the archive payload and that ``.delete()`` never fired on
    ``jobs`` (#57 slice 4 made ``delete_job`` async)."""
    supabase = MagicMock()
    tables: dict[str, MagicMock] = {}

    def _table(name: str) -> MagicMock:
        if name in tables:
            return tables[name]
        tbl = MagicMock()
        if name == "jobs":
            tbl.select.return_value.eq.return_value.limit.return_value.execute = AsyncMock(
                return_value=MagicMock(data=[{"id": "job-1"}])
            )
        elif name == "user_targets":
            tbl.select.return_value.eq.return_value.execute = AsyncMock(
                return_value=MagicMock(data=[{"target_id": "tgt-a"}])
            )
        elif name == "scores":
            tbl.select.return_value.eq.return_value.in_.return_value.order.return_value.limit.return_value.execute = AsyncMock(
                return_value=MagicMock(data=[{"target_id": "tgt-a", "score": 90, "score_breakdown": {}}])
            )
        elif name == "user_jobs":
            tbl.upsert.return_value.execute = AsyncMock(return_value=MagicMock(data=None))
        tables[name] = tbl
        return tbl

    supabase.table.side_effect = _table
    return supabase, tables


def test_delete_job_archives_caller_user_jobs_never_deletes_shared_row() -> None:
    """The route soft-archives the caller's own user_jobs row and never
    issues a ``jobs.delete()`` against the shared catalog (audit #29 H1)."""
    supabase, tables = _owned_posting_supabase()

    from app.main import app

    tc = _client_with_overrides(
        {
            get_async_user_supabase: lambda: supabase,
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

    # Per-user archive happened, scoped to the caller, on the user_jobs table.
    archive_payload = tables["user_jobs"].upsert.call_args.args[0]
    assert archive_payload["user_id"] == "user-a"
    assert archive_payload["job_posting_id"] == "job-1"
    assert archive_payload["status"] == "archived"
    # The shared `jobs` row was NEVER hard-deleted — that was the regression
    # that cascade-wiped every other user's data. `jobs` is still *read* for
    # the ownership check, but `.delete()` must never be issued on it.
    assert "jobs" in tables  # sanity: ownership path did read `jobs`
    assert tables["jobs"].delete.call_count == 0


def test_delete_job_unowned_posting_is_404() -> None:
    """A caller with no scored target for the posting → 404, no archive."""
    supabase = MagicMock()
    tables: dict[str, MagicMock] = {}

    def _table(name: str) -> MagicMock:
        if name in tables:
            return tables[name]
        tbl = MagicMock()
        if name == "jobs":
            tbl.select.return_value.eq.return_value.limit.return_value.execute = AsyncMock(
                return_value=MagicMock(data=[{"id": "job-1"}])
            )
        elif name == "user_targets":
            # Caller follows no targets → unowned.
            tbl.select.return_value.eq.return_value.execute = AsyncMock(
                return_value=MagicMock(data=[])
            )
        tables[name] = tbl
        return tbl

    supabase.table.side_effect = _table

    from app.main import app

    tc = _client_with_overrides(
        {
            get_async_user_supabase: lambda: supabase,
            get_current_user_id: lambda: "user-b",
            verify_api_key_or_jwt: lambda: "user-b",
        }
    )
    try:
        resp = tc.delete("/jobs/job-1")
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()

    # No archive write on an unowned posting.
    assert "user_jobs" not in tables


# ---------------------------------------------------------------------------
# M2 — GET /targets/{id}/status ownership gate
# ---------------------------------------------------------------------------


def test_target_status_unowned_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    # #57 slice 3: the handler inlines the crud reads as async helpers.
    from app.routers import targets as targets_mod

    monkeypatch.setattr(targets_mod, "_user_target_ids", AsyncMock(return_value={"tgt-mine"}))
    target_get = AsyncMock()
    monkeypatch.setattr(targets_mod, "_target_get", target_get)

    from app.main import app

    tc = _client_with_overrides(
        {
            get_async_service_supabase: lambda: MagicMock(),
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
    target_get.assert_not_called()


def test_target_status_operator_bypasses(monkeypatch: pytest.MonkeyPatch) -> None:
    """api-key/operator path (user_id None) bypasses ownership."""

    from app.routers import targets as targets_mod

    def _user_target_ids(*_a: Any, **_kw: Any) -> set[str]:
        raise AssertionError("ownership check should be skipped for operators")

    monkeypatch.setattr(targets_mod, "_user_target_ids", _user_target_ids)

    target = MagicMock()
    target.activation_status = "ready"
    monkeypatch.setattr(targets_mod, "_target_get", AsyncMock(return_value=target))

    supabase = MagicMock()
    # Count chain: .select(count).eq(target_id).eq(excluded).eq(job_is_live).
    # The handler now AWAITS the count round-trip on the async client, so the
    # terminal ``.execute`` is an AsyncMock; the rest of the chain stays a plain
    # MagicMock so the call-arg introspection below still works.
    (
        supabase.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.execute
    ) = AsyncMock(return_value=MagicMock(count=3))

    from app.main import app

    tc = _client_with_overrides(
        {
            get_async_service_supabase: lambda: supabase,
            verify_api_key_or_jwt: lambda: None,
            get_current_user_id_optional: lambda: None,
        }
    )
    try:
        resp = tc.get("/targets/any-target/status")
        assert resp.status_code == 200
        assert resp.json()["jobs_count"] == 3
        # Regression guard: the count read must pass head=True so PostgREST
        # returns only the count (HEAD), never the up-to-~13k id rows it
        # would otherwise ship and the endpoint discards. Assert every
        # count=exact select on this path carried head=True.
        count_selects = [
            c for c in supabase.table.return_value.select.call_args_list if "count" in c.kwargs
        ]
        assert count_selects, "expected a count=exact select on the status path"
        assert all(c.kwargs.get("head") is True for c in count_selects)
        # And it must gate ``job_is_live`` so the count reflects the live
        # list, not the raw not-excluded scored count (which over-reports
        # ~4.5x with archived jobs — 12,853 vs 2,808 on the heaviest target).
        live_eq = supabase.table.return_value.select.return_value.eq.return_value.eq.return_value.eq
        live_eq.assert_called_with("job_is_live", True)
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# M3 — GET /targets/{id} ownership gate
# ---------------------------------------------------------------------------


def test_get_target_unowned_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.routers import targets as targets_mod

    monkeypatch.setattr(targets_mod, "_user_target_ids", AsyncMock(return_value={"tgt-mine"}))
    target_get = AsyncMock()
    monkeypatch.setattr(targets_mod, "_target_get", target_get)

    from app.main import app

    tc = _client_with_overrides(
        {
            get_async_service_supabase: lambda: MagicMock(),
            verify_api_key_or_jwt: lambda: "jwt",
            get_current_user_id_optional: lambda: "user-a",
        }
    )
    try:
        resp = tc.get("/targets/tgt-not-mine")
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()

    target_get.assert_not_called()


# ---------------------------------------------------------------------------
# M3 — GET /targets/{id}/reference-jds ownership gate + user_id anonymization
# ---------------------------------------------------------------------------


def test_reference_jds_unowned_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.routers import targets as targets_mod

    monkeypatch.setattr(targets_mod, "_user_target_ids", AsyncMock(return_value={"tgt-mine"}))
    list_ref = AsyncMock()
    monkeypatch.setattr(targets_mod, "_list_reference_jds_async", list_ref)

    from app.main import app

    tc = _client_with_overrides(
        {
            get_async_service_supabase: lambda: MagicMock(),
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
    from app.routers import targets as targets_mod

    monkeypatch.setattr(targets_mod, "_user_target_ids", AsyncMock(return_value={"tgt-mine"}))
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
    monkeypatch.setattr(targets_mod, "_list_reference_jds_async", AsyncMock(return_value=[ref]))

    from app.main import app

    tc = _client_with_overrides(
        {
            get_async_service_supabase: lambda: MagicMock(),
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

    # #57 PR-G2b: the handler ownership-checks + reads the target + counts
    # contributions on the async service client (router inline helpers); the
    # whole reference-JD path is async now, so no sync client is acquired.
    async def _owns(*_a: object, **_kw: object) -> None:
        return None

    monkeypatch.setattr(targets, "_require_user_owns_target_async", _owns)
    monkeypatch.setattr(targets, "_target_get", AsyncMock(return_value=MagicMock()))
    # The caller is already at the cap.
    monkeypatch.setattr(
        targets,
        "_count_user_reference_jds_async",
        AsyncMock(return_value=settings.reference_jd_max_per_user_per_target),
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
