from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.dependencies import (
    get_current_user_id,
    get_supabase,
    verify_api_key_or_jwt,
    verify_supabase_jwt,
)
from app.main import app


class _Resp:
    def __init__(self, data: Any) -> None:
        self.data = data


_TEST_USER_ID = "00000000-0000-0000-0000-000000000001"
_TEST_TARGET_ID = "11111111-1111-1111-1111-111111111111"


def _build_supabase(
    posting_data: dict[str, Any] | None,
    *,
    owns_posting: bool = True,
) -> MagicMock:
    """Mock supabase chain for status + delete routes.

    Status uses ``_assert_user_owns_posting`` which:
      1. ``table("jobs").select("id").eq("id", id).single().execute()``
      2. ``table("user_targets").select("target_id").eq("user_id", uid).execute()``
      3. ``table("scores").select("target_id").eq("job_posting_id", id).in_("target_id", [...]).limit(1).execute()``

    ``update_status`` then reads the prior per-user status from
    ``user_jobs`` (#75 C4: jobs.status was dropped):
      ``table("user_jobs").select("status").eq("user_id", uid).eq("job_posting_id", id).limit(1).execute()``

    The old shape went via ``jobs.target_id`` directly; this version
    routes ownership through the ``scores`` table since the poller
    doesn't populate ``jobs.target_id``.

    Delete uses the jobs router's own ``_assert_user_owns_posting``
    (still on the old shape today — ``jobs.target_id`` + .limit(1)).
    Mock both.
    """
    sb = MagicMock()
    # Status route's existence probe selects only ``id`` (#75 C4: jobs.status
    # gone). The prior per-user status comes from user_jobs (below). Delete
    # route still uses the legacy shape with ``jobs.target_id`` inline.
    prior_status = posting_data.get("status", "new") if posting_data else "new"
    posting_id_only = (
        {"id": "abc"} if posting_data is not None else None
    )
    posting_with_target = (
        {**posting_data, "target_id": _TEST_TARGET_ID, "id": "abc"}
        if posting_data is not None
        else None
    )

    def _table(name: str):
        t = MagicMock()
        if name == "jobs":
            sel = t.select.return_value
            # status.py uses .single().execute() — selects just ``id``
            sel.eq.return_value.single.return_value.execute.return_value = _Resp(
                posting_id_only
            )
            # jobs.py delete/get use .limit(1).execute() — still selects target_id
            sel.eq.return_value.limit.return_value.execute.return_value = _Resp(
                [posting_with_target] if posting_with_target else None
            )
            t.insert.return_value.execute.return_value = _Resp(None)
            t.update.return_value.eq.return_value.execute.return_value = _Resp(None)
            delete_chain = t.delete.return_value.eq.return_value
            delete_chain.execute.return_value = _Resp(
                [posting_with_target] if posting_with_target else None
            )
        elif name == "user_targets":
            # Status route: ``.select("target_id").eq("user_id", uid).execute()``
            t.select.return_value.eq.return_value.execute.return_value = _Resp(
                [{"target_id": _TEST_TARGET_ID}] if owns_posting else []
            )
            # Legacy delete-path chain (kept for jobs.py compatibility).
            t.select.return_value.eq.return_value.eq.return_value.limit.return_value.execute.return_value = _Resp(
                [{"target_id": _TEST_TARGET_ID}]
                if owns_posting and posting_with_target
                else []
            )
        elif name == "scores":
            # Status route: ``.eq("job_posting_id", id).in_("target_id", [...]).limit(1).execute()``
            score_rows = (
                [{"target_id": _TEST_TARGET_ID}]
                if owns_posting and posting_with_target
                else []
            )
            t.select.return_value.eq.return_value.in_.return_value.limit.return_value.execute.return_value = _Resp(
                score_rows
            )
        elif name == "status_log":
            t.insert.return_value.execute.return_value = _Resp(None)
        elif name == "user_jobs":
            # Prior per-user status read (#75 C4):
            # ``.select("status").eq(...).eq(...).limit(1).execute()``.
            t.select.return_value.eq.return_value.eq.return_value.limit.return_value.execute.return_value = _Resp(
                [{"status": prior_status}] if posting_data is not None else []
            )
            # Dual-write target (#75 C1): upsert(...).execute().
            t.upsert.return_value.execute.return_value = _Resp(None)
        return t

    sb.table.side_effect = _table
    return sb


