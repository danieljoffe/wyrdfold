"""Every ACTIVE membership write goes through ``activate_user_target`` (#1071 review).

In plain terms: the app used to count a user's active targets and then write
the new one as two separate steps, on every screen that can switch a target
on. Two requests at once could both pass the count. Now every such write
calls one database function that counts and writes under one per-user lock;
these tests pin that the router's helpers reach that function with the right
arguments, map its rejection onto the existing 409 contract, and leave the
inactive-link write alone.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from postgrest.exceptions import APIError

from app.routers import targets as router
from app.services.targets import crud


def _row(**overrides: Any) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    base: dict[str, Any] = {
        "id": "ut-1",
        "user_id": "u-1",
        "target_id": "t-1",
        "is_active": True,
        "fit_score": None,
        "fit_score_reasoning": None,
        "fit_score_prose_doc_id": None,
        "created_at": now,
        "updated_at": now,
    }
    base.update(overrides)
    return base


def _supabase(rpc_result: Any = None, rpc_error: BaseException | None = None) -> MagicMock:
    supabase = MagicMock()
    execute = AsyncMock(return_value=SimpleNamespace(data=rpc_result))
    if rpc_error is not None:
        execute = AsyncMock(side_effect=rpc_error)
    supabase.rpc.return_value.execute = execute
    supabase.table.return_value.upsert.return_value.execute = AsyncMock(
        return_value=SimpleNamespace(data=[_row(is_active=False)])
    )
    supabase.table.return_value.update.return_value.eq.return_value.eq.return_value.execute = (
        AsyncMock(return_value=SimpleNamespace(data=[_row(is_active=False)]))
    )
    return supabase


def _cap_error(active_count: int, limit: int) -> APIError:
    return APIError(
        {
            "message": f"active target limit reached ({active_count} of {limit})",
            "code": "PT409",
            "details": json.dumps(
                {"error": "ACTIVE_LIMIT", "active_count": active_count, "limit": limit}
            ),
            "hint": "Deactivate a target first.",
        }
    )


@pytest.fixture
def cap(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {"resolved_for": []}

    async def resolve(supabase: Any, user_id: str) -> int:
        seen["resolved_for"].append(user_id)
        return 2

    monkeypatch.setattr(router, "_effective_active_target_cap_async", resolve)
    return seen


@pytest.mark.asyncio
async def test_active_link_calls_the_activation_function_with_the_resolved_cap(
    cap: dict[str, Any],
) -> None:
    supabase = _supabase(rpc_result=_row())

    link = await router._link_user_to_target_async(
        supabase, user_id="u-1", target_id="t-1", is_active=True
    )

    name, params = supabase.rpc.call_args.args
    assert name == "activate_user_target"
    assert params == {"p_user_id": "u-1", "p_target_id": "t-1", "p_active_limit": 2}
    assert cap["resolved_for"] == ["u-1"]
    assert link.is_active is True
    supabase.table.return_value.upsert.assert_not_called()


@pytest.mark.asyncio
async def test_fit_score_fields_are_passed_only_when_supplied(cap: dict[str, Any]) -> None:
    supabase = _supabase(rpc_result=_row(fit_score=71))

    await router._link_user_to_target_async(
        supabase,
        user_id="u-1",
        target_id="t-1",
        is_active=True,
        fit_score=71,
        fit_score_reasoning="strong overlap",
    )

    _, params = supabase.rpc.call_args.args
    assert params["p_fit_score"] == 71
    assert params["p_fit_score_reasoning"] == "strong overlap"
    assert "p_fit_score_prose_doc_id" not in params


@pytest.mark.asyncio
async def test_inactive_link_is_still_a_plain_upsert(cap: dict[str, Any]) -> None:
    supabase = _supabase()

    link = await router._link_user_to_target_async(
        supabase, user_id="u-1", target_id="t-1", is_active=False
    )

    supabase.rpc.assert_not_called()
    payload = supabase.table.return_value.upsert.call_args.args[0]
    assert payload["is_active"] is False
    assert cap["resolved_for"] == []
    assert link.is_active is False


@pytest.mark.asyncio
async def test_cap_rejection_becomes_the_existing_409_error_with_the_counts(
    cap: dict[str, Any],
) -> None:
    supabase = _supabase(rpc_error=_cap_error(active_count=2, limit=2))

    with pytest.raises(crud.ActiveTargetLimitError) as exc:
        await router._link_user_to_target_async(
            supabase, user_id="u-1", target_id="t-1", is_active=True
        )

    assert exc.value.current_count == 2
    assert exc.value.limit == 2


@pytest.mark.asyncio
async def test_other_database_errors_propagate_unchanged(cap: dict[str, Any]) -> None:
    boom = APIError({"message": "relation missing", "code": "42P01", "details": "", "hint": ""})
    supabase = _supabase(rpc_error=boom)

    with pytest.raises(APIError) as exc:
        await router._link_user_to_target_async(
            supabase, user_id="u-1", target_id="t-1", is_active=True
        )

    assert exc.value is boom


@pytest.mark.asyncio
async def test_swap_calls_the_swap_function_with_both_ids_and_the_cap(cap: dict[str, Any]) -> None:
    supabase = _supabase(rpc_result=_row())

    await router._activate_user_target_async(
        supabase, user_id="u-1", target_id="t-new", swap_out="t-old"
    )

    name, params = supabase.rpc.call_args.args
    assert name == "swap_user_target_active"
    assert params == {
        "p_user_id": "u-1",
        "p_target_id": "t-new",
        "p_active_limit": 2,
        "p_swap_out": "t-old",
    }


@pytest.mark.asyncio
async def test_swap_rejected_by_the_function_maps_onto_the_400_messages(
    cap: dict[str, Any],
) -> None:
    """The function re-checks the swap under the lock; a swap-out that a
    concurrent request just deactivated is refused there, with the same
    message the router's own precheck uses."""
    from fastapi import HTTPException

    not_active = APIError(
        {
            "message": "swap-out target is not active for this user",
            "code": "PT400",
            "details": json.dumps({"error": "SWAP_NOT_ACTIVE"}),
            "hint": "",
        }
    )
    supabase = _supabase(rpc_error=not_active)

    with pytest.raises(HTTPException) as exc:
        await router._activate_user_target_async(
            supabase, user_id="u-1", target_id="t-new", swap_out="t-old"
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == "That target is not currently active."


@pytest.mark.asyncio
async def test_the_cap_is_always_resolved_and_passed(cap: dict[str, Any]) -> None:
    """No uncapped activation exists in the router any more: every call
    carries the resolved limit (the function refuses a NULL one)."""
    supabase = _supabase(rpc_result=_row())

    await router._activate_user_target_async(supabase, user_id="u-1", target_id="t-1")

    _, params = supabase.rpc.call_args.args
    assert params["p_active_limit"] == 2
    assert cap["resolved_for"] == ["u-1"]


@pytest.mark.asyncio
async def test_an_empty_function_result_is_a_loud_failure(cap: dict[str, Any]) -> None:
    supabase = _supabase(rpc_result=None)

    with pytest.raises(RuntimeError, match="activate_user_target returned no row"):
        await router._link_user_to_target_async(
            supabase, user_id="u-1", target_id="t-1", is_active=True
        )
