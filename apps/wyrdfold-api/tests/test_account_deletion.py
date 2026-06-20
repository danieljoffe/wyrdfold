"""Account deletion / right-to-erasure (#29 P1).

Pins the erasure contract:

* every per-user table is deleted by ``user_id`` (+ ``notifications_sent``
  by the resolved ``user_profiles.id``);
* both storage buckets' ``<user_id>/`` objects are purged;
* the auth user is deleted **last** (after the data);
* the shared catalog (``jobs`` / ``targets`` / ``scores`` / ``sources``)
  is NEVER touched — the multi-tenant safety invariant;
* the route surfaces it behind JWT-only auth using the service-role
  client.

Uses an in-memory fake supabase (tables + storage + auth.admin) that
records an ordered op log, so ordering and "what got touched" are
assertable.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.services import account_deletion
from app.services.account_deletion import _USER_ID_TABLES

_UID = "u1"
_PROFILE_ID = "profile-1"

# Shared catalog — deleting any of these for a user would corrupt other
# tenants' data. The test asserts none are ever deleted.
_SHARED_TABLES = frozenset({"jobs", "targets", "scores", "sources"})


# ---- in-memory fakes --------------------------------------------------


class _FakeTableQuery:
    def __init__(self, name: str, rows: list[dict[str, Any]], log: list) -> None:
        self.name = name
        self._rows = rows
        self._log = log
        self._op: str | None = None
        self._filters: list[tuple[str, Any]] = []
        self._in_filters: list[tuple[str, list[Any]]] = []
        self._payload: dict[str, Any] = {}

    def delete(self) -> _FakeTableQuery:
        self._op = "delete"
        return self

    def select(self, _cols: str) -> _FakeTableQuery:
        self._op = "select"
        return self

    def update(self, payload: dict[str, Any]) -> _FakeTableQuery:
        self._op = "update"
        self._payload = payload
        return self

    def eq(self, col: str, val: Any) -> _FakeTableQuery:
        self._filters.append((col, val))
        return self

    def in_(self, col: str, vals: list[Any]) -> _FakeTableQuery:
        self._in_filters.append((col, list(vals)))
        return self

    def limit(self, _n: int) -> _FakeTableQuery:
        return self

    def _matches(self, row: dict[str, Any]) -> bool:
        return all(row.get(c) == v for c, v in self._filters) and all(
            row.get(c) in vals for c, vals in self._in_filters
        )

    def execute(self) -> SimpleNamespace:
        matched = [r for r in self._rows if self._matches(r)]
        self._log.append((self._op, self.name, dict(self._filters)))
        if self._op == "delete":
            self._rows[:] = [r for r in self._rows if not self._matches(r)]
        elif self._op == "update":
            for row in matched:
                row.update(self._payload)
        return SimpleNamespace(data=matched)


class _FakeBucket:
    def __init__(self, name: str, objects: dict[str, dict[str, list[str]]], log: list):
        self.name = name
        self._objects = objects
        self._log = log
        self.removed: list[list[str]] = []

    def list(self, prefix: str) -> list[dict[str, str]]:
        return [{"name": n} for n in self._objects.get(self.name, {}).get(prefix, [])]

    def remove(self, paths: list[str]) -> list[dict[str, str]]:
        self._log.append(("storage_remove", self.name, paths))
        self.removed.append(list(paths))
        for p in paths:
            prefix, name = p.split("/", 1)
            files = self._objects.get(self.name, {}).get(prefix, [])
            if name in files:
                files.remove(name)
        return [{"name": p} for p in paths]


class _FakeStorage:
    def __init__(self, objects: dict[str, dict[str, list[str]]], log: list) -> None:
        self._objects = objects
        self._log = log
        self.buckets: dict[str, _FakeBucket] = {}

    def from_(self, name: str) -> _FakeBucket:
        b = self.buckets.get(name) or _FakeBucket(name, self._objects, self._log)
        self.buckets[name] = b
        return b


class _FakeAdmin:
    def __init__(self, log: list) -> None:
        self._log = log
        self.deleted: list[str] = []

    def delete_user(self, user_id: str, should_soft_delete: bool = False) -> None:
        self._log.append(("auth_delete", user_id, {}))
        self.deleted.append(user_id)


class _FakeSupabase:
    def __init__(
        self,
        tables: dict[str, list[dict[str, Any]]] | None = None,
        objects: dict[str, dict[str, list[str]]] | None = None,
    ) -> None:
        self.tables = tables or {}
        self.log: list = []
        self.storage = _FakeStorage(objects or {}, self.log)
        self.auth = SimpleNamespace(admin=_FakeAdmin(self.log))

    def table(self, name: str) -> _FakeTableQuery:
        return _FakeTableQuery(name, self.tables.setdefault(name, []), self.log)


def _seeded() -> _FakeSupabase:
    tables: dict[str, list[dict[str, Any]]] = {
        "documents": [{"user_id": _UID}, {"user_id": _UID}],
        "user_targets": [{"user_id": _UID}],
        "job_feedback": [{"user_id": _UID}],
        "user_api_keys": [{"user_id": _UID, "provider": "openrouter"}],
        "user_profiles": [{"id": _PROFILE_ID, "user_id": _UID}],
        "notifications_sent": [
            {"user_profile_id": _PROFILE_ID},
            {"user_profile_id": _PROFILE_ID},
        ],
        # Shared catalog rows that must survive erasure.
        "scores": [{"target_id": "t1", "job_posting_id": "j1"}],
        "jobs": [{"id": "j1", "status": "applied"}],
        "targets": [{"id": "t1"}],
    }
    objects = {
        "resume-uploads": {_UID: ["up-1.pdf", "up-2.docx"]},
        "tailored-resumes": {_UID: ["r-1.docx"]},
    }
    return _FakeSupabase(tables, objects)


# ---- service ----------------------------------------------------------


def test_deletes_every_per_user_table_by_user_id() -> None:
    sb = _seeded()
    report = account_deletion.delete_account(sb, user_id=_UID)

    deleted = {(table, frozenset(filt.items())) for op, table, filt in sb.log if op == "delete"}
    expected_filter = frozenset({"user_id": _UID}.items())
    for table in _USER_ID_TABLES:
        assert (table, expected_filter) in deleted, table
        assert table in report


def test_notifications_deleted_by_resolved_profile_id() -> None:
    sb = _seeded()
    report = account_deletion.delete_account(sb, user_id=_UID)
    assert ("delete", "notifications_sent", {"user_profile_id": _PROFILE_ID}) in sb.log
    assert report["notifications_sent"] == 2


def test_shared_catalog_is_never_deleted() -> None:
    """The multi-tenant safety invariant: erasing one user must not delete
    rows from the shared catalog."""
    sb = _seeded()
    account_deletion.delete_account(sb, user_id=_UID)
    deleted_tables = {table for op, table, _ in sb.log if op == "delete"}
    assert deleted_tables.isdisjoint(_SHARED_TABLES)
    # And the shared rows are physically still present.
    assert sb.tables["scores"] == [{"target_id": "t1", "job_posting_id": "j1"}]
    assert sb.tables["jobs"] == [{"id": "j1", "status": "applied"}]


def test_scrubs_shared_score_pii_for_user_targets() -> None:
    """Erasure nulls the Phase-2 grader PII on shared ``scores`` rows for
    the user's targets (without deleting the rows), re-opens them to grade,
    and leaves scores for *other* tenants' targets untouched."""
    tables: dict[str, list[dict[str, Any]]] = {
        "user_targets": [{"user_id": _UID, "target_id": "t1"}],
        "user_profiles": [{"id": _PROFILE_ID, "user_id": _UID}],
        "scores": [
            {
                "target_id": "t1",
                "job_posting_id": "j1",
                "score": 80,
                "fit_reasoning": "Your FightCamp work (Lighthouse +40)",
                "axis_scores": {"skills_fit": 90},
                "logistics_filters": {"remote": True},
                "scoring_status": "complete",
            },
            # Another tenant's target — must NOT be touched.
            {
                "target_id": "t2",
                "job_posting_id": "j1",
                "fit_reasoning": "Someone else's resume",
                "scoring_status": "complete",
            },
        ],
    }
    sb = _FakeSupabase(tables, {})
    report = account_deletion.delete_account(sb, user_id=_UID)

    scrubbed = next(r for r in sb.tables["scores"] if r["target_id"] == "t1")
    assert scrubbed["fit_reasoning"] is None
    assert scrubbed["axis_scores"] is None
    assert scrubbed["logistics_filters"] is None
    assert scrubbed["scoring_status"] == "stage2"
    assert scrubbed["score"] == 80  # numeric score left to re-grade
    assert report["scores_scrubbed"] == 1

    other = next(r for r in sb.tables["scores"] if r["target_id"] == "t2")
    assert other["fit_reasoning"] == "Someone else's resume"
    # The row was scrubbed, never deleted — still present.
    assert {r["target_id"] for r in sb.tables["scores"]} == {"t1", "t2"}
    assert "scores" not in {table for op, table, _ in sb.log if op == "delete"}


