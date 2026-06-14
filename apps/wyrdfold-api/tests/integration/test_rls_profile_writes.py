"""RLS gate for #79 Phase 3 — profile writes.

The proof these give that the mock suite cannot: an UPDATE issued through
the JWT-bound user client is constrained by Postgres RLS (`auth.uid() =
user_id`, command `ALL`). A cross-tenant UPDATE matches zero rows even
when it explicitly targets the victim's `user_id`; an UPDATE of the
caller's OWN row succeeds and persists. If someone wired the
service-role client back into a write path, the cross-tenant case would
start mutating another user's row and these fail.

`service_client` (bypasses RLS) is used only to verify the resulting
state, so the assertions reflect what is actually on disk rather than
what the user client merely reported.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from supabase import Client

pytestmark = pytest.mark.integration


def test_cross_tenant_update_affects_zero_rows(
    two_seeded_users: tuple[str, str],
    user_client_factory: Callable[[str], Client],
    service_client: Client,
) -> None:
    uid_a, uid_b = two_seeded_users
    client_a = user_client_factory(uid_a)

    # User A tries to overwrite user B's row. RLS must match zero rows.
    resp = (
        client_a.table("user_profiles")
        .update({"name": "hacked"})
        .eq("user_id", uid_b)
        .execute()
    )
    assert resp.data == [], "RLS leak: user A's UPDATE matched user B's row"

    # Verify via service-role that B's row is untouched.
    rows = (
        service_client.table("user_profiles")
        .select("name")
        .eq("user_id", uid_b)
        .execute()
        .data
    )
    assert rows and rows[0]["name"] == "User B", (
        "RLS leak: user B's profile was mutated by user A"
    )


def test_own_row_update_succeeds_and_persists(
    two_seeded_users: tuple[str, str],
    user_client_factory: Callable[[str], Client],
    service_client: Client,
) -> None:
    uid_a, _uid_b = two_seeded_users
    client_a = user_client_factory(uid_a)

    resp = (
        client_a.table("user_profiles")
        .update({"name": "User A renamed"})
        .eq("user_id", uid_a)
        .execute()
    )
    assert resp.data, "user A's UPDATE of its own row returned no rows"

    rows = (
        service_client.table("user_profiles")
        .select("name")
        .eq("user_id", uid_a)
        .execute()
        .data
    )
    assert rows and rows[0]["name"] == "User A renamed", (
        "user A's own-row UPDATE did not persist"
    )
