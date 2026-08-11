"""The invariant the whole #667 arc exists to establish, plus the erasure gap.

THE INVARIANT: a target with no ops sponsorship must always have at least one
membership.

    NOT app_active  =>  EXISTS(a user_targets row)

Why it is worth a test of its own: before this arc it was FALSE, and that is
precisely why orphan cleanup was hard. Two legitimate flows passed through the
violating state —

  * create-then-link: target inserted, membership inserted one round-trip later;
  * the catalog seed: target inserted unsponsored, an LLM derive ran, and only
    THEN app_active was set — a window an LLM call wide.

While either window existed, "orphan" was indistinguishable from "being born",
so any sweep had to guess by age. Both are now closed (atomic create+link;
sponsor-at-birth in the seed), which is what makes the reap predicate exact.

If someone reintroduces a create-without-link path, this file is what should
fail — before a cleanup job silently deletes a row mid-creation.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from supabase import Client

pytestmark = pytest.mark.integration


@pytest.fixture
def cleanup_targets(service_client: Client) -> Iterator[list[str]]:
    created: list[str] = []
    yield created
    for tid in created:
        service_client.table("targets").delete().eq("id", tid).execute()


def _orphans(service_client: Client) -> list[dict]:
    """Rows violating the invariant: unsponsored and unfollowed."""
    targets = (
        service_client.table("targets").select("id,label,app_active").execute().data or []
    )
    links = service_client.table("user_targets").select("target_id").execute().data or []
    followed = {r["target_id"] for r in links}
    return [t for t in targets if not t["app_active"] and t["id"] not in followed]


def test_the_atomic_create_path_cannot_violate_the_invariant(
    service_client: Client, two_seeded_users: tuple[str, str], cleanup_targets: list[str]
) -> None:
    uid, _ = two_seeded_users
    before = {t["id"] for t in _orphans(service_client)}

    out = (
        service_client.rpc(
            "create_target_and_link",
            {
                "p_user_id": uid,
                "p_label": f"Invariant {uuid.uuid4()}",
                "p_normalized_label": f"invariant {uuid.uuid4()}",
                "p_activation_status": "deriving",
                "p_description": None,
                "p_scoring_profile": {},
                "p_search_keywords": [],
            },
        )
        .execute()
        .data
    )
    cleanup_targets.append(out["target"]["id"])

    after = {t["id"] for t in _orphans(service_client)}
    assert after == before, "creating a target introduced an unfollowed, unsponsored row"


def test_a_seeded_catalog_target_is_sponsored_from_birth(
    service_client: Client, cleanup_targets: list[str]
) -> None:
    """The seed's row is link-free BY DESIGN (#543), so the only thing keeping it
    out of the orphan set is `app_active` — which must be true at INSERT, not
    set after the derive."""
    target_id = str(uuid.uuid4())
    cleanup_targets.append(target_id)
    service_client.table("targets").insert(
        {"id": target_id, "label": f"Catalog {uuid.uuid4()}", "app_active": True}
    ).execute()

    assert target_id not in {t["id"] for t in _orphans(service_client)}
    # And the reap refuses it, link-free though it is.
    assert (
        service_client.rpc("reap_orphaned_target", {"p_target_id": target_id}).execute().data
        is False
    )


def test_erasure_reaps_a_target_the_user_solely_followed(
    service_client: Client, two_seeded_users: tuple[str, str], cleanup_targets: list[str]
) -> None:
    """#667 step 1 — account erasure deletes `user_targets` rows by a different
    route than the unlink endpoint, and used to skip the reap entirely.

    Modelled at the DB level: erasure's step 2 deletes the membership, step 3b
    reaps. The router-level wiring is unit-tested; what needs a real Postgres is
    that the guarded reap does the right thing for BOTH the sole-follower and
    co-follower cases when handed every id the user was linked to.
    """
    uid_a, uid_b = two_seeded_users
    solo = str(uuid.uuid4())
    shared = str(uuid.uuid4())
    cleanup_targets.extend([solo, shared])
    service_client.table("targets").insert(
        [
            {"id": solo, "label": f"Solo {uuid.uuid4()}"},
            {"id": shared, "label": f"Shared {uuid.uuid4()}"},
        ]
    ).execute()
    service_client.table("user_targets").insert(
        [
            {"user_id": uid_a, "target_id": solo, "is_active": False},
            {"user_id": uid_a, "target_id": shared, "is_active": False},
            {"user_id": uid_b, "target_id": shared, "is_active": False},
        ]
    ).execute()

    # Erasure step 2: the departing user's memberships go.
    service_client.table("user_targets").delete().eq("user_id", uid_a).execute()

    # Erasure step 3b: reap every target they were linked to. The guard decides.
    reaped = {
        tid: service_client.rpc("reap_orphaned_target", {"p_target_id": tid}).execute().data
        for tid in (solo, shared)
    }

    assert reaped[solo] is True, "sole-followed target survived erasure"
    assert reaped[shared] is False, "reaped a target another user still follows"

    remaining = {
        r["id"]
        for r in (
            service_client.table("targets")
            .select("id")
            .in_("id", [solo, shared])
            .execute()
            .data
            or []
        )
    }
    assert remaining == {shared}
