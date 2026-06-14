"""RLS gate for #79 Phase 2 — profile reads.

The proof these give that the mock suite cannot: a SELECT issued through
the JWT-bound user client with NO `.eq("user_id", ...)` filter still
returns only the caller's rows, because Postgres RLS (`auth.uid() =
user_id`) is the control. If a refactor drops the Python filter these
stay green; if someone disables the policy or wires the service-role
client back into a read path, they fail.

The `test_service_role_sees_both_users` contrast matters: it proves the
seeded rows are visible at all, so the empty user-client results below
are RLS doing its job — not just absent data.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from supabase import Client

pytestmark = pytest.mark.integration


def test_service_role_sees_both_users(
    two_seeded_users: tuple[str, str],
    service_client: Client,
) -> None:
    uid_a, uid_b = two_seeded_users
    rows = (
        service_client.table("user_profiles")
        .select("user_id")
        .in_("user_id", [uid_a, uid_b])
        .execute()
        .data
    )
    assert {uid_a, uid_b} <= {r["user_id"] for r in rows}


def test_user_profiles_select_is_rls_scoped_without_python_filter(
    two_seeded_users: tuple[str, str],
    user_client_factory: Callable[[str], Client],
) -> None:
    uid_a, uid_b = two_seeded_users
    client_a = user_client_factory(uid_a)

    # No .eq("user_id", ...) — RLS alone must scope this.
    rows = client_a.table("user_profiles").select("user_id, name").execute().data

    seen = {r["user_id"] for r in rows}
    assert uid_a in seen
    assert uid_b not in seen, "RLS leak: user A sees user B's profile row"


def test_user_profiles_cross_read_returns_empty(
    two_seeded_users: tuple[str, str],
    user_client_factory: Callable[[str], Client],
) -> None:
    uid_a, uid_b = two_seeded_users
    client_a = user_client_factory(uid_a)

    # Even explicitly targeting B's row, RLS yields nothing.
    rows = (
        client_a.table("user_profiles")
        .select("user_id")
        .eq("user_id", uid_b)
        .execute()
        .data
    )
    assert rows == []


def test_llm_costs_select_is_rls_scoped(
    two_seeded_users: tuple[str, str],
    user_client_factory: Callable[[str], Client],
) -> None:
    uid_a, uid_b = two_seeded_users
    client_b = user_client_factory(uid_b)

    rows = client_b.table("llm_costs").select("user_id, cost_usd").execute().data

    seen = {r["user_id"] for r in rows}
    assert uid_b in seen
    assert uid_a not in seen, "RLS leak: user B sees user A's llm_costs"
