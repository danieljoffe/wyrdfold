"""Live-stack gate for the #191 `apply_target_profile_patch` RPC.

The learner's shared-profile writes go through this SECURITY DEFINER
function; these tests prove the in-DB guards actually bite against a real
Postgres: a non-follower is refused, a stale `profile_version` is refused,
the happy path applies atomically, and the function is not executable by
anon/authenticated (service_role only) — so no client-side caller can reach
it directly even if a Python authz check is bypassed.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from supabase import Client

pytestmark = pytest.mark.integration

_PROFILE = {
    "categories": {"core_skills": {"keywords": {"python": 3}, "weight": 2.0}},
    "negative": {"keywords": [], "weight": -10.0},
}
_NEXT_PROFILE = {
    "categories": {"core_skills": {"keywords": {"python": 3}, "weight": 2.0}},
    "negative": {"keywords": ["sales"], "weight": -10.0},
}


def _rpc(
    client: Client,
    *,
    user_id: str,
    target_id: str,
    next_profile: dict[str, Any] | None = None,
    expected_version: int = 1,
) -> list[dict[str, Any]]:
    return (
        client.rpc(
            "apply_target_profile_patch",
            {
                "p_user_id": user_id,
                "p_target_id": target_id,
                "p_next_profile": next_profile or _NEXT_PROFILE,
                "p_expected_version": expected_version,
            },
        )
        .execute()
        .data
    )


def _target_state(client: Client, target_id: str) -> dict[str, Any]:
    return (
        client.table("targets")
        .select("scoring_profile,profile_version")
        .eq("id", target_id)
        .single()
        .execute()
        .data
    )


@pytest.fixture
def seeded_target(
    service_client: Client, two_seeded_users: tuple[str, str]
) -> Iterator[tuple[str, str, str]]:
    """A target followed by user A only. Yields (uid_a, uid_b, target_id)."""
    uid_a, uid_b = two_seeded_users
    tag = uuid.uuid4().hex[:8]
    target_id: str = (
        service_client.table("targets")
        .insert(
            {
                "label": f"RPC gate {tag}",
                "scoring_profile": _PROFILE,
                "search_keywords": ["python engineer"],
                "profile_version": 1,
            }
        )
        .execute()
        .data[0]["id"]
    )
    service_client.table("user_targets").insert(
        {"user_id": uid_a, "target_id": target_id}
    ).execute()
    try:
        yield uid_a, uid_b, target_id
    finally:
        service_client.table("user_targets").delete().eq(
            "target_id", target_id
        ).execute()
        service_client.table("targets").delete().eq("id", target_id).execute()


def test_follower_apply_updates_profile_and_bumps_version(
    service_client: Client, seeded_target: tuple[str, str, str]
) -> None:
    uid_a, _, target_id = seeded_target

    rows = _rpc(
        service_client, user_id=uid_a, target_id=target_id, expected_version=1
    )

    assert rows == [{"outcome": "applied", "new_version": 2}]
    state = _target_state(service_client, target_id)
    assert state["profile_version"] == 2
    assert state["scoring_profile"]["negative"]["keywords"] == ["sales"]


def test_non_follower_is_refused_and_profile_untouched(
    service_client: Client, seeded_target: tuple[str, str, str]
) -> None:
    """User B never linked this target — the in-DB re-check refuses the
    write even on the service-role client (the Python-bug backstop)."""
    _, uid_b, target_id = seeded_target

    rows = _rpc(
        service_client, user_id=uid_b, target_id=target_id, expected_version=1
    )

    assert rows == [{"outcome": "not_a_follower", "new_version": 1}]
    state = _target_state(service_client, target_id)
    assert state["profile_version"] == 1
    assert state["scoring_profile"]["negative"]["keywords"] == []


def test_stale_expected_version_is_refused(
    service_client: Client, seeded_target: tuple[str, str, str]
) -> None:
    """A patch computed against v1 must not clobber a profile that has
    since moved to v2 — the lost-update guard."""
    uid_a, _, target_id = seeded_target

    first = _rpc(
        service_client, user_id=uid_a, target_id=target_id, expected_version=1
    )
    assert first[0]["outcome"] == "applied"

    poisoned = {"negative": {"keywords": ["everything"], "weight": -10.0}}
    stale = _rpc(
        service_client,
        user_id=uid_a,
        target_id=target_id,
        next_profile=poisoned,
        expected_version=1,  # stale: the profile is at v2 now
    )

    assert stale == [{"outcome": "version_conflict", "new_version": 2}]
    state = _target_state(service_client, target_id)
    assert state["profile_version"] == 2
    assert state["scoring_profile"]["negative"]["keywords"] == ["sales"]


def test_unknown_target_reports_not_found(service_client: Client) -> None:
    rows = _rpc(
        service_client,
        user_id=str(uuid.uuid4()),
        target_id=str(uuid.uuid4()),
        expected_version=1,
    )
    assert rows[0]["outcome"] == "target_not_found"


def test_anon_and_authenticated_cannot_execute(
    service_client: Client,
    anon_client: Client,
    user_client_factory: Any,
    seeded_target: tuple[str, str, str],
) -> None:
    """EXECUTE is service_role-only (REVOKE FROM PUBLIC/anon/authenticated)
    — a browser client can never reach the shared-profile write directly,
    even for a target its user legitimately follows."""
    uid_a, _, target_id = seeded_target

    with pytest.raises(Exception) as anon_err:
        _rpc(anon_client, user_id=uid_a, target_id=target_id)
    assert "42501" in str(anon_err.value) or "permission denied" in str(
        anon_err.value
    )

    user_client = user_client_factory(uid_a)
    with pytest.raises(Exception) as user_err:
        _rpc(user_client, user_id=uid_a, target_id=target_id)
    assert "42501" in str(user_err.value) or "permission denied" in str(
        user_err.value
    )

    # Neither refused call touched the shared profile.
    state = _target_state(service_client, target_id)
    assert state["profile_version"] == 1
    assert state["scoring_profile"]["negative"]["keywords"] == []
