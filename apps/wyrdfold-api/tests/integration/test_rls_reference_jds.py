"""RLS gate for audit #29 R3 M3 — reference_jds reads are follower-scoped.

The deanonymization the audit flagged: reference_jds carried a single
permissive SELECT policy (`reference_jds_authenticated_read` USING (true)),
so any authenticated caller — or anyone replaying the browser anon key at
PostgREST — could read every contributed ``jd_text`` AND the contributor
``user_id`` (#5 P2), unmasking the "anonymous" contribution graph.

The fix (20260623120000) replaces that with
``reference_jds_follower_read``: a user may read a target's reference JDs
only if they FOLLOW the target (a row in ``user_targets`` ties them to it).

These prove the policy with the JWT-bound user client the API would use:
- a follower sees the target's JD (incl. its contributor user_id),
- a non-follower (authenticated, but not linked to the target) sees nothing,
- a raw cross-target read by id still returns nothing for a non-follower,
- service-role (the backend's real read path) still sees the row — so the
  empty user-client results above are RLS doing its job, not absent data.

If someone reverts the policy to USING (true), the non-follower assertions
fail; if the backend's service-role read ever regressed onto a user client,
the service-role contrast fails.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

import pytest
from supabase import Client

from app.models.targets import ScoringProfile
from app.services.targets import crud

pytestmark = pytest.mark.integration


@pytest.fixture
def target_with_follower_and_jd(
    service_client: Client,
    two_seeded_users: tuple[str, str],
) -> Iterator[tuple[str, str, str, str]]:
    """A target, one reference JD attributed to user A, and a user_targets
    link making user A a FOLLOWER (user B is left un-linked).

    Yields (target_id, ref_jd_id, follower_uid, non_follower_uid).
    """
    uid_a, uid_b = two_seeded_users  # A becomes the follower, B the non-follower
    target_id: str = (
        service_client.table("targets")
        .insert({"label": f"RJD RLS {uuid.uuid4()}"})
        .execute()
        .data[0]["id"]
    )
    ref = crud.add_reference_jd(
        service_client,
        target_id=target_id,
        jd_text="follower-only reference jd body",
        jd_url=None,
        extracted_profile=ScoringProfile(),
        user_id=uid_a,
    )
    # User A follows the target; user B does not.
    service_client.table("user_targets").insert(
        {"user_id": uid_a, "target_id": target_id, "is_active": True}
    ).execute()
    try:
        yield target_id, ref.id, uid_a, uid_b
    finally:
        service_client.table("user_targets").delete().eq("target_id", target_id).execute()
        service_client.table("reference_jds").delete().eq("target_id", target_id).execute()
        service_client.table("targets").delete().eq("id", target_id).execute()


def test_service_role_sees_the_reference_jd(
    service_client: Client,
    target_with_follower_and_jd: tuple[str, str, str, str],
) -> None:
    target_id, ref_id, _, _ = target_with_follower_and_jd
    rows = (
        service_client.table("reference_jds")
        .select("id, user_id, jd_text")
        .eq("target_id", target_id)
        .execute()
        .data
    )
    assert {r["id"] for r in rows} == {ref_id}


def test_follower_can_read_the_reference_jd(
    user_client_factory: Callable[[str], Client],
    target_with_follower_and_jd: tuple[str, str, str, str],
) -> None:
    target_id, ref_id, follower_uid, _ = target_with_follower_and_jd
    client = user_client_factory(follower_uid)

    # No .eq on user_id — RLS (the follower EXISTS join) alone must scope this.
    rows = (
        client.table("reference_jds")
        .select("id, user_id, jd_text")
        .eq("target_id", target_id)
        .execute()
        .data
    )
    assert {r["id"] for r in rows} == {ref_id}


def test_non_follower_cannot_read_the_reference_jd(
    user_client_factory: Callable[[str], Client],
    target_with_follower_and_jd: tuple[str, str, str, str],
) -> None:
    target_id, _, _, non_follower_uid = target_with_follower_and_jd
    client = user_client_factory(non_follower_uid)

    rows = (
        client.table("reference_jds")
        .select("id, user_id, jd_text")
        .eq("target_id", target_id)
        .execute()
        .data
    )
    assert rows == [], "RLS leak: a non-follower can read the target's reference JDs"


def test_non_follower_cross_read_by_id_returns_empty(
    user_client_factory: Callable[[str], Client],
    target_with_follower_and_jd: tuple[str, str, str, str],
) -> None:
    _, ref_id, _, non_follower_uid = target_with_follower_and_jd
    client = user_client_factory(non_follower_uid)

    # Even targeting the row by primary key, RLS yields nothing — so neither
    # jd_text nor the contributor user_id leaks.
    rows = client.table("reference_jds").select("id, user_id").eq("id", ref_id).execute().data
    assert rows == []
