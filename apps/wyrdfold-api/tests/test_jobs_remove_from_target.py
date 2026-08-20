"""``POST/DELETE /jobs/{id}/remove`` — remove a posting from a target.

Replaces the old "Delete" button, which soft-archived the caller's
``user_jobs`` row, told the user it "can't be undone", and left the row in the
list anyway (both list RPCs applied no archived exclusion when no status filter
was set).

The invariants that matter here:

* removal is per-(user, target, job) — removing from one target must not touch
  the others, and must not touch anyone else's list
* a target id from the request body is never trusted; it must be one the caller
  owns AND one that currently holds the posting
* undo exists (the old flow had none)
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.models.schemas import RemoveJobRequest
from app.routers.jobs import (
    _removed_pairs,
    remove_job_from_target,
    undo_remove_job_from_target,
)

pytestmark = pytest.mark.asyncio

_USER = "u-1"
_JOB = "job-1"
_T1 = "t-1"
_T2 = "t-2"


class _Resp:
    def __init__(self, data: Any) -> None:
        self.data = data


def _supabase(
    *,
    owned_targets: list[str],
    scored_targets: list[str],
    posting_exists: bool = True,
) -> tuple[MagicMock, dict[str, Any]]:
    """Mock the RLS client. Returns (client, captured) where ``captured``
    records the removal writes so tests can assert exactly what was written."""
    captured: dict[str, Any] = {"upserted": None, "deleted_filters": {}}

    def _table(name: str) -> MagicMock:
        t = MagicMock()
        if name == "jobs":
            t.select.return_value.eq.return_value.limit.return_value.execute = AsyncMock(
                return_value=_Resp([{"id": _JOB}] if posting_exists else [])
            )
        elif name == "user_targets":
            t.select.return_value.eq.return_value.execute = AsyncMock(
                return_value=_Resp([{"target_id": t_id} for t_id in owned_targets])
            )
        elif name == "scores":
            # ``_assert_user_owns_posting_async`` (ordered+limited) and
            # ``_targets_holding_posting`` (plain) read the same table.
            rows = [
                {"target_id": t_id, "score": 80, "score_breakdown": {}} for t_id in scored_targets
            ]
            t.select.return_value.eq.return_value.in_.return_value.order.return_value.limit.return_value.execute = AsyncMock(
                return_value=_Resp(rows)
            )
            t.select.return_value.eq.return_value.in_.return_value.execute = AsyncMock(
                return_value=_Resp(rows)
            )
        elif name == "user_target_job_removals":

            async def _upsert_exec() -> _Resp:
                return _Resp([])

            def _upsert(rows: Any, **_kw: Any) -> MagicMock:
                captured["upserted"] = rows
                m = MagicMock()
                m.execute = _upsert_exec
                return m

            t.upsert.side_effect = _upsert

            delete_chain = MagicMock()

            def _eq(col: str, val: Any) -> MagicMock:
                captured["deleted_filters"][col] = val
                return delete_chain

            delete_chain.eq.side_effect = _eq
            delete_chain.execute = AsyncMock(
                return_value=_Resp([{"target_id": _T1}, {"target_id": _T2}])
            )
            t.delete.return_value = delete_chain
        return t

    sb = MagicMock()
    sb.table.side_effect = _table
    return sb, captured


class TestRemove:
    async def test_removes_from_the_named_target_only(self) -> None:
        sb, captured = _supabase(owned_targets=[_T1, _T2], scored_targets=[_T1, _T2])

        result = await remove_job_from_target(
            _JOB, RemoveJobRequest(target_id=_T1), user_id=_USER, supabase=sb
        )

        assert result["removed_from"] == [_T1]
        assert captured["upserted"] == [
            {"user_id": _USER, "target_id": _T1, "job_posting_id": _JOB}
        ]

    async def test_no_target_removes_from_every_holding_target(self) -> None:
        """The All Jobs tab has no single target in scope."""
        sb, captured = _supabase(owned_targets=[_T1, _T2], scored_targets=[_T1, _T2])

        result = await remove_job_from_target(_JOB, RemoveJobRequest(), user_id=_USER, supabase=sb)

        assert result["removed_from"] == [_T1, _T2]
        assert {r["target_id"] for r in captured["upserted"]} == {_T1, _T2}

    async def test_rejects_a_target_the_caller_does_not_own(self) -> None:
        """A target id from the body is never trusted on its own — otherwise a
        caller could probe which targets hold a posting."""
        sb, captured = _supabase(owned_targets=[_T1], scored_targets=[_T1])

        with pytest.raises(HTTPException) as exc:
            await remove_job_from_target(
                _JOB, RemoveJobRequest(target_id="t-someone-else"), user_id=_USER, supabase=sb
            )

        assert exc.value.status_code == 404
        assert captured["upserted"] is None

    async def test_rejects_a_target_that_does_not_hold_the_posting(self) -> None:
        sb, captured = _supabase(owned_targets=[_T1, _T2], scored_targets=[_T1])

        with pytest.raises(HTTPException) as exc:
            await remove_job_from_target(
                _JOB, RemoveJobRequest(target_id=_T2), user_id=_USER, supabase=sb
            )

        assert exc.value.status_code == 404
        assert captured["upserted"] is None

    async def test_no_write_when_nothing_holds_the_posting(self) -> None:
        sb, captured = _supabase(owned_targets=[_T1], scored_targets=[])

        # Ownership probe fails first — a posting none of your targets scored
        # is not yours to see, let alone remove.
        with pytest.raises(HTTPException):
            await remove_job_from_target(_JOB, RemoveJobRequest(), user_id=_USER, supabase=sb)
        assert captured["upserted"] is None


class TestUndo:
    async def test_undo_scopes_the_delete_to_the_caller(self) -> None:
        sb, captured = _supabase(owned_targets=[_T1], scored_targets=[_T1])

        result = await undo_remove_job_from_target(_JOB, target_id=None, user_id=_USER, supabase=sb)

        assert result["success"] is True
        assert result["restored_to"] == [_T1, _T2]
        # The user filter is the security boundary alongside RLS.
        assert captured["deleted_filters"]["user_id"] == _USER
        assert captured["deleted_filters"]["job_posting_id"] == _JOB
        assert "target_id" not in captured["deleted_filters"]

    async def test_undo_can_scope_to_one_target(self) -> None:
        sb, captured = _supabase(owned_targets=[_T1], scored_targets=[_T1])

        await undo_remove_job_from_target(_JOB, target_id=_T1, user_id=_USER, supabase=sb)

        assert captured["deleted_filters"]["target_id"] == _T1


class TestRemovedPairs:
    async def test_pairs_keep_target_granularity(self) -> None:
        """Flattening to job ids here would remove the job from every target."""
        sb = MagicMock()
        t = MagicMock()
        t.select.return_value.eq.return_value.in_.return_value.execute = AsyncMock(
            return_value=_Resp([{"target_id": _T1, "job_posting_id": _JOB}])
        )
        sb.table.return_value = t

        pairs = await _removed_pairs(sb, user_id=_USER, target_ids=[_T1, _T2])

        assert pairs == {(_T1, _JOB)}
        assert (_T2, _JOB) not in pairs

    async def test_anonymous_callers_have_no_removals(self) -> None:
        sb = MagicMock()
        assert await _removed_pairs(sb, user_id=None, target_ids=[_T1]) == set()
        sb.table.assert_not_called()

    async def test_no_targets_short_circuits(self) -> None:
        sb = MagicMock()
        assert await _removed_pairs(sb, user_id=_USER, target_ids=[]) == set()
        sb.table.assert_not_called()