@pytest.fixture
def client_factory():
    def _make(supabase: MagicMock, *, authed: bool = True) -> TestClient:
        app.dependency_overrides[get_supabase] = lambda: supabase
        if authed:
            app.dependency_overrides[verify_api_key_or_jwt] = lambda: "test"
            app.dependency_overrides[verify_supabase_jwt] = lambda: _TEST_USER_ID
            app.dependency_overrides[get_current_user_id] = lambda: _TEST_USER_ID
        c = TestClient(app)
        return c

    yield _make
    app.dependency_overrides.clear()


def test_status_unauth_returns_401():
    # No override for auth dep — should 401 via real dependency.
    client = TestClient(app)
    r = client.post("/jobs/abc/status", json={"status": "new"})
    assert r.status_code == 401


def test_status_404_when_posting_missing(client_factory):
    sb = _build_supabase(posting_data=None)
    client = client_factory(sb)
    r = client.post("/jobs/abc/status", json={"status": "new"})
    assert r.status_code == 404


def test_status_200_on_valid_update(client_factory):
    sb = _build_supabase(posting_data={"status": "saved"})
    client = client_factory(sb)
    r = client.post("/jobs/abc/status", json={"status": "applied", "note": "sent"})
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["old_status"] == "saved"
    assert body["new_status"] == "applied"


def test_status_update_dual_writes_user_jobs_and_status_log_user(client_factory):
    """#75 C3: a status update writes the per-user status into user_jobs and
    stamps the status_log row with user_id; the global jobs.status is no
    longer touched."""
    seen: dict[str, Any] = {}

    def _build() -> MagicMock:
        sb = MagicMock()

        def _table(name: str):
            t = MagicMock()
            if name == "jobs":
                t.select.return_value.eq.return_value.single.return_value.execute.return_value = _Resp(
                    {"id": "abc"}
                )
                t.update.return_value.eq.return_value.execute.return_value = _Resp(
                    None
                )
            elif name == "user_targets":
                t.select.return_value.eq.return_value.execute.return_value = _Resp(
                    [{"target_id": _TEST_TARGET_ID}]
                )
            elif name == "scores":
                t.select.return_value.eq.return_value.in_.return_value.limit.return_value.execute.return_value = _Resp(
                    [{"target_id": _TEST_TARGET_ID}]
                )
            elif name == "status_log":

                def _insert(payload: Any):
                    seen["status_log"] = payload
                    return MagicMock(execute=lambda: _Resp(None))

                t.insert.side_effect = _insert
            elif name == "user_jobs":
                # Prior per-user status read (#75 C4).
                t.select.return_value.eq.return_value.eq.return_value.limit.return_value.execute.return_value = _Resp(
                    [{"status": "saved"}]
                )

                def _upsert(payload: Any, **kwargs: Any):
                    seen["user_jobs"] = payload
                    seen["user_jobs_kwargs"] = kwargs
                    return MagicMock(execute=lambda: _Resp(None))

                t.upsert.side_effect = _upsert
            return t

        sb.table.side_effect = _table
        return sb

    client = client_factory(_build())
    r = client.post("/jobs/abc/status", json={"status": "applied", "note": "x"})
    assert r.status_code == 200

    # status_log carries the user_id now.
    assert seen["status_log"]["user_id"] == _TEST_USER_ID
    # user_jobs mirrors the new status keyed by (user_id, posting).
    assert seen["user_jobs"]["user_id"] == _TEST_USER_ID
    assert seen["user_jobs"]["job_posting_id"] == "abc"
    assert seen["user_jobs"]["status"] == "applied"
    assert seen["user_jobs_kwargs"]["on_conflict"] == "user_id,job_posting_id"


