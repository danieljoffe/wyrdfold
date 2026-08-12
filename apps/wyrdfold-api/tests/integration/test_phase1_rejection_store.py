"""Integration tests for the persistent Phase-1 rejection store.

The unit fakes (``tests/support/fake_phase1_store.py``) prove the poller
wiring; only a live PostgREST can prove the store's SQL actually
round-trips — the ``on_conflict`` clause syntax, the ``judged_at``
timestamptz comparison against an ISO-string cutoff, and the FK CASCADE
that reaps a deleted target's rejections.

Self-skips when the local stack is unreachable (see conftest).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from supabase import Client

from app.services.relevance.rejection_store import (
    fetch_rejected_titles,
    record_rejections,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def store_target(service_client: Client) -> Iterator[SimpleNamespace]:
    """A real ``targets`` row (the FK parent) wrapped in the duck-typed
    shape the store reads (``.id`` / ``.profile_version``). Deleting the
    row on teardown also exercises production cleanup: rejections must
    CASCADE away with their target."""
    target_id = str(uuid.uuid4())
    service_client.table("targets").insert(
        {"id": target_id, "label": f"Rejection Store IT {target_id[:8]}"}
    ).execute()
    yield SimpleNamespace(id=target_id, profile_version=1)
    service_client.table("targets").delete().eq("id", target_id).execute()


def _rows(service_client: Client, target_id: str) -> list[dict]:
    return (
        service_client.table("phase1_rejections")
        .select("*")
        .eq("target_id", target_id)
        .execute()
        .data
        or []
    )


@pytest.mark.asyncio
async def test_roundtrip_upsert_ttl_and_profile_keying(
    service_client: Client, store_target: SimpleNamespace
) -> None:
    # Write two rejections; messy title must normalize on the way in.
    await record_rejections(
        service_client, store_target, [("Senior  Backend\tEngineer", 91), ("Sales Lead", None)]
    )
    stored = {r["title_norm"]: r for r in _rows(service_client, store_target.id)}
    assert set(stored) == {"senior backend engineer", "sales lead"}
    assert stored["senior backend engineer"]["confidence"] == 91

    # Read path: hit on a differently-messy variant, miss on the unknown.
    hits = await fetch_rejected_titles(
        service_client,
        store_target,
        ["SENIOR BACKEND ENGINEER", "Sales Lead", "Frontend Engineer"],
    )
    assert hits == {"senior backend engineer", "sales lead"}

    # Profile bump = different key = clean miss (positive control above).
    bumped = SimpleNamespace(id=store_target.id, profile_version=2)
    assert await fetch_rejected_titles(service_client, bumped, ["Sales Lead"]) == set()

    # Re-recording the same title must UPDATE the row (on_conflict), not
    # raise a duplicate-key error nor add a second row — and it refreshes
    # judged_at past a deliberately staled value.
    stale = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    service_client.table("phase1_rejections").update({"judged_at": stale}).eq(
        "target_id", store_target.id
    ).eq("title_norm", "sales lead").execute()
    await record_rejections(service_client, store_target, [("Sales Lead", 77)])
    rows = [r for r in _rows(service_client, store_target.id) if r["title_norm"] == "sales lead"]
    assert len(rows) == 1
    assert rows[0]["judged_at"] > stale
    assert rows[0]["confidence"] == 77

    # TTL: a row older than the window is invisible to the read path even
    # though it still physically exists (retention sweeps it later).
    expired = (
        datetime.now(UTC) - timedelta(hours=2000)  # past the 1440h default
    ).isoformat()
    service_client.table("phase1_rejections").update({"judged_at": expired}).eq(
        "target_id", store_target.id
    ).eq("title_norm", "sales lead").execute()
    hits = await fetch_rejected_titles(service_client, store_target, ["Sales Lead"])
    assert hits == set()
    assert _rows(service_client, store_target.id)  # precondition: row still there


@pytest.mark.asyncio
async def test_target_delete_cascades_rejections(service_client: Client) -> None:
    target_id = str(uuid.uuid4())
    service_client.table("targets").insert(
        {"id": target_id, "label": f"Cascade IT {target_id[:8]}"}
    ).execute()
    target = SimpleNamespace(id=target_id, profile_version=1)
    await record_rejections(service_client, target, [("Doomed Role", None)])
    assert _rows(service_client, target_id)  # precondition: rejection persisted

    service_client.table("targets").delete().eq("id", target_id).execute()
    assert _rows(service_client, target_id) == []
