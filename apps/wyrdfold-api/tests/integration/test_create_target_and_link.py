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

import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from postgrest.exceptions import APIError
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
    is_active: bool = False,
    active_limit: int | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "p_user_id": user_id,
        "p_label": label,
        "p_normalized_label": normalized or label.strip().lower(),
        "p_activation_status": activation_status,
        "p_description": description,
        "p_scoring_profile": {},
        "p_search_keywords": [],
    }
    if is_active:
        # The legacy seven-key shape is what every pre-#1071 caller sends;
        # only the active shape adds the two new keys.
        params["p_is_active"] = True
        params["p_active_limit"] = active_limit
    return service_client.rpc("create_target_and_link", params).execute().data


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


# ---- #1071: active linking, the cap, and was_created ------------------------


def _rows_for(service_client: Client, normalized: str) -> list[dict[str, Any]]:
    return (
        service_client.table("targets")
        .select("id")
        .eq("normalized_label", normalized)
        .execute()
        .data
    )


def test_was_created_reports_insert_vs_conflict(
    service_client: Client, two_seeded_users: tuple[str, str], cleanup_targets: list[str]
) -> None:
    """The flag the from-posting route derives on: only the request that
    inserted the row gets ``true``; a later (or concurrent) exact-key call
    gets ``false`` and the same single row."""
    uid_a, uid_b = two_seeded_users
    label = f"Race {uuid.uuid4()}"

    first = _call(
        service_client,
        user_id=uid_a,
        label=label,
        activation_status="deriving",
        description="the winner's shared description",
    )
    cleanup_targets.append(first["target"]["id"])
    second = _call(service_client, user_id=uid_b, label=label, is_active=True, active_limit=5)

    assert first["was_created"] is True
    assert second["was_created"] is False
    assert second["target"]["id"] == first["target"]["id"]
    assert len(_rows_for(service_client, label.strip().lower())) == 1
    # The loser passed no status, so the winner's row is untouched: label,
    # description, profile, lifecycle status and error fields all as written.
    winner = (
        service_client.table("targets")
        .select("*")
        .eq("id", first["target"]["id"])
        .execute()
        .data[0]
    )
    for field in (
        "label",
        "description",
        "scoring_profile",
        "search_keywords",
        "activation_status",
        "activation_error",
    ):
        assert winner[field] == first["target"][field], field
    assert winner["activation_status"] == "deriving"


def test_active_request_links_active_and_activates_an_existing_inactive_link(
    service_client: Client, two_seeded_users: tuple[str, str], cleanup_targets: list[str]
) -> None:
    uid, _ = two_seeded_users
    label = f"Activate {uuid.uuid4()}"

    followed = _call(service_client, user_id=uid, label=label)
    cleanup_targets.append(followed["target"]["id"])
    assert followed["user_target"]["is_active"] is False

    active = _call(service_client, user_id=uid, label=label, is_active=True, active_limit=5)

    assert active["was_created"] is False
    assert active["user_target"]["is_active"] is True
    # Re-running the legacy shape never deactivates what the user activated.
    again = _call(service_client, user_id=uid, label=label)
    assert again["user_target"]["is_active"] is True


def test_cap_rejection_is_pt409_with_json_detail_and_leaves_no_target_row(
    service_client: Client, two_seeded_users: tuple[str, str], cleanup_targets: list[str]
) -> None:
    """The property the whole design rests on: a rejected active create rolls
    its own target insert back, so nothing owned by nobody is ever visible."""
    uid, _ = two_seeded_users
    held = _call(
        service_client, user_id=uid, label=f"Held {uuid.uuid4()}", is_active=True, active_limit=1
    )
    cleanup_targets.append(held["target"]["id"])
    assert held["was_created"] is True
    assert held["user_target"]["is_active"] is True

    rejected_label = f"Rejected {uuid.uuid4()}"
    with pytest.raises(APIError) as exc:
        _call(service_client, user_id=uid, label=rejected_label, is_active=True, active_limit=1)

    assert exc.value.code == "PT409"
    detail = json.loads(exc.value.details)
    assert detail == {"error": "ACTIVE_LIMIT", "active_count": 1, "limit": 1}
    assert _rows_for(service_client, rejected_label.strip().lower()) == []


def test_reactivating_the_held_link_is_exempt_from_the_cap(
    service_client: Client, two_seeded_users: tuple[str, str], cleanup_targets: list[str]
) -> None:
    """Re-activating a link the user already holds active changes no count,
    so an idempotent repeat at the cap succeeds (the app's rule, mirrored)."""
    uid, _ = two_seeded_users
    label = f"Repeat {uuid.uuid4()}"
    first = _call(service_client, user_id=uid, label=label, is_active=True, active_limit=1)
    cleanup_targets.append(first["target"]["id"])

    repeat = _call(service_client, user_id=uid, label=label, is_active=True, active_limit=1)

    assert repeat["was_created"] is False
    assert repeat["user_target"]["is_active"] is True


# ---- activate_user_target: the one activation path -------------------------


def _activate(
    client: Client, *, user_id: str, target_id: str, active_limit: int | None, **fit: Any
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "p_user_id": user_id,
        "p_target_id": target_id,
        "p_active_limit": active_limit,
    }
    params.update({f"p_{k}": v for k, v in fit.items()})
    return client.rpc("activate_user_target", params).execute().data