def test_no_targets_skips_score_scrub() -> None:
    """A user with no target links issues no scores update (no ``.in_([])``)."""
    sb = _seeded()  # seeded user_targets row carries no target_id
    report = account_deletion.delete_account(sb, user_id=_UID)
    assert report["scores_scrubbed"] == 0
    assert ("update", "scores", {}) not in sb.log


def test_both_storage_buckets_purged() -> None:
    sb = _seeded()
    report = account_deletion.delete_account(sb, user_id=_UID)
    assert report["resume_uploads_objects"] == 2
    assert report["tailored_resume_objects"] == 1
    assert sb.storage.buckets["resume-uploads"].removed == [
        [f"{_UID}/up-1.pdf", f"{_UID}/up-2.docx"]
    ]
    assert sb.storage.buckets["tailored-resumes"].removed == [[f"{_UID}/r-1.docx"]]


def test_auth_user_deleted_last() -> None:
    sb = _seeded()
    account_deletion.delete_account(sb, user_id=_UID)
    ops = [(op, table) for op, table, _ in sb.log]
    assert ("auth_delete", _UID) in ops
    # auth deletion comes after the profile row delete (data first).
    assert ops.index(("auth_delete", _UID)) > ops.index(("delete", "user_profiles"))
    assert sb.auth.admin.deleted == [_UID]


