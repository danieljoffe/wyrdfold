"""DB-layer gate for reference-JD contributor attribution + remove-own (#5).

Each reference JD records the user who added it (``user_id``) so the shared
profile merge can de-bias by contributor, and so the "remove your own
contribution" delete is scoped to the caller. These exercise the writes the
route makes against real Postgres: attribution is persisted, a non-contributor
cannot delete someone else's JD, and the contributor can.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from supabase import AsyncClient, Client

from app.models.targets import ScoringProfile
from app.routers.targets import _delete_reference_jd_async
from app.services.targets import crud

pytestmark = pytest.mark.integration


@pytest.fixture
def target_id(service_client: Client) -> Iterator[str]:
    tid: str = (
        service_client.table("targets")
        .insert({"label": f"RefJD Attribution {uuid.uuid4()}"})
        .execute()
        .data[0]["id"]
    )
    try:
        yield tid
    finally:
        service_client.table("reference_jds").delete().eq("target_id", tid).execute()
        service_client.table("targets").delete().eq("id", tid).execute()


def _add(client: Client, target_id: str, user_id: str | None) -> crud.TargetReferenceJD:
    return crud.add_reference_jd(
        client,
        target_id=target_id,
        jd_text=f"JD from {user_id}",
        jd_url=None,
        extracted_profile=ScoringProfile(),
        user_id=user_id,
    )


def test_add_records_contributor(
    service_client: Client, target_id: str, two_seeded_users: tuple[str, str]
) -> None:
    uid_a, _ = two_seeded_users
    ref = _add(service_client, target_id, uid_a)
    assert ref.user_id == uid_a
    # ...and it round-trips through the list read the merge consumes.
    assert [j.user_id for j in crud.list_reference_jds(service_client, target_id)] == [uid_a]


async def test_remove_own_is_scoped_to_contributor(
    service_client: Client,
    async_service_client: AsyncClient,
    target_id: str,
    two_seeded_users: tuple[str, str],
) -> None:
    uid_a, uid_b = two_seeded_users
    ref_a = _add(service_client, target_id, uid_a)

    # B cannot delete A's contribution: no row matches -> False, JD persists.
    assert (
        await _delete_reference_jd_async(
            async_service_client, ref_a.id, target_id=target_id, user_id=uid_b
        )
        is False
    )
    assert len(crud.list_reference_jds(service_client, target_id)) == 1

    # A can delete their own.
    assert (
        await _delete_reference_jd_async(
            async_service_client, ref_a.id, target_id=target_id, user_id=uid_a
        )
        is True
    )
    assert crud.list_reference_jds(service_client, target_id) == []


async def test_remove_is_scoped_to_target(
    service_client: Client,
    async_service_client: AsyncClient,
    target_id: str,
    two_seeded_users: tuple[str, str],
) -> None:
    """Audit #24 F1: a JD id from another target matches no row, even for its
    own contributor, so the route's target-ownership check can't be sidestepped."""
    uid_a, _ = two_seeded_users
    ref_a = _add(service_client, target_id, uid_a)

    other = str(uuid.uuid4())
    assert (
        await _delete_reference_jd_async(
            async_service_client, ref_a.id, target_id=other, user_id=uid_a
        )
        is False
    )
    assert len(crud.list_reference_jds(service_client, target_id)) == 1
