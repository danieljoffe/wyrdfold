"""P0 re-semantics (schema audit 2026-07-31): the app_active floor survives
membership traffic, and pipeline-active is derived — not trigger-cached.

The bug these pin down: the old ``trg_sync_target_active`` recomputed
``targets.is_active = EXISTS(active membership)`` on ANY ``user_targets``
event, so the first user to *follow* a catalog target (an inactive link — the
catalog-search happy path) permanently deactivated its ingestion, and an
activate→deactivate cycle did the same. With the trigger dropped and the flag
demoted to the instance floor, no membership traffic may ever write it.

Real-Postgres tests: the invariant lives in the DB (absence of a trigger),
which mocks cannot witness.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from supabase import Client

from app.services.targets import crud
from tests.integration.conftest import create_auth_user, delete_auth_user

pytestmark = pytest.mark.integration


def _app_active_in_db(service_client: Client, target_id: str) -> bool:
    row = (
        service_client.table("targets")
        .select("app_active")
        .eq("id", target_id)
        .single()
        .execute()
        .data
    )
    return bool(row["app_active"])


@pytest.fixture
def catalog_target(service_client: Client) -> Iterator[str]:
    """A link-free target with the instance floor raised — the #543 shape."""
    tid: str = (
        service_client.table("targets")
        .insert({"label": f"P0 Catalog {uuid.uuid4()}", "app_active": True})
        .execute()
        .data[0]["id"]
    )
    try:
        yield tid
    finally:
        service_client.table("user_targets").delete().eq("target_id", tid).execute()
        service_client.table("targets").delete().eq("id", tid).execute()


@pytest.fixture
def auth_user(service_client: Client) -> Iterator[str]:
    uid = create_auth_user(service_client)
    try:
        yield uid
    finally:
        delete_auth_user(service_client, uid)


def test_inactive_follow_keeps_catalog_target_pipeline_active(
    service_client: Client, catalog_target: str, auth_user: str
) -> None:
    """THE P0 regression: an inactive link (catalog-search 'follow') must not
    clobber the floor. Under the old trigger this flipped is_active=false."""
    crud.link_user_to_target(
        service_client, user_id=auth_user, target_id=catalog_target, is_active=False
    )

    assert _app_active_in_db(service_client, catalog_target) is True
    assert crud.is_pipeline_active(service_client, catalog_target) is True
    assert catalog_target in {t.id for t in crud.get_active(service_client)}


def test_activate_deactivate_cycle_keeps_catalog_target_pipeline_active(
    service_client: Client, catalog_target: str, auth_user: str
) -> None:
    """Second kill path: activate then deactivate a membership. The old
    trigger's recompute on the deactivate erased the manual floor."""
    crud.link_user_to_target(
        service_client, user_id=auth_user, target_id=catalog_target, is_active=True
    )
    crud.set_user_target_inactive(service_client, user_id=auth_user, target_id=catalog_target)

    assert _app_active_in_db(service_client, catalog_target) is True
    assert crud.is_pipeline_active(service_client, catalog_target) is True
    assert catalog_target in {t.id for t in crud.get_active(service_client)}


def test_user_target_drops_out_when_last_member_deactivates(
    service_client: Client, auth_user: str
) -> None:
    """A plain user target (floor down) is pipeline-active exactly while it
    has an active membership — the EXISTS arm of the derived predicate."""
    tid: str = (
        service_client.table("targets")
        .insert({"label": f"P0 User Target {uuid.uuid4()}"})
        .execute()
        .data[0]["id"]
    )
    try:
        assert _app_active_in_db(service_client, tid) is False  # new default

        crud.link_user_to_target(service_client, user_id=auth_user, target_id=tid, is_active=True)
        assert crud.is_pipeline_active(service_client, tid) is True
        assert tid in {t.id for t in crud.get_active(service_client)}

        crud.set_user_target_inactive(service_client, user_id=auth_user, target_id=tid)
        assert crud.is_pipeline_active(service_client, tid) is False
        assert tid not in {t.id for t in crud.get_active(service_client)}
    finally:
        service_client.table("user_targets").delete().eq("target_id", tid).execute()
        service_client.table("targets").delete().eq("id", tid).execute()


def test_get_active_dedupes_floor_and_membership_arms(
    service_client: Client, catalog_target: str, auth_user: str
) -> None:
    """A target satisfying BOTH arms (floor up + active member) appears once."""
    crud.link_user_to_target(
        service_client, user_id=auth_user, target_id=catalog_target, is_active=True
    )

    ids = [t.id for t in crud.get_active(service_client)]
    assert ids.count(catalog_target) == 1
