"""Live-stack gate for the #57 PR-G2b ASYNC #191 RPC twins.

The interactive targets router (add_reference_jd, vote re-merge, from_input's
URL corpus builder) now writes the SHARED scoring profile through
``apply_profile_patch_rpc_async`` / ``apply_profile_merge_rpc_async`` on the
pooled async service client. The sync twins' full contract (every outcome +
service_role-only EXECUTE) is proven in ``test_profile_patch_rpc.py``; these
prove the genuinely-new ASYNC leg — ``await client.rpc(...).execute()`` — drives
the same SECURITY DEFINER functions correctly against real Postgres: the in-DB
follower re-check and the optimistic version guard still bite, and the merge
carries its search_keywords.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from supabase import AsyncClient, Client

from app.services.targets.profile_writes import (
    apply_profile_merge_rpc_async,
    apply_profile_patch_rpc_async,
)

pytestmark = pytest.mark.integration

_PROFILE = {
    "categories": {"core_skills": {"keywords": {"python": 3}, "weight": 2.0}},
    "negative": {"keywords": [], "weight": -10.0},
}
_NEXT = {
    "categories": {"core_skills": {"keywords": {"python": 3}, "weight": 2.0}},
    "negative": {"keywords": ["sales"], "weight": -10.0},
}


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
                "label": f"async RPC gate {tag}",
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
        service_client.table("user_targets").delete().eq("target_id", target_id).execute()
        service_client.table("targets").delete().eq("id", target_id).execute()


def _state(client: Client, target_id: str) -> dict[str, Any]:
    return (
        client.table("targets")
        .select("scoring_profile,profile_version,search_keywords")
        .eq("id", target_id)
        .single()
        .execute()
        .data
    )


@pytest.mark.asyncio
async def test_patch_async_follower_applies_and_guards_bite(
    service_client: Client,
    async_service_client: AsyncClient,
    seeded_target: tuple[str, str, str],
) -> None:
    uid_a, uid_b, target_id = seeded_target

    # Non-follower B is refused by the in-DB re-check even on the async
    # service-role client — the Python-authz-bug backstop. Profile untouched.
    outcome, version = await apply_profile_patch_rpc_async(
        async_service_client,
        user_id=uid_b,
        target_id=target_id,
        next_profile=_NEXT,
        expected_version=1,
    )
    assert (outcome, version) == ("not_a_follower", 1)
    assert _state(service_client, target_id)["profile_version"] == 1

    # Follower A applies → version bumps, profile written atomically.
    outcome, version = await apply_profile_patch_rpc_async(
        async_service_client,
        user_id=uid_a,
        target_id=target_id,
        next_profile=_NEXT,
        expected_version=1,
    )
    assert (outcome, version) == ("applied", 2)
    state = _state(service_client, target_id)
    assert state["profile_version"] == 2
    assert state["scoring_profile"]["negative"]["keywords"] == ["sales"]

    # A stale expected_version can't clobber the now-v2 profile (lost-update guard).
    outcome, _ = await apply_profile_patch_rpc_async(
        async_service_client,
        user_id=uid_a,
        target_id=target_id,
        next_profile={"negative": {"keywords": ["everything"], "weight": -10.0}},
        expected_version=1,
    )
    assert outcome == "version_conflict"
    assert _state(service_client, target_id)["scoring_profile"]["negative"]["keywords"] == ["sales"]


@pytest.mark.asyncio
async def test_merge_async_writes_profile_and_search_keywords(
    service_client: Client,
    async_service_client: AsyncClient,
    seeded_target: tuple[str, str, str],
) -> None:
    uid_a, _, target_id = seeded_target

    outcome, version = await apply_profile_merge_rpc_async(
        async_service_client,
        user_id=uid_a,
        target_id=target_id,
        next_profile=_NEXT,
        expected_version=1,
        search_keywords=["golang engineer"],
    )
    assert (outcome, version) == ("applied", 2)
    state = _state(service_client, target_id)
    assert state["profile_version"] == 2
    assert state["scoring_profile"]["negative"]["keywords"] == ["sales"]
    # The merge RPC also carries the derived search_keywords (COALESCE-written).
    assert state["search_keywords"] == ["golang engineer"]
