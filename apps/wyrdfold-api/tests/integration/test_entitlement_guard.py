"""SEC-H1 (audit 2026-07-18): user_profiles entitlement columns must be
immutable to a user hitting PostgREST directly (browser anon key), while the
service-role client (billing/admin) can still set them, and genuine prefs still
work. Also: sms_daily_limit is DB-bounded 1..50 (Twilio-cost abuse guard).

Requires migration 20260718000000. Runs against the local Supabase stack.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from supabase import Client


def test_entitlement_columns_immutable_to_user(
    service_client: Client,
    user_client_factory: Callable[[str], Client],
    two_seeded_users: tuple[str, str],
) -> None:
    uid, _other = two_seeded_users
    user = user_client_factory(uid)  # the exact per-request client a browser uses

    # 1. Direct escalation attempt via the user's own client — PostgREST accepts
    #    the PATCH (row is theirs, RLS passes) but the trigger pins the columns.
    user.table("user_profiles").update(
        {
            "plan": "pro",
            "llm_monthly_budget_usd": 1_000_000,
            "max_active_targets": 100_000,
            "llm_enabled": True,
        }
    ).eq("user_id", uid).execute()

    row = (
        service_client.table("user_profiles")
        .select("plan, llm_monthly_budget_usd, max_active_targets")
        .eq("user_id", uid)
        .single()
        .execute()
        .data
    )
    assert row["plan"] == "free", "entitlement escalation must be blocked"
    assert row["llm_monthly_budget_usd"] is None
    assert row["max_active_targets"] is None

    # 2. A genuine preference still updates through the same client.
    user.table("user_profiles").update({"list_min_score": 77}).eq(
        "user_id", uid
    ).execute()
    assert (
        service_client.table("user_profiles")
        .select("list_min_score")
        .eq("user_id", uid)
        .single()
        .execute()
        .data["list_min_score"]
        == 77
    )

    # 3. The service-role client (billing webhook / admin) CAN set entitlements.
    service_client.table("user_profiles").update({"plan": "pro"}).eq(
        "user_id", uid
    ).execute()
    assert (
        service_client.table("user_profiles")
        .select("plan")
        .eq("user_id", uid)
        .single()
        .execute()
        .data["plan"]
        == "pro"
    )

    # 4. sms_daily_limit: a valid value works, an abusive one is rejected by the
    #    DB CHECK (the Pydantic 1..50 bound, now enforced past PostgREST too).
    user.table("user_profiles").update({"sms_daily_limit": 30}).eq(
        "user_id", uid
    ).execute()
    with pytest.raises(Exception):
        user.table("user_profiles").update({"sms_daily_limit": 999}).eq(
            "user_id", uid
        ).execute()
