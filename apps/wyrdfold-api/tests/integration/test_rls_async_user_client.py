"""RLS + token-isolation gate for the ASYNC per-request user client (#57 slice 3).

Proves what the mock-based unit suite can't: real Postgres RLS scopes the
JWT-bound *async* client to its own rows, and many concurrent requests through
the shared ``_async_user_httpx`` pool never bleed one user's token onto another
user's request. This is the security invariant every slice-3 router migration
relies on — the async mirror of the sync ``test_user_supabase_client`` /
``test_rls_storage`` guarantees.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest
from supabase import AsyncClient

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_async_user_client_sees_only_own_rows(
    two_seeded_users: tuple[str, str],
    async_user_client_factory: Callable[[str], Awaitable[AsyncClient]],
) -> None:
    uid_a, _uid_b = two_seeded_users
    client_a = await async_user_client_factory(uid_a)
    resp = await client_a.table("user_profiles").select("user_id").execute()
    seen = {row["user_id"] for row in resp.data}
    # RLS scopes to A: A's own row is visible, B's is not.
    assert seen == {uid_a}


@pytest.mark.asyncio
async def test_async_user_client_no_token_bleed_under_concurrency(
    two_seeded_users: tuple[str, str],
    async_user_client_factory: Callable[[str], Awaitable[AsyncClient]],
) -> None:
    uid_a, uid_b = two_seeded_users

    async def read_as(user_id: str) -> set[str]:
        # A fresh per-request client over the SHARED async pool — exactly the
        # production shape. If the pool bled a token, this request would see the
        # other user's row.
        client = await async_user_client_factory(user_id)
        resp = await client.table("user_profiles").select("user_id").execute()
        return {row["user_id"] for row in resp.data}

    uids = [uid_a if i % 2 == 0 else uid_b for i in range(40)]
    results = await asyncio.gather(*[read_as(uid) for uid in uids])

    mismatches = [
        (uid, seen) for uid, seen in zip(uids, results, strict=True) if seen != {uid}
    ]
    assert mismatches == [], f"token bleed / RLS leak: {mismatches[:5]}"
