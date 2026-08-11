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
        "app_active": False,
        "created_at": "2026-07-18T00:00:00+00:00",
        "updated_at": "2026-07-18T00:00:00+00:00",
    }


def test_create_inserts_when_no_conflict() -> None:
    """No existing row → the upsert inserts and returns the new target, and it
    uses the race-safe on-conflict/ignore-duplicates idiom."""
    supabase = MagicMock()
    (supabase.table.return_value.upsert.return_value.execute.return_value.data) = [
        _target_row("Data Scientist")
    ]

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


# ---- app_active is internal-only (#667) ------------------------------------


def test_target_create_model_cannot_carry_app_active() -> None:
    """``app_active`` must NOT be a ``TargetCreate`` field.

    That model is bound straight from the ``POST /targets`` request body, so a
    field here would let any caller self-sponsor a catalog target — which
    bypasses membership scoping, keeps the row pipeline-active, and makes it
    permanently un-reapable. It is a keyword argument on ``crud.create``
    instead, reachable only from the seed script.
    """
    assert "app_active" not in TargetCreate.model_fields
    # Pydantic must also reject it if someone posts it anyway.
    created = TargetCreate(label="Some Role", app_active=True)  # type: ignore[call-arg]
    assert not hasattr(created, "app_active")


def test_create_sponsors_at_birth_only_when_asked() -> None:
    """The seed sponsors at INSERT so the row is never in the ambiguous
    unsponsored-and-unfollowed state; every other caller must not.

    Before this, the seed created the row unsponsored, ran an LLM derive, and
    only then set ``app_active`` — a window an LLM call wide in which a
    legitimate catalog row was indistinguishable from an orphan.
    """
    supabase = MagicMock()
    (supabase.table.return_value.upsert.return_value.execute.return_value.data) = [
        _target_row("Catalog Role")
    ]

    crud.create(supabase, TargetCreate(label="Catalog Role"), app_active=True)
    row, _ = supabase.table.return_value.upsert.call_args
    assert row[0].get("app_active") is True

    supabase.reset_mock()
    (supabase.table.return_value.upsert.return_value.execute.return_value.data) = [
        _target_row("User Role")
    ]
    crud.create(supabase, TargetCreate(label="User Role"))
    row, _ = supabase.table.return_value.upsert.call_args
    assert "app_active" not in row[0], "a user-path create must never self-sponsor"
