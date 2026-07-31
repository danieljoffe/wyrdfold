"""Unit coverage for the derived pipeline-active predicate (P0 re-semantics).

The DB-side invariants (trigger absence, floor survival) live in
``tests/integration/test_app_active_semantics.py``; these pin the Python
query shape: ``get_active`` unions the floor arm with the membership arm and
dedupes, ``is_pipeline_active`` short-circuits on the floor.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from app.services.targets import crud


def _target_row(target_id: str, label: str, *, app_active: bool) -> dict[str, Any]:
    return {
        "id": target_id,
        "label": label,
        "normalized_label": label.lower(),
        "scoring_profile": {},
        "app_active": app_active,
        "created_at": "2026-07-31T00:00:00+00:00",
        "updated_at": "2026-07-31T00:00:00+00:00",
    }


def _client_for_get_active(
    floor_rows: list[dict[str, Any]],
    member_target_ids: list[str],
    member_rows: list[dict[str, Any]],
) -> MagicMock:
    """A supabase mock whose ``table()`` dispatches per table name."""
    supabase = MagicMock()

    targets_table = MagicMock()
    # Arm 1: .select("*").eq("app_active", True).execute()
    targets_table.select.return_value.eq.return_value.execute.return_value.data = floor_rows
    # Arm 2 fetch: .select("*").in_("id", missing).execute()
    targets_table.select.return_value.in_.return_value.execute.return_value.data = member_rows

    ut_table = MagicMock()
    ut_table.select.return_value.eq.return_value.execute.return_value.data = [
        {"target_id": tid} for tid in member_target_ids
    ]

    def _table(name: str) -> MagicMock:
        return targets_table if name == crud.TARGETS_TABLE else ut_table

    supabase.table.side_effect = _table
    return supabase


def test_get_active_unions_floor_and_membership_arms() -> None:
    floor = [_target_row("t-floor", "Catalog Role", app_active=True)]
    member = [_target_row("t-member", "User Role", app_active=False)]
    supabase = _client_for_get_active(floor, ["t-member"], member)

    ids = {t.id for t in crud.get_active(supabase)}

    assert ids == {"t-floor", "t-member"}


def test_get_active_dedupes_and_skips_member_fetch_when_covered() -> None:
    """A floor target that ALSO has an active member must not be re-fetched
    (missing set empty → no .in_ round-trip) nor duplicated."""
    floor = [_target_row("t-both", "Catalog Role", app_active=True)]
    supabase = _client_for_get_active(floor, ["t-both"], member_rows=[])

    targets = crud.get_active(supabase)

    assert [t.id for t in targets] == ["t-both"]


def test_is_pipeline_active_short_circuits_on_floor() -> None:
    supabase = MagicMock()
    (
        supabase.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data
    ) = [{"app_active": True}]

    assert crud.is_pipeline_active(supabase, "t-1") is True
    # Only the targets read happened — no membership count round-trip.
    assert supabase.table.call_count == 1


def test_is_pipeline_active_falls_through_to_membership_count() -> None:
    supabase = MagicMock()
    targets_table = MagicMock()
    (
        targets_table.select.return_value.eq.return_value.limit.return_value.execute.return_value.data
    ) = [{"app_active": False}]
    ut_table = MagicMock()
    (
        ut_table.select.return_value.eq.return_value.eq.return_value.execute.return_value.count
    ) = 2

    supabase.table.side_effect = (
        lambda name: targets_table if name == crud.TARGETS_TABLE else ut_table
    )

    assert crud.is_pipeline_active(supabase, "t-1") is True


def test_is_pipeline_active_false_for_missing_or_dormant_target() -> None:
    supabase = MagicMock()
    targets_table = MagicMock()
    (
        targets_table.select.return_value.eq.return_value.limit.return_value.execute.return_value.data
    ) = []
    supabase.table.side_effect = lambda name: targets_table

    assert crud.is_pipeline_active(supabase, "t-missing") is False
