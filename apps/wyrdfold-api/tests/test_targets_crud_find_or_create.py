"""crud.create is find-or-create on normalized_label (defense-in-depth with
the UNIQUE(normalized_label) constraint, migration 20260717060000)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from app.models.targets import TargetCreate
from app.services.targets import crud


def _target_row(label: str, *, target_id: str = "tgt-1") -> dict[str, Any]:
    return {
        "id": target_id,
        "label": label,
        "normalized_label": label.lower().strip(),
        "scoring_profile": {},
        "is_active": False,
        "created_at": "2026-07-18T00:00:00+00:00",
        "updated_at": "2026-07-18T00:00:00+00:00",
    }


def test_create_inserts_when_no_conflict() -> None:
    """No existing row → the upsert inserts and returns the new target, and it
    uses the race-safe on-conflict/ignore-duplicates idiom."""
    supabase = MagicMock()
    (
        supabase.table.return_value.upsert.return_value.execute.return_value.data
    ) = [_target_row("Data Scientist")]

    t = crud.create(supabase, TargetCreate(label="Data Scientist"))

    assert t.id == "tgt-1"
    _, kwargs = supabase.table.return_value.upsert.call_args
    assert kwargs.get("on_conflict") == "normalized_label"
    assert kwargs.get("ignore_duplicates") is True


def test_create_returns_existing_on_conflict() -> None:
    """A normalized_label collision (ignore-duplicates upsert → empty data)
    must return the existing canonical target, never raise a 23505."""
    supabase = MagicMock()
    # ignore-duplicates upsert skipped the row → empty representation.
    supabase.table.return_value.upsert.return_value.execute.return_value.data = []
    # get_by_normalized_label finds the row that already held the label.
    (
        supabase.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data
    ) = [_target_row("Data Scientist", target_id="tgt-existing")]

    t = crud.create(supabase, TargetCreate(label="DATA SCIENTIST"))

    assert t.id == "tgt-existing"