def test_no_profile_skips_notifications_but_still_deletes_auth() -> None:
    sb = _FakeSupabase(tables={"documents": [{"user_id": _UID}]}, objects={})
    report = account_deletion.delete_account(sb, user_id=_UID)
    assert "notifications_sent" not in report  # no profile id to resolve
    assert sb.auth.admin.deleted == [_UID]
    assert report["auth_user"] == 1


# ---- storage helper ---------------------------------------------------


def test_purge_user_objects_empty_prefix_returns_zero() -> None:
    from app.services.ingest import storage

    sb = _FakeSupabase(objects={"resume-uploads": {}})
    assert storage.purge_user_objects(sb, _UID) == 0


def test_purge_user_objects_loops_until_empty() -> None:
    from app.services.tailor import persistence

    sb = _FakeSupabase(objects={"tailored-resumes": {_UID: ["a.docx", "b.docx"]}})
    assert persistence.purge_user_objects(sb, _UID) == 2
    assert sb.storage.buckets["tailored-resumes"]._objects["tailored-resumes"][_UID] == []


# ---- route ------------------------------------------------------------


def test_delete_account_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from app.dependencies import (
        get_current_user_id,
        get_supabase,
        verify_supabase_jwt,
    )
    from app.main import app

    sb = _seeded()
    app.dependency_overrides[verify_supabase_jwt] = lambda: _UID
    app.dependency_overrides[get_current_user_id] = lambda: _UID
    app.dependency_overrides[get_supabase] = lambda: sb
    try:
        client = TestClient(app)
        resp = client.delete("/profile/account")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] is True
    assert body["report"]["auth_user"] == 1
    assert body["report"]["documents"] == 2
    assert sb.auth.admin.deleted == [_UID]
