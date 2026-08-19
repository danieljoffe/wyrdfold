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

pytestmark = pytest.mark.integration


def test_entitlement_columns_immutable_to_user(
    service_client: Client,
    user_client_factory: Callable[[str], Client],
    two_seeded_users: tuple[str, str],
) -> None:
    uid, _other = two_seeded_users
    user = user_client_factory(uid)  # the exact per-request client a browser uses

    # The seeded plan is whatever the column DEFAULT is — assert against THAT
    # rather than a literal. This assertion is about the trigger pinning the
    # column, not about which tier new accounts start on; hardcoding 'free'
    # coupled a security test to an unrelated default and broke it when the
    # trial tier landed (#841).
    plan_before = (
        service_client.table("user_profiles")
        .select("plan")
        .eq("user_id", uid)
        .single()
        .execute()
        .data["plan"]
    )

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
    assert row["plan"] == plan_before, "entitlement escalation must be blocked"
    # Belt and braces: name the escalation we actually attempted, so this can
    # never pass by the default coincidentally equalling the attacked value.
    assert row["plan"] != "pro", "user client must not be able to self-upgrade to pro"
    assert row["llm_monthly_budget_usd"] is None
    assert row["max_active_targets"] is None

    # 2. A genuine preference still updates through the same client.
    user.table("user_profiles").update({"list_min_score": 77}).eq("user_id", uid).execute()
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
    service_client.table("user_profiles").update({"plan": "pro"}).eq("user_id", uid).execute()
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
    user.table("user_profiles").update({"sms_daily_limit": 30}).eq("user_id", uid).execute()
    with pytest.raises(Exception):
        user.table("user_profiles").update({"sms_daily_limit": 999}).eq("user_id", uid).execute()


def test_trial_clock_immutable_to_user(
    service_client: Client,
    user_client_factory: Callable[[str], Client],
    two_seeded_users: tuple[str, str],
) -> None:
    """A user cannot extend their own trial (#841 release-gate finding).

    `trial_started_at` decides whether the trial has expired, so it is an
    entitlement column in everything but name. Before it was pinned, a PATCH
    from the user's own browser client moved the clock forward and bought an
    unlimited trial on host keys.
    """
    uid, _other = two_seeded_users
    user = user_client_factory(uid)

    lapsed = "2026-01-01T00:00:00+00:00"
    service_client.table("user_profiles").update({"plan": "trial", "trial_started_at": lapsed}).eq(
        "user_id", uid
    ).execute()

    # The attack: push my own clock forward. PostgREST accepts the PATCH (the
    # row is mine, RLS passes) — the trigger is what must pin the value.
    user.table("user_profiles").update({"trial_started_at": "2099-01-01T00:00:00+00:00"}).eq(
        "user_id", uid
    ).execute()

    after = (
        service_client.table("user_profiles")
        .select("trial_started_at")
        .eq("user_id", uid)
        .single()
        .execute()
        .data["trial_started_at"]
    )
    assert after.startswith("2026-01-01"), "a user must not be able to extend their own trial"


def test_user_client_insert_cannot_mint_a_fresh_trial(
    service_client: Client,
    user_client_factory: Callable[[str], Client],
    two_seeded_users: tuple[str, str],
) -> None:
    """Pinning on UPDATE alone is not enough (#841 release-gate finding).

    RLS lets a user DELETE their own profile row and INSERT a new one. If the
    INSERT branch seeded a fresh clock, churning the row would buy an
    unlimited trial by another route. The INSERT branch must seed an
    ALREADY-EXPIRED clock instead.
    """
    uid, _other = two_seeded_users
    user = user_client_factory(uid)

    user.table("user_profiles").delete().eq("user_id", uid).execute()
    user.table("user_profiles").insert({"user_id": uid}).execute()

    row = (
        service_client.table("user_profiles")
        .select("plan, trial_started_at")
        .eq("user_id", uid)
        .single()
        .execute()
        .data
    )
    # 'epoch' — deliberately parseable and unambiguously past. A NULL or
    # '-infinity' would degrade to "unknown" in `entitlements.parse_trial_stamp`,
    # which fails OPEN and would grant the very trial this blocks.
    assert row["trial_started_at"].startswith("1970-01-01")
    assert row["plan"] == "trial"
