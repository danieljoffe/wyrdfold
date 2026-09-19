"""Tests for shared-targets junction CRUD and fit-score model bounds (#553).

Covers:
  1. set_user_target_inactive deactivates via user_targets (so the trigger
     fed the old targets.is_active trigger; the flag is now the derived pipeline predicate).
  2. FitScoreResult tolerates reasoning strings up to 1500 chars (the LLM
     occasionally exceeds the original 500 cap, which caused 502s).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.services.targets import crud
from app.services.targets.fit_score import FitScoreResult


def _user_target_row(
    *,
    user_id: str = "user-1",
    target_id: str = "target-1",
    is_active: bool = True,
    fit_score: int | None = None,
    fit_score_reasoning: str | None = None,
) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    return {
        "id": "ut-1",
        "user_id": user_id,
        "target_id": target_id,
        "is_active": is_active,
        "fit_score": fit_score,
        "fit_score_reasoning": fit_score_reasoning,
        "created_at": now,
        "updated_at": now,
    }


# ---------------------------------------------------------------------------
# (1) Activate / deactivate route through user_targets
# ---------------------------------------------------------------------------


def test_link_user_to_target_writes_is_active_true() -> None:
    supabase = MagicMock()
    supabase.table.return_value.upsert.return_value.execute.return_value.data = [
        _user_target_row(is_active=True)
    ]

    result = crud.link_user_to_target(
        supabase, user_id="user-1", target_id="target-1", is_active=True
    )

    payload = supabase.table.return_value.upsert.call_args.args[0]
    assert payload["is_active"] is True
    assert payload["user_id"] == "user-1"
    assert payload["target_id"] == "target-1"
    assert result.is_active is True


def test_set_user_target_inactive_updates_user_targets_table() -> None:
    supabase = MagicMock()
    update_chain = (
        supabase.table.return_value.update.return_value.eq.return_value.eq.return_value.execute
    )
    update_chain.return_value.data = [_user_target_row(is_active=False)]

    result = crud.set_user_target_inactive(supabase, user_id="user-1", target_id="target-1")

    supabase.table.assert_called_with("user_targets")
    update_args = supabase.table.return_value.update.call_args.args[0]
    assert update_args["is_active"] is False
    assert "updated_at" in update_args
    assert result is not None
    assert result.is_active is False


def test_set_user_target_inactive_returns_none_when_no_row() -> None:
    supabase = MagicMock()
    update_chain = (
        supabase.table.return_value.update.return_value.eq.return_value.eq.return_value.execute
    )
    update_chain.return_value.data = []

    result = crud.set_user_target_inactive(supabase, user_id="user-1", target_id="missing")

    assert result is None


# ---------------------------------------------------------------------------
# (2) FitScoreResult tolerates long reasoning
# ---------------------------------------------------------------------------


def test_fit_score_result_accepts_reasoning_up_to_1500_chars() -> None:
    long_reasoning = "x" * 1500
    result = FitScoreResult(fit_score=82, reasoning=long_reasoning)
    assert len(result.reasoning) == 1500


def test_fit_score_result_rejects_reasoning_over_1500_chars() -> None:
    with pytest.raises(ValueError):
        FitScoreResult(fit_score=82, reasoning="x" * 1501)


def test_fit_score_result_enforces_score_bounds() -> None:
    with pytest.raises(ValueError):
        FitScoreResult(fit_score=101, reasoning="ok")
    with pytest.raises(ValueError):
        FitScoreResult(fit_score=-1, reasoning="ok")


# ---------------------------------------------------------------------------
# (3) Active-target limit
# ---------------------------------------------------------------------------


def _mock_supabase_for_link(
    *,
    existing_row: dict[str, Any] | None,
    active_count: int,
    max_active_override: int | None = None,
) -> MagicMock:
    """Build a Supabase mock that returns deterministic answers for the
    three reads link_user_to_target performs before its upsert: (1) "is
    this (user, target) pair already linked, and was it active?",
    (2) "how many active links does this user already have?", and
    (3) the per-user ``max_active_targets`` override on user_profiles.
    """
    supabase = MagicMock()
    table = supabase.table.return_value

    # Existing-row check: select().eq().eq().limit().execute()
    existing_chain = (
        table.select.return_value.eq.return_value.eq.return_value.limit.return_value.execute
    )
    existing_chain.return_value.data = [existing_row] if existing_row else []

    # Count check: select().eq().eq().limit().execute() — same chain in
    # MagicMock, so we shim ``.count`` on the same return value.
    existing_chain.return_value.count = active_count

    # Override read: select().eq().execute() — one .eq() shorter, so it's
    # a distinct chain on the mock.
    override_chain = table.select.return_value.eq.return_value.execute
    override_chain.return_value.data = (
        [{"max_active_targets": max_active_override}] if max_active_override is not None else []
    )

    # Upsert: returns a row that matches what we wrote.
    table.upsert.return_value.execute.return_value.data = [_user_target_row(is_active=True)]
    return supabase


def test_link_user_to_target_allows_when_under_limit() -> None:
    # Cap is 1 (cost-caps work) — "under limit" means zero active links.
    supabase = _mock_supabase_for_link(existing_row=None, active_count=0)

    result = crud.link_user_to_target(
        supabase, user_id="user-1", target_id="target-1", is_active=True
    )

    assert result.is_active is True


def test_link_user_to_target_honors_max_active_override() -> None:
    """A per-user ``max_active_targets`` override (the operator's "add
    credits" lever) raises the cap above the global default of 1."""
    supabase = _mock_supabase_for_link(existing_row=None, active_count=2, max_active_override=3)

    result = crud.link_user_to_target(
        supabase, user_id="user-1", target_id="target-1", is_active=True
    )

    assert result.is_active is True


def test_link_user_to_target_raises_when_at_limit_and_new_target() -> None:
    """At the cap, activating a NEW target raises."""
    supabase = _mock_supabase_for_link(
        existing_row=None,
        active_count=crud.MAX_ACTIVE_TARGETS_PER_USER,
    )

    with pytest.raises(crud.ActiveTargetLimitError) as ex:
        crud.link_user_to_target(supabase, user_id="user-1", target_id="new-target", is_active=True)
    assert ex.value.current_count == crud.MAX_ACTIVE_TARGETS_PER_USER
    assert ex.value.limit == crud.MAX_ACTIVE_TARGETS_PER_USER
    # And critically: no upsert fired.
    supabase.table.return_value.upsert.assert_not_called()


def test_link_user_to_target_allows_reupsert_of_already_active_row() -> None:
    """At the cap, re-upserting an ALREADY-ACTIVE row is fine — no net
    change. Lets callers refresh ``fit_score`` on the row without
    tripping the limit.
    """
    supabase = _mock_supabase_for_link(
        existing_row=_user_target_row(is_active=True),
        active_count=crud.MAX_ACTIVE_TARGETS_PER_USER,
    )

    # Doesn't raise.
    result = crud.link_user_to_target(
        supabase,
        user_id="user-1",
        target_id="target-1",
        is_active=True,
        fit_score=85,
    )
    assert result is not None
    # Upsert fired as expected.
    supabase.table.return_value.upsert.assert_called_once()


def test_link_user_to_target_with_enforce_active_limit_false_bypasses_cap() -> None:
    """Internal callers (future backfill scripts) can opt out of the
    cap. Defaults to ``True`` so the path remains safe by default.
    """
    supabase = _mock_supabase_for_link(
        existing_row=None,
        active_count=crud.MAX_ACTIVE_TARGETS_PER_USER + 10,
    )

    result = crud.link_user_to_target(
        supabase,
        user_id="user-1",
        target_id="target-1",
        is_active=True,
        enforce_active_limit=False,
    )
    assert result is not None


def test_link_user_to_target_skips_count_when_is_active_false() -> None:
    """Deactivation never trips the cap — we're removing an active
    target, not adding one.
    """
    supabase = MagicMock()
    supabase.table.return_value.upsert.return_value.execute.return_value.data = [
        _user_target_row(is_active=False)
    ]

    result = crud.link_user_to_target(
        supabase, user_id="user-1", target_id="target-1", is_active=False
    )

    assert result.is_active is False
    # The "existing row" / "count" reads never happened — only the
    # upsert. (Verified by checking that .select() wasn't called.)
    supabase.table.return_value.select.assert_not_called()


def test_count_active_for_user_uses_exact_count_head() -> None:
    """``count_active_for_user`` only needs a row count, not the rows
    themselves — verify it asks Supabase for ``count='exact'`` with a
    ``limit(1)`` so we don't ship a payload we don't use.
    """
    supabase = MagicMock()
    chain = supabase.table.return_value.select.return_value.eq.return_value.eq.return_value.limit.return_value.execute
    chain.return_value.count = 3

    n = crud.count_active_for_user(supabase, "user-1")

    assert n == 3
    select_args = supabase.table.return_value.select.call_args
    # First positional arg or 'count' kwarg should signal exact-count semantics.
    assert select_args.kwargs.get("count") == "exact"


def test_get_active_target_ids_filters_on_membership_activity() -> None:
    """The /jobs list scope (Group D decision 2026-07-30): ACTIVE memberships
    only — the query must carry BOTH the user filter and is_active=True, so a
    paused link's jobs leave the list while every any-status surface
    (authz/dedup via get_user_target_ids) still sees the link."""
    supabase = MagicMock()
    chain = supabase.table.return_value.select.return_value
    chain.eq.return_value.eq.return_value.execute.return_value.data = [{"target_id": "t-active"}]

    ids = crud.get_active_target_ids(supabase, "user-1")

    assert ids == {"t-active"}
    supabase.table.assert_called_with("user_targets")
    first_eq = chain.eq.call_args_list[0].args
    second_eq = chain.eq.return_value.eq.call_args_list[0].args
    assert first_eq == ("user_id", "user-1")
    assert second_eq == ("is_active", True)


class TestActiveLimitErrorPayload:
    """The 409 body for the active-target cap.

    ``message`` is the string the user actually reads — the frontend surfaces
    it verbatim rather than composing its own — so its wording is a contract,
    not an implementation detail. Three routes (activate, link/follow,
    add-from-posting) used to carry byte-identical copies of this dict; they
    now share one builder.
    """

    def test_shape_carries_error_limit_count_and_message(self) -> None:
        from app.routers.targets import _active_limit_error
        from app.services.targets import crud

        exc = _active_limit_error(crud.ActiveTargetLimitError(current_count=3, limit=3))

        assert exc.status_code == 409
        assert exc.detail["error"] == "ACTIVE_LIMIT"
        assert exc.detail["limit"] == 3
        assert exc.detail["active_count"] == 3
        assert exc.detail["message"] == (
            "You already have 3 active targets (limit 3) — deactivate one first."
        )

    def test_singularizes_at_a_count_of_one(self) -> None:
        """The default cap is 1, so "1 active targets" was the common case."""
        from app.routers.targets import _active_limit_error
        from app.services.targets import crud

        exc = _active_limit_error(crud.ActiveTargetLimitError(current_count=1, limit=1))

        assert exc.detail["message"] == (
            "You already have 1 active target (limit 1) — deactivate one first."
        )
        assert "1 active targets" not in exc.detail["message"]

    def test_message_is_non_empty_so_the_frontend_never_falls_back(self) -> None:
        from app.routers.targets import _active_limit_error
        from app.services.targets import crud

        for count, limit in ((0, 1), (1, 1), (2, 5), (10, 10)):
            exc = _active_limit_error(crud.ActiveTargetLimitError(current_count=count, limit=limit))
            assert isinstance(exc.detail["message"], str)
            assert exc.detail["message"].strip()
            assert "deactivate one first" in exc.detail["message"]


class TestActiveLimitPickerPayload:
    """The 409 names WHICH targets hold the cap.

    Without it the client can only say "deactivate one first" and leave the
    user to find them. `/targets` could work them out from its own state, but
    the detail page knows only the target it is showing — so the server sends
    the choices and every surface can offer the same swap.
    """

    def test_active_targets_default_to_empty_not_missing(self) -> None:
        """The key must always exist: a client reading `detail.active_targets`
        should never have to distinguish absent from empty."""
        from app.routers.targets import _active_limit_error
        from app.services.targets import crud

        exc = _active_limit_error(crud.ActiveTargetLimitError(current_count=1, limit=1))
        assert exc.detail["active_targets"] == []

    def test_active_targets_are_carried_through(self) -> None:
        from app.routers.targets import _active_limit_error
        from app.services.targets import crud

        choices = [
            {"id": "t-1", "label": "Senior Frontend Engineer"},
            {"id": "t-2", "label": "Staff Full-Stack Engineer"},
        ]
        exc = _active_limit_error(crud.ActiveTargetLimitError(current_count=2, limit=2), choices)
        assert exc.detail["active_targets"] == choices
        # ...and the message still stands on its own for a client that
        # ignores the list entirely.
        assert "deactivate one first" in exc.detail["message"]

    def test_scales_past_a_cap_of_one(self) -> None:
        """Free is 1, starter 2, pro 5 — the picker is a list at every tier,
        so nothing may assume a single swap candidate."""
        from app.routers.targets import _active_limit_error
        from app.services.targets import crud

        choices = [{"id": f"t-{i}", "label": f"Target {i}"} for i in range(5)]
        exc = _active_limit_error(crud.ActiveTargetLimitError(current_count=5, limit=5), choices)
        assert len(exc.detail["active_targets"]) == 5
        assert exc.detail["message"] == (
            "You already have 5 active targets (limit 5) — deactivate one first."
        )


class TestActivateWithSwap:
    """``_activate_link_with_optional_swap``: one database transaction (#1080 review).

    The swap used to be three writes (deactivate, activate, restore on
    failure) and the restore ran uncapped, so a concurrent activation between
    the first two could leave the user over the cap. Now the helper makes ONE
    call to the activation path with ``swap_out`` set; the database function
    deactivates, counts and activates under one lock, and a rejection unwinds
    the deactivation with it. These tests pin that there is exactly one write
    call and no restore logic left in the router.
    """

    @staticmethod
    def _choices(*ids: str) -> AsyncMock:
        return AsyncMock(return_value=[{"id": i, "label": i.title()} for i in ids])

    @pytest.mark.asyncio
    async def test_swap_is_one_call_with_both_ids(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.routers import targets as mod

        monkeypatch.setattr(mod, "_active_target_choices", self._choices("old"))
        activate = AsyncMock()
        monkeypatch.setattr(mod, "_activate_user_target_async", activate)

        await mod._activate_link_with_optional_swap(
            MagicMock(), user_id="u1", target_id="new", swap_out="old"
        )

        activate.assert_awaited_once()
        assert activate.await_args.kwargs["target_id"] == "new"
        assert activate.await_args.kwargs["swap_out"] == "old"

    @pytest.mark.asyncio
    async def test_plain_activation_passes_no_swap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.routers import targets as mod

        activate = AsyncMock()
        monkeypatch.setattr(mod, "_activate_user_target_async", activate)

        await mod._activate_link_with_optional_swap(
            MagicMock(), user_id="u1", target_id="new", swap_out=None
        )

        assert activate.await_args.kwargs["swap_out"] is None

    @pytest.mark.asyncio
    async def test_cap_rejection_is_a_409_with_the_swap_picker_and_no_restore(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The database unwound the deactivation; the router has nothing to
        put back, so the only write is the one rejected call."""
        from app.routers import targets as mod
        from app.services.targets import crud

        monkeypatch.setattr(mod, "_active_target_choices", self._choices("old"))
        activate = AsyncMock(side_effect=crud.ActiveTargetLimitError(current_count=1, limit=1))
        monkeypatch.setattr(mod, "_activate_user_target_async", activate)

        with pytest.raises(HTTPException) as caught:
            await mod._activate_link_with_optional_swap(
                MagicMock(), user_id="u1", target_id="new", swap_out="old"
            )

        assert caught.value.status_code == 409
        assert caught.value.detail["active_targets"] == [{"id": "old", "label": "Old"}]
        activate.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_swapping_out_a_target_that_is_not_active_is_a_400_before_any_write(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.routers import targets as mod

        monkeypatch.setattr(mod, "_active_target_choices", self._choices("other"))
        activate = AsyncMock()
        monkeypatch.setattr(mod, "_activate_user_target_async", activate)

        with pytest.raises(HTTPException) as caught:
            await mod._activate_link_with_optional_swap(
                MagicMock(), user_id="u1", target_id="new", swap_out="old"
            )

        assert caught.value.status_code == 400
        assert caught.value.detail == "That target is not currently active."
        activate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_swapping_a_target_for_itself_is_a_400_before_any_write(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.routers import targets as mod

        activate = AsyncMock()
        monkeypatch.setattr(mod, "_activate_user_target_async", activate)

        with pytest.raises(HTTPException) as caught:
            await mod._activate_link_with_optional_swap(
                MagicMock(), user_id="u1", target_id="same", swap_out="same"
            )

        assert caught.value.status_code == 400
        activate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unexpected_errors_propagate_with_no_restore_attempt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.routers import targets as mod

        monkeypatch.setattr(mod, "_active_target_choices", self._choices("old"))
        activate = AsyncMock(side_effect=RuntimeError("db blip"))
        monkeypatch.setattr(mod, "_activate_user_target_async", activate)

        with pytest.raises(RuntimeError, match="db blip"):
            await mod._activate_link_with_optional_swap(
                MagicMock(), user_id="u1", target_id="new", swap_out="old"
            )

        activate.assert_awaited_once()


async def test_link_route_refuses_at_cap_without_deriving_a_fit_score() -> None:
    """`link_target` must not pay for work the cap is about to reject.

    The fit score is an LLM call and is charged to the user via
    `cost_log.record_async`. It used to run BEFORE the write that raises on
    the active-target cap, so a refused link still cost the user quota — and
    it fires exactly when someone is trying to add MORE targets, i.e. when
    they are most engaged. Same shape as #845, on the interactive side.
    """
    from app.routers import targets as targets_router

    called: dict[str, bool] = {"derived": False, "charged": False}

    async def _never_derive(*_a: object, **_k: object) -> object:
        called["derived"] = True
        raise AssertionError("fit score derived despite the cap being full")

    async def _never_charge(*_a: object, **_k: object) -> None:
        called["charged"] = True
        raise AssertionError("user charged despite the cap being full")

    async def _at_cap(*_a: object, **_k: object) -> None:
        raise crud.ActiveTargetLimitError(current_count=2, limit=2)

    async def _target(*_a: object, **_k: object) -> object:
        return MagicMock(id="t-new")

    async def _choices(*_a: object, **_k: object) -> list[dict[str, str]]:
        return [{"id": "t-old", "label": "Staff Frontend Engineer"}]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(targets_router, "_target_get", _target)
        mp.setattr(targets_router, "_raise_if_active_limit_async", _at_cap)
        mp.setattr(targets_router, "_active_target_choices", _choices)
        mp.setattr(targets_router, "resolve_current_payload", _never_derive)
        mp.setattr(targets_router, "derive_fit_score", _never_derive)
        mp.setattr(targets_router.cost_log, "record_async", _never_charge)

        with pytest.raises(HTTPException) as exc:
            await targets_router.link_target(
                request=MagicMock(),
                target_id="t-new",
                supabase=MagicMock(),
                llm=MagicMock(),
                user_id="user-1",
            )

    assert exc.value.status_code == 409
    assert exc.value.detail["error"] == "ACTIVE_LIMIT"
    # The point of the test: neither happened.
    assert called["derived"] is False, "no LLM call may precede the cap check"
    assert called["charged"] is False, "the user must not be billed for a refused link"
    # The 409 still carries what the client needs to offer a swap.
    assert exc.value.detail["active_targets"] == [
        {"id": "t-old", "label": "Staff Frontend Engineer"}
    ]
