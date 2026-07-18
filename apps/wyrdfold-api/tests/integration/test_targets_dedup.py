"""Integration: crud.create is find-or-create against the real
UNIQUE(normalized_label) constraint (migration 20260717060000).

Proves the duplicate path end-to-end: a second create for the same
normalized label returns the existing catalog row rather than a second row
or a 23505 surfaced as a 500.
"""

from __future__ import annotations

import uuid

from supabase import Client

from app.models.targets import TargetCreate
from app.services.targets import crud


def test_crud_create_is_idempotent_find_or_create(service_client: Client) -> None:
    label = f"Dedup Int {uuid.uuid4()}"
    normalized = label.lower().strip()
    created: list[str] = []
    try:
        t1 = crud.create(service_client, TargetCreate(label=label))
        created.append(t1.id)

        # Same role, different case → same normalized_label → find-or-create
        # returns the existing row (no exception, no duplicate).
        t2 = crud.create(service_client, TargetCreate(label=label.upper()))
        created.append(t2.id)
        assert t2.id == t1.id, "duplicate create must return the existing target"

        rows = (
            service_client.table("targets")
            .select("id")
            .eq("normalized_label", normalized)
            .execute()
            .data
        )
        assert len(rows) == 1, "exactly one catalog row for the normalized label"
    finally:
        for tid in set(created):
            service_client.table("targets").delete().eq("id", tid).execute()