def _active_count(client: Client, user_id: str) -> int:
    rows = (
        client.table("user_targets")
        .select("target_id")
        .eq("user_id", user_id)
        .eq("is_active", True)
        .execute()
        .data
    )
    return len(rows)


def test_activate_user_target_enforces_the_cap_and_exempts_the_held_link(
    service_client: Client, two_seeded_users: tuple[str, str], cleanup_targets: list[str]
) -> None:
    uid, _ = two_seeded_users
    first = _call(service_client, user_id=uid, label=f"Act1 {uuid.uuid4()}")
    second = _call(service_client, user_id=uid, label=f"Act2 {uuid.uuid4()}")
    cleanup_targets += [first["target"]["id"], second["target"]["id"]]

    held = _activate(service_client, user_id=uid, target_id=first["target"]["id"], active_limit=1)
    assert held["is_active"] is True

    with pytest.raises(APIError) as exc:
        _activate(service_client, user_id=uid, target_id=second["target"]["id"], active_limit=1)
    assert exc.value.code == "PT409"
    assert json.loads(exc.value.details) == {"error": "ACTIVE_LIMIT", "active_count": 1, "limit": 1}

    # Re-activating the held link changes no count, so it is exempt.
    again = _activate(service_client, user_id=uid, target_id=first["target"]["id"], active_limit=1)
    assert again["is_active"] is True
    assert _active_count(service_client, uid) == 1


def test_activate_user_target_writes_fit_fields_only_when_supplied(
    service_client: Client, two_seeded_users: tuple[str, str], cleanup_targets: list[str]
) -> None:
    uid, _ = two_seeded_users
    made = _call(service_client, user_id=uid, label=f"Fit {uuid.uuid4()}")
    cleanup_targets.append(made["target"]["id"])
    tid = made["target"]["id"]

    scored = _activate(
        service_client,
        user_id=uid,
        target_id=tid,
        active_limit=5,
        fit_score=71,
        fit_score_reasoning="strong overlap",
    )
    assert (scored["fit_score"], scored["fit_score_reasoning"]) == (71, "strong overlap")

    bare = _activate(service_client, user_id=uid, target_id=tid, active_limit=5)
    assert (bare["fit_score"], bare["fit_score_reasoning"]) == (71, "strong overlap")


def test_uncapped_activation_is_allowed_only_with_a_null_limit(
    service_client: Client, two_seeded_users: tuple[str, str], cleanup_targets: list[str]
) -> None:
    """The swap-rollback shape: NULL limit skips the cap (the hard ceiling
    trigger still applies), an integer limit does not."""
    uid, _ = two_seeded_users
    a = _call(service_client, user_id=uid, label=f"Null1 {uuid.uuid4()}")
    b = _call(service_client, user_id=uid, label=f"Null2 {uuid.uuid4()}")
    cleanup_targets += [a["target"]["id"], b["target"]["id"]]
    _activate(service_client, user_id=uid, target_id=a["target"]["id"], active_limit=1)

    restored = _activate(
        service_client, user_id=uid, target_id=b["target"]["id"], active_limit=None
    )
    assert restored["is_active"] is True
    assert _active_count(service_client, uid) == 2


def test_concurrent_active_writes_across_both_paths_admit_exactly_one_at_the_cap(
    service_client: Client, two_seeded_users: tuple[str, str], cleanup_targets: list[str]
) -> None:
    """The cross-path race the review named: one request creates-and-links
    active, another activates an existing follow, on two distinct
    connections, released together, at a cap of one. Under the shared lock
    exactly one commits and the user ends with exactly one active target;
    without it both application prechecks would have seen zero."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from supabase import create_client

    from tests.integration.conftest import LOCAL_URL, SERVICE_KEY

    uid, _ = two_seeded_users
    followed = _call(service_client, user_id=uid, label=f"Race2 {uuid.uuid4()}")
    cleanup_targets.append(followed["target"]["id"])
    label_new = f"Race1 {uuid.uuid4()}"

    c1, c2 = create_client(LOCAL_URL, SERVICE_KEY), create_client(LOCAL_URL, SERVICE_KEY)
    gate = threading.Barrier(2)

    def run(fn: Any) -> tuple[str, Any]:
        gate.wait()
        try:
            return ("ok", fn())
        except APIError as e:
            return ("cap", (e.code, json.loads(e.details)))

    def create_path() -> dict[str, Any]:
        return _call(c1, user_id=uid, label=label_new, is_active=True, active_limit=1)

    def activate_path() -> dict[str, Any]:
        return _activate(c2, user_id=uid, target_id=followed["target"]["id"], active_limit=1)

    with ThreadPoolExecutor(max_workers=2) as pool:
        r1, r2 = pool.submit(run, create_path), pool.submit(run, activate_path)
        create_result, activate_result = r1.result(timeout=30), r2.result(timeout=30)

    new_rows = _rows_for(service_client, label_new.strip().lower())
    for row in new_rows:
        cleanup_targets.append(row["id"])

    outcomes = sorted([create_result[0], activate_result[0]])
    assert outcomes == ["cap", "ok"], (create_result, activate_result)
    loser = create_result if create_result[0] == "cap" else activate_result
    assert loser[1][0] == "PT409"
    assert loser[1][1] == {"error": "ACTIVE_LIMIT", "active_count": 1, "limit": 1}
    assert _active_count(service_client, uid) == 1
    # If the create path lost, its target insert was rolled back with it.
    if create_result[0] == "cap":
        assert new_rows == []
