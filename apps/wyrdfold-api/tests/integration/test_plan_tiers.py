"""Live-stack gate for Phase 3 slice 2 — plan column + billable-spend RPC.

Proves against a real Postgres: the `plan` CHECK constraint bites, and
`total_billable_spend_since` sums exactly the non-excluded purposes with
the same own-spend guard as `total_spend_since` (a JWT caller can only
see their own numbers).
"""

from __future__ import annotations

from typing import Any

import pytest
from supabase import Client

pytestmark = pytest.mark.integration


def _seed_costs(service_client: Client, user_id: str) -> None:
    service_client.table("llm_costs").insert(
        [
            # Billable (interactive).
            {"user_id": user_id, "model": "m", "purpose": "job_analysis",
             "input_tokens": 1, "output_tokens": 1, "cost_usd": 1.25},
            {"user_id": user_id, "model": "m", "purpose": "tailor.resume",
             "input_tokens": 1, "output_tokens": 1, "cost_usd": 0.50},
            # Background (excluded from managed quotas).
            {"user_id": user_id, "model": "m", "purpose": "fit.job",
             "input_tokens": 1, "output_tokens": 1, "cost_usd": 3.00},
            {"user_id": user_id, "model": "m",
             "purpose": "relevance.title_triage",
             "input_tokens": 1, "output_tokens": 1, "cost_usd": 2.00},
        ]
    ).execute()


@pytest.fixture
def seeded_costs(
    service_client: Client, two_seeded_users: tuple[str, str]
) -> Any:
    uid_a, uid_b = two_seeded_users
    _seed_costs(service_client, uid_a)
    try:
        yield uid_a, uid_b
    finally:
        service_client.table("llm_costs").delete().eq("user_id", uid_a).execute()


_EXCLUDED = ["fit.job", "relevance.title_triage", "poll_scoring"]


def test_billable_rpc_excludes_background_purposes(
    service_client: Client, seeded_costs: tuple[str, str]
) -> None:
    # two_seeded_users itself seeds one purpose='test' row at $1.00 for
    # user A — billable by blocklist semantics (unknown purposes default
    # to billable, the safe-for-cost direction).
    uid_a, _ = seeded_costs
    total = service_client.rpc(
        "total_billable_spend_since",
        {"p_user_id": uid_a, "p_since": None, "p_excluded_purposes": _EXCLUDED},
    ).execute().data
    assert float(total) == pytest.approx(2.75)  # 1.25 + 0.50 + 1.00, not 7.75

    # NULL exclusion list = everything (parity with total_spend_since).
    total_all = service_client.rpc(
        "total_billable_spend_since",
        {"p_user_id": uid_a, "p_since": None, "p_excluded_purposes": None},
    ).execute().data
    assert float(total_all) == pytest.approx(7.75)


def test_billable_rpc_guard_hides_other_users_spend(
    user_client_factory: Any, seeded_costs: tuple[str, str]
) -> None:
    """Negative: a JWT caller asking about someone ELSE's spend gets 0 —
    the auth.uid() guard filters every row (same posture as
    total_spend_since)."""
    uid_a, uid_b = seeded_costs
    client_b = user_client_factory(uid_b)
    total = client_b.rpc(
        "total_billable_spend_since",
        {"p_user_id": uid_a, "p_since": None, "p_excluded_purposes": None},
    ).execute().data
    assert float(total) == 0.0

    # …while their own query returns their real spend (the fixture's
    # $2.00 baseline row), proving the guard scopes rather than breaks.
    own = client_b.rpc(
        "total_billable_spend_since",
        {"p_user_id": uid_b, "p_since": None, "p_excluded_purposes": None},
    ).execute().data
    assert float(own) == pytest.approx(2.0)


def test_plan_check_constraint_rejects_unknown_tier(
    service_client: Client, two_seeded_users: tuple[str, str]
) -> None:
    uid_a, _ = two_seeded_users
    with pytest.raises(Exception) as err:
        service_client.table("user_profiles").upsert(
            {"user_id": uid_a, "plan": "gold"}, on_conflict="user_id"
        ).execute()
    assert "user_profiles_plan_check" in str(err.value) or "23514" in str(
        err.value
    )

    # Valid tiers upsert cleanly.
    service_client.table("user_profiles").upsert(
        {"user_id": uid_a, "plan": "starter"}, on_conflict="user_id"
    ).execute()
    row = (
        service_client.table("user_profiles")
        .select("plan")
        .eq("user_id", uid_a)
        .single()
        .execute()
        .data
    )
    assert row["plan"] == "starter"
