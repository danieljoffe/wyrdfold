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
from collections.abc import Callable, Iterator
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


def test_a_null_limit_is_refused_so_no_uncapped_activation_exists(
    service_client: Client, two_seeded_users: tuple[str, str], cleanup_targets: list[str]
) -> None:
    uid, _ = two_seeded_users
    a = _call(service_client, user_id=uid, label=f"Null {uuid.uuid4()}")
    cleanup_targets.append(a["target"]["id"])

    with pytest.raises(APIError) as exc:
        _activate(service_client, user_id=uid, target_id=a["target"]["id"], active_limit=None)

    assert exc.value.code == "PT400"
    assert json.loads(exc.value.details) == {"error": "LIMIT_REQUIRED"}
    assert _active_count(service_client, uid) == 0


def _swap(
    client: Client, *, user_id: str, swap_out: str, target_id: str, active_limit: int | None
) -> dict[str, Any]:
    return (
        client.rpc(
            "swap_user_target_active",
            {
                "p_user_id": user_id,
                "p_swap_out": swap_out,
                "p_target_id": target_id,
                "p_active_limit": active_limit,
            },
        )
        .execute()
        .data
    )


def test_swap_is_one_transaction_and_a_rejection_unwinds_the_deactivation(
    service_client: Client, two_seeded_users: tuple[str, str], cleanup_targets: list[str]
) -> None:
    """The race the review named: a swap used to be deactivate, activate,
    restore. Now a rejected swap leaves the swapped-out target exactly as
    active as it was, in the same transaction."""
    uid, _ = two_seeded_users
    a = _call(service_client, user_id=uid, label=f"SwapA {uuid.uuid4()}")
    x = _call(service_client, user_id=uid, label=f"SwapX {uuid.uuid4()}")
    b = _call(service_client, user_id=uid, label=f"SwapB {uuid.uuid4()}")
    cleanup_targets += [a["target"]["id"], x["target"]["id"], b["target"]["id"]]
    a_id, x_id, b_id = a["target"]["id"], x["target"]["id"], b["target"]["id"]
    _activate(service_client, user_id=uid, target_id=a_id, active_limit=2)
    _activate(service_client, user_id=uid, target_id=x_id, active_limit=2)

    # Swap A for B at a cap of 2: A out, B in, still 2 active.
    swapped = _swap(service_client, user_id=uid, swap_out=a_id, target_id=b_id, active_limit=2)
    assert swapped["target_id"] == b_id and swapped["is_active"] is True
    active = {
        r["target_id"]
        for r in service_client.table("user_targets")
        .select("target_id")
        .eq("user_id", uid)
        .eq("is_active", True)
        .execute()
        .data
    }
    assert active == {x_id, b_id}

    # Swap B back for A at a cap of 1: after deactivating B, X still holds
    # the only slot, so the activation is refused and B stays ACTIVE.
    with pytest.raises(APIError) as exc:
        _swap(service_client, user_id=uid, swap_out=b_id, target_id=a_id, active_limit=1)
    assert exc.value.code == "PT409"
    assert json.loads(exc.value.details)["active_count"] == 1
    active_after = {
        r["target_id"]
        for r in service_client.table("user_targets")
        .select("target_id")
        .eq("user_id", uid)
        .eq("is_active", True)
        .execute()
        .data
    }
    assert active_after == {x_id, b_id}


def test_swap_input_checks_are_400s_that_write_nothing(
    service_client: Client, two_seeded_users: tuple[str, str], cleanup_targets: list[str]
) -> None:
    uid, _ = two_seeded_users
    a = _call(service_client, user_id=uid, label=f"Bad {uuid.uuid4()}")
    b = _call(service_client, user_id=uid, label=f"Bad2 {uuid.uuid4()}")
    cleanup_targets += [a["target"]["id"], b["target"]["id"]]

    with pytest.raises(APIError) as self_swap:
        _swap(
            service_client,
            user_id=uid,
            swap_out=a["target"]["id"],
            target_id=a["target"]["id"],
            active_limit=5,
        )
    assert (self_swap.value.code, json.loads(self_swap.value.details)) == (
        "PT400",
        {"error": "SWAP_SELF"},
    )

    # A is not active, so it cannot be swapped out.
    with pytest.raises(APIError) as not_active:
        _swap(
            service_client,
            user_id=uid,
            swap_out=a["target"]["id"],
            target_id=b["target"]["id"],
            active_limit=5,
        )
    assert json.loads(not_active.value.details) == {"error": "SWAP_NOT_ACTIVE"}
    assert _active_count(service_client, uid) == 0


