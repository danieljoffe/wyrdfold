"""Every abandoned activation is recorded, and the record survives recovery (#1090).

The point of this log is to answer "how often is deferred work abandoned?"
so that question stops being a guess. A marker column could not answer it:
it holds one value and is cleared when the user re-activates, so repeated
abandonments of one target collapse into one and a target that recovers
disappears from the count entirely — a survivor-biased number that cannot
tell a rare defect from a frequent one with quick recovery.

These run against real Postgres because that is the claim under test: rows
accumulate, and nothing in the normal lifecycle deletes them.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from supabase import AsyncClient, Client

from app.services.targets.activation import sweep_stalled_activations

pytestmark = pytest.mark.integration


@pytest.fixture
def stalled_target(service_client: Client) -> Iterator[str]:
    """A target stuck in an in-flight state, old enough for the sweep."""
    stale = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
    tid = (
        service_client.table("targets")
        .insert({"label": f"Reclaim {uuid.uuid4()}", "activation_status": "polling"})
        .execute()
        .data[0]["id"]
    )
    service_client.table("targets").update({"updated_at": stale}).eq("id", tid).execute()
    yield tid
    service_client.table("targets").delete().eq("id", tid).execute()


def _reclaims(client: Client, target_id: str) -> list[dict[str, Any]]:
    return (
        client.table("activation_reclaims")
        .select("target_id, from_status, stale_after_hours, reclaimed_at")
        .eq("target_id", target_id)
        .order("reclaimed_at")
        .execute()
        .data
    )


def _restall(client: Client, target_id: str) -> None:
    stale = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
    client.table("targets").update({"activation_status": "polling"}).eq("id", target_id).execute()
    client.table("targets").update({"updated_at": stale}).eq("id", target_id).execute()


@pytest.mark.asyncio
async def test_a_reclaim_is_recorded_with_its_stage_and_window(
    service_client: Client, async_service_client: AsyncClient, stalled_target: str
) -> None:
    await sweep_stalled_activations(async_service_client, stale_after_hours=6)

    events = _reclaims(service_client, stalled_target)
    assert len(events) == 1
    assert events[0]["from_status"] == "polling"
    assert events[0]["stale_after_hours"] == 6
    # And the target itself is back in the re-activatable state.
    row = (
        service_client.table("targets")
        .select("activation_status")
        .eq("id", stalled_target)
        .execute()
        .data[0]
    )
    assert row["activation_status"] == "idle"


@pytest.mark.asyncio
async def test_repeated_abandonment_accumulates_instead_of_collapsing(
    service_client: Client, async_service_client: AsyncClient, stalled_target: str
) -> None:
    """A target that stalls, recovers, and stalls again is TWO abandonments.
    This is the property a marker column cannot have, and the reason the
    frequency it measures would otherwise be wrong."""
    await sweep_stalled_activations(async_service_client, stale_after_hours=6)
    _restall(service_client, stalled_target)
    await sweep_stalled_activations(async_service_client, stale_after_hours=6)

    assert len(_reclaims(service_client, stalled_target)) == 2


@pytest.mark.asyncio
async def test_the_record_survives_the_user_recovering_the_target(
    service_client: Client, async_service_client: AsyncClient, stalled_target: str
) -> None:
    """The survivor-bias case: a target reclaimed and then successfully
    re-activated must still be counted. If recovery erased the evidence, a
    frequent-but-quickly-recovered defect would read as a rare one."""
    await sweep_stalled_activations(async_service_client, stale_after_hours=6)
    assert len(_reclaims(service_client, stalled_target)) == 1

    # The user re-activates and it completes normally this time.
    service_client.table("targets").update(
        {"activation_status": "ready", "activation_error": None}
    ).eq("id", stalled_target).execute()

    assert len(_reclaims(service_client, stalled_target)) == 1, (
        "recovering a target must not erase the record that it was abandoned"
    )


@pytest.mark.asyncio
async def test_a_healthy_target_is_never_recorded(
    service_client: Client, async_service_client: AsyncClient
) -> None:
    """A record that appears without an abandonment would make the count a lie."""
    tid = (
        service_client.table("targets")
        .insert({"label": f"Healthy {uuid.uuid4()}", "activation_status": "ready"})
        .execute()
        .data[0]["id"]
    )
    try:
        await sweep_stalled_activations(async_service_client, stale_after_hours=6)
        assert _reclaims(service_client, tid) == []
    finally:
        service_client.table("targets").delete().eq("id", tid).execute()


@pytest.mark.asyncio
async def test_the_record_survives_the_target_being_deleted(
    service_client: Client, async_service_client: AsyncClient, stalled_target: str
) -> None:
    """The subtlest survivor bias, and the one most likely to bite.

    A user whose target sat stuck for hours is more likely than average to
    delete it. If deleting the target took its reclaim history with it, the
    deletions would correlate with the very failure being measured and the
    count would drift back toward "reclaims for targets that still exist".
    The event survives with its target reference severed: the measurement
    needs the stage, the window and the time, not the identifier.
    """
    await sweep_stalled_activations(async_service_client, stale_after_hours=6)
    events = _reclaims(service_client, stalled_target)
    assert len(events) == 1
    event_id = (
        service_client.table("activation_reclaims")
        .select("id")
        .eq("target_id", stalled_target)
        .execute()
        .data[0]["id"]
    )

    service_client.table("targets").delete().eq("id", stalled_target).execute()

    survivor = (
        service_client.table("activation_reclaims")
        .select("id, target_id, from_status, stale_after_hours, reclaimed_at")
        .eq("id", event_id)
        .execute()
        .data
    )
    assert len(survivor) == 1, "deleting a target must not erase that it was abandoned"
    assert survivor[0]["target_id"] is None, "the user-linked identifier is severed"
    # Everything the measurement actually reads is intact.
    assert survivor[0]["from_status"] == "polling"
    assert survivor[0]["stale_after_hours"] == 6
    assert survivor[0]["reclaimed_at"]

    service_client.table("activation_reclaims").delete().eq("id", event_id).execute()
