"""Integration tests for atomic target creation (#667 follow-up).

Unit tests can only prove the caller invokes the RPC. The claims that matter are
all about what Postgres does, and none of them are observable from a mock:

  * the target and the membership land in ONE transaction, so the
    "target exists, nobody follows it" state — which IS the definition of an
    orphan — never becomes visible;
  * find-or-create stays idempotent on ``normalized_label`` and does not
    clobber a co-followed catalog row's content;
  * the active-target ceiling trigger still governs the membership.

Self-skips when the local stack is unreachable (see conftest).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from supabase import Client

pytestmark = pytest.mark.integration


def _call(
    service_client: Client,
    *,
    user_id: str,
    label: str,
    normalized: str | None = None,
    activation_status: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    return (
        service_client.rpc(
            "create_target_and_link",
            {
                "p_user_id": user_id,
                "p_label": label,
                "p_normalized_label": normalized or label.strip().lower(),
                "p_activation_status": activation_status,
                "p_description": description,
                "p_scoring_profile": {},
                "p_search_keywords": [],
            },
        )
        .execute()
        .data
    )


@pytest.fixture
def cleanup_targets(service_client: Client) -> Iterator[list[str]]:
    created: list[str] = []
    yield created
    for tid in created:
        service_client.table("targets").delete().eq("id", tid).execute()


def test_creates_target_and_membership_together(
    service_client: Client, two_seeded_users: tuple[str, str], cleanup_targets: list[str]
) -> None:
    uid, _ = two_seeded_users
    label = f"Atomic {uuid.uuid4()}"

    out = _call(service_client, user_id=uid, label=label, activation_status="deriving")

    target, link = out["target"], out["user_target"]
    cleanup_targets.append(target["id"])

    assert target["label"] == label
    assert target["activation_status"] == "deriving"
    # Never sponsored by a user action — that is the ops-only floor (#543).
    assert target["app_active"] is False
    assert link["target_id"] == target["id"]
    assert str(link["user_id"]) == uid
    # Following never trips the active-target cap.
    assert link["is_active"] is False


def test_the_orphan_state_is_never_visible(
    service_client: Client, two_seeded_users: tuple[str, str], cleanup_targets: list[str]
) -> None:
    """THE point of the change.

    After the call there is no moment at which the target exists without a
    membership — so `NOT app_active AND no memberships` stops being a state the
    happy path passes through, and becomes an unambiguously invalid one. That is
    what lets the reap predicate be exact instead of guessing by age.
    """
    uid, _ = two_seeded_users
    out = _call(service_client, user_id=uid, label=f"NoWindow {uuid.uuid4()}")
    target_id = out["target"]["id"]
    cleanup_targets.append(target_id)

    memberships = (
        service_client.table("user_targets").select("id").eq("target_id", target_id).execute().data
    )
    assert len(memberships or []) == 1

    # ...and therefore the reap — the exact predicate — refuses to touch it.
    reaped = service_client.rpc("reap_orphaned_target", {"p_target_id": target_id}).execute().data
    assert reaped is False
    still_there = service_client.table("targets").select("id").eq("id", target_id).execute().data
    assert len(still_there or []) == 1


def test_find_or_create_is_idempotent_and_does_not_clobber(
    service_client: Client, two_seeded_users: tuple[str, str], cleanup_targets: list[str]
) -> None:
    """A second caller converges on the one canonical row and must not overwrite
    its content — the shared-catalog rule. Only the lifecycle column moves."""
    uid_a, uid_b = two_seeded_users
    label = f"Shared {uuid.uuid4()}"
    normalized = label.lower()

    first = _call(
        service_client,
        user_id=uid_a,
        label=label,
        normalized=normalized,
        activation_status="deriving",
        description="the original description",
    )
    target_id = first["target"]["id"]
    cleanup_targets.append(target_id)

    second = _call(
        service_client,
        user_id=uid_b,
        label="A DIFFERENT LABEL",
        normalized=normalized,
        activation_status="idle",
        description="an attempted overwrite",
    )

    assert second["target"]["id"] == target_id, "did not converge on the canonical row"
    # Content preserved — the shared row is not the second caller's to rewrite.
    assert second["target"]["label"] == label
    assert second["target"]["description"] == "the original description"
    # Only the lifecycle column follows the request.
    assert second["target"]["activation_status"] == "idle"

    rows = (
        service_client.table("user_targets").select("user_id").eq("target_id", target_id).execute()
    ).data or []
    assert {str(r["user_id"]) for r in rows} == {uid_a, uid_b}


def test_relinking_the_same_user_is_a_no_op(
    service_client: Client, two_seeded_users: tuple[str, str], cleanup_targets: list[str]
) -> None:
    uid, _ = two_seeded_users
    label = f"Twice {uuid.uuid4()}"

    first = _call(service_client, user_id=uid, label=label)
    cleanup_targets.append(first["target"]["id"])
    second = _call(service_client, user_id=uid, label=label)

    assert second["target"]["id"] == first["target"]["id"]
    assert second["user_target"]["id"] == first["user_target"]["id"]
    rows = (
        service_client.table("user_targets")
        .select("id")
        .eq("target_id", first["target"]["id"])
        .execute()
    ).data or []
    assert len(rows) == 1


def test_null_activation_status_leaves_an_existing_row_alone(
    service_client: Client, two_seeded_users: tuple[str, str], cleanup_targets: list[str]
) -> None:
    """Passing no status must not blank an existing one — the COALESCE in the
    conflict branch. A plain `SET activation_status = EXCLUDED...` would wipe it."""
    uid_a, uid_b = two_seeded_users
    label = f"Keep {uuid.uuid4()}"
    normalized = label.lower()

    first = _call(
        service_client,
        user_id=uid_a,
        label=label,
        normalized=normalized,
        activation_status="polling",
    )
    cleanup_targets.append(first["target"]["id"])

    second = _call(
        service_client,
        user_id=uid_b,
        label=label,
        normalized=normalized,
        activation_status=None,
    )

    assert second["target"]["activation_status"] == "polling"