def test_concurrent_swap_and_activation_never_exceed_the_cap(
    service_client: Client, two_seeded_users: tuple[str, str], cleanup_targets: list[str]
) -> None:
    """The interleaving from the review: A active at cap 1; one connection
    swaps A for B while another activates C. Whatever the order, the user
    ends with exactly one active target, because the swap's deactivation and
    activation are one critical section and nothing runs uncapped."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from supabase import create_client

    from tests.integration.conftest import LOCAL_URL, SERVICE_KEY

    uid, _ = two_seeded_users
    c1, c2 = create_client(LOCAL_URL, SERVICE_KEY), create_client(LOCAL_URL, SERVICE_KEY)

    def one_round() -> tuple[tuple[str, Any], tuple[str, Any]]:
        a = _call(service_client, user_id=uid, label=f"RaceA {uuid.uuid4()}")
        b = _call(service_client, user_id=uid, label=f"RaceB {uuid.uuid4()}")
        c = _call(service_client, user_id=uid, label=f"RaceC {uuid.uuid4()}")
        cleanup_targets.extend([a["target"]["id"], b["target"]["id"], c["target"]["id"]])
        a_id, b_id, c_id = a["target"]["id"], b["target"]["id"], c["target"]["id"]
        _activate(service_client, user_id=uid, target_id=a_id, active_limit=1)
        gate = threading.Barrier(2)

        def run(fn: Any) -> tuple[str, Any]:
            gate.wait()
            try:
                return ("ok", fn())
            except APIError as e:
                return ("cap", e.code)

        def swap_path() -> dict[str, Any]:
            return _swap(c1, user_id=uid, swap_out=a_id, target_id=b_id, active_limit=1)

        def activate_path() -> dict[str, Any]:
            return _activate(c2, user_id=uid, target_id=c_id, active_limit=1)

        with ThreadPoolExecutor(max_workers=2) as pool:
            r1, r2 = pool.submit(run, swap_path), pool.submit(run, activate_path)
            return r1.result(timeout=30), r2.result(timeout=30)

    for _ in range(3):
        swap_result, activate_result = one_round()
        assert _active_count(service_client, uid) == 1, (swap_result, activate_result)
        assert "cap" in {swap_result[0], activate_result[0]}
        # reset for the next round
        service_client.table("user_targets").update({"is_active": False}).eq(
            "user_id", uid
        ).execute()


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


def test_active_create_without_a_limit_is_refused_before_any_write(
    service_client: Client, two_seeded_users: tuple[str, str]
) -> None:
    """#1084 review: p_is_active with a NULL limit used to create the target
    and activate the membership with no cap check. Now it is refused before
    the first insert: no target row, no membership."""
    uid, _ = two_seeded_users
    label = f"NullLimit {uuid.uuid4()}"

    with pytest.raises(APIError) as exc:
        _call(service_client, user_id=uid, label=label, is_active=True, active_limit=None)

    assert exc.value.code == "PT400"
    assert json.loads(exc.value.details) == {"error": "LIMIT_REQUIRED"}
    assert _rows_for(service_client, label.strip().lower()) == []
    assert _active_count(service_client, uid) == 0
    assert service_client.table("user_targets").select("id").eq("user_id", uid).execute().data == []


# --- #1083: only the API's service role may call the membership functions ---


_MEMBERSHIP_FUNCTIONS = (
    "create_target_and_link",
    "activate_user_target",
    "swap_user_target_active",
)


def _membership_call(
    client: Client, fn_name: str, *, user_id: str, held_id: str, other_id: str, label: str
) -> dict[str, Any]:
    """One call per function, shaped so that a caller who IS let in would grow
    the user's active set past a cap of one: activate the second target, swap
    the held one for it, or create-and-activate a new one, each at a cap of 999
    (the number the release-gate check used on staging)."""
    if fn_name == "create_target_and_link":
        return _call(client, user_id=user_id, label=label, is_active=True, active_limit=999)
    if fn_name == "activate_user_target":
        return _activate(client, user_id=user_id, target_id=other_id, active_limit=999)
    return _swap(client, user_id=user_id, swap_out=held_id, target_id=other_id, active_limit=999)


def _memberships(client: Client, user_id: str) -> set[tuple[str, bool]]:
    rows = (
        client.table("user_targets")
        .select("target_id, is_active")
        .eq("user_id", user_id)
        .execute()
        .data
    )
    return {(r["target_id"], r["is_active"]) for r in rows}


@pytest.mark.parametrize("caller", ["own_jwt", "anon"])
@pytest.mark.parametrize("fn_name", _MEMBERSHIP_FUNCTIONS)
def test_only_the_service_role_may_execute_the_membership_functions(
    service_client: Client,
    two_seeded_users: tuple[str, str],
    cleanup_targets: list[str],
    user_client_factory: Callable[[str], Client],
    anon_client: Client,
    fn_name: str,
    caller: str,
) -> None:
    """#1083: the plan cap is a number the API passes in, so a user calling
    these functions with their own login token could pass any cap and hold
    more active targets than their plan allows. EXECUTE is now revoked from
    anon and authenticated; only the service role may call them.

    The refusal must be the FUNCTION privilege (42501 "permission denied for
    function ..."), not row-level security on a table further in: before the
    revoke, create_target_and_link was already refused, but by the targets
    insert policy, while activate_user_target went straight through."""
    uid, _ = two_seeded_users
    held = _call(service_client, user_id=uid, label=f"Held {uuid.uuid4()}")
    other = _call(service_client, user_id=uid, label=f"Other {uuid.uuid4()}")
    held_id, other_id = held["target"]["id"], other["target"]["id"]
    cleanup_targets += [held_id, other_id]
    _activate(service_client, user_id=uid, target_id=held_id, active_limit=1)
    before = _memberships(service_client, uid)
    assert before == {(held_id, True), (other_id, False)}
    label = f"Bypass {uuid.uuid4()}"

    client = user_client_factory(uid) if caller == "own_jwt" else anon_client
    with pytest.raises(APIError) as exc:
        _membership_call(
            client, fn_name, user_id=uid, held_id=held_id, other_id=other_id, label=label
        )

    assert exc.value.code == "42501"
    assert f"permission denied for function {fn_name}" in exc.value.message
    # Nothing was written: the active set is untouched and no target was minted.
    assert _memberships(service_client, uid) == before
    assert _rows_for(service_client, label.strip().lower()) == []


def test_the_service_role_still_executes_every_membership_function(
    service_client: Client, two_seeded_users: tuple[str, str], cleanup_targets: list[str]
) -> None:
    """The revoke must not reach the API's own role: the same three calls, made
    as the service role, keep working. swap calls activate internally, so this
    also proves the inner call survives under SECURITY INVOKER."""
    uid, _ = two_seeded_users
    held = _call(service_client, user_id=uid, label=f"SvcHeld {uuid.uuid4()}")
    other = _call(service_client, user_id=uid, label=f"SvcOther {uuid.uuid4()}")
    held_id, other_id = held["target"]["id"], other["target"]["id"]
    cleanup_targets += [held_id, other_id]

    activated = _activate(service_client, user_id=uid, target_id=held_id, active_limit=1)
    assert activated["is_active"] is True
    swapped = _swap(
        service_client, user_id=uid, swap_out=held_id, target_id=other_id, active_limit=1
    )
    assert swapped["target_id"] == other_id and swapped["is_active"] is True
    label = f"SvcNew {uuid.uuid4()}"
    created = _call(service_client, user_id=uid, label=label, is_active=True, active_limit=2)
    cleanup_targets.append(created["target"]["id"])
    assert created["was_created"] is True and created["user_target"]["is_active"] is True
    assert _memberships(service_client, uid) == {
        (held_id, False),
        (other_id, True),
        (created["target"]["id"], True),
    }