def test_status_history_scopes_to_caller(client_factory):
    """#113: a shared posting can carry other users' transitions, so the
    history query must filter status_log by the caller's user_id, not just the
    posting."""
    captured: dict[str, Any] = {}

    def _build() -> MagicMock:
        sb = MagicMock()

        def _table(name: str):
            t = MagicMock()
            if name == "jobs":
                t.select.return_value.eq.return_value.single.return_value.execute.return_value = _Resp(
                    {"id": "abc"}
                )
            elif name == "user_targets":
                t.select.return_value.eq.return_value.execute.return_value = _Resp(
                    [{"target_id": _TEST_TARGET_ID}]
                )
            elif name == "scores":
                t.select.return_value.eq.return_value.in_.return_value.limit.return_value.execute.return_value = _Resp(
                    [{"target_id": _TEST_TARGET_ID}]
                )
            elif name == "status_log":
                q = MagicMock()

                def _eq(col: str, val: Any):
                    captured[col] = val
                    return q

                q.eq.side_effect = _eq
                q.order.return_value.limit.return_value.execute.return_value = _Resp(
                    [
                        {
                            "id": "l1",
                            "old_status": "saved",
                            "new_status": "applied",
                            "note": None,
                            "created_at": "2026-01-01T00:00:00Z",
                        }
                    ]
                )
                t.select.return_value = q
            return t

        sb.table.side_effect = _table
        return sb

    client = client_factory(_build())
    r = client.get("/jobs/abc/status-history")
    assert r.status_code == 200
    assert r.json()["entries"][0]["new_status"] == "applied"
    # The query was scoped to BOTH the posting and the caller.
    assert captured["posting_id"] == "abc"
    assert captured["user_id"] == _TEST_USER_ID


def test_status_update_only_evicts_owning_target_and_global_views(client_factory):
    """Sibling targets' cached pages must survive a status mutation."""
    from app.cache import job_list_cache, jobs_cache_prefix, make_cache_key

    sibling_target = "22222222-2222-2222-2222-222222222222"
    owning_key = make_cache_key(
        jobs_cache_prefix(target_id=_TEST_TARGET_ID), page=1
    )
    sibling_key = make_cache_key(
        jobs_cache_prefix(target_id=sibling_target), page=1
    )
    global_key = make_cache_key(jobs_cache_prefix(target_id=None), page=1)

    job_list_cache.set(owning_key, {"v": "owning"})
    job_list_cache.set(sibling_key, {"v": "sibling"})
    job_list_cache.set(global_key, {"v": "global"})

    sb = _build_supabase(posting_data={"status": "saved"})
    client = client_factory(sb)
    r = client.post("/jobs/abc/status", json={"status": "applied"})
    assert r.status_code == 200

    assert job_list_cache.get(owning_key) is None
    assert job_list_cache.get(global_key) is None
    assert job_list_cache.get(sibling_key) == {"v": "sibling"}


def test_status_422_on_invalid_status(client_factory):
    sb = _build_supabase(posting_data={"status": "new"})
    client = client_factory(sb)
    r = client.post("/jobs/abc/status", json={"status": "bogus"})
    assert r.status_code == 422


# --- DELETE /jobs/{posting_id} ---


def test_delete_unauth_returns_401():
    client = TestClient(app)
    r = client.delete("/jobs/abc")
    assert r.status_code == 401


def test_delete_404_when_posting_missing(client_factory):
    sb = _build_supabase(posting_data=None)
    client = client_factory(sb)
    r = client.delete("/jobs/abc")
    assert r.status_code == 404


def test_delete_200_on_valid_delete(client_factory):
    sb = _build_supabase(posting_data={"id": "abc"})
    client = client_factory(sb)
    r = client.delete("/jobs/abc")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["deleted_id"] == "abc"
