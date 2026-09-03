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


# ---- deny-by-default column classification (#873) ---------------------------
#
# #872 was a privilege escalation introduced by ADDING A COLUMN: `trial_started_at`
# decided whether a trial had expired, but nothing pinned it, so a user could PATCH
# their own clock forward. Unit tests, the migration and CI were all green.
#
# A checklist would not have caught that — the release skill's abuse step was
# available all session and the column still shipped unpinned. What was missing is
# something that FAILS when a new server-trusted column appears. Hence these two.

#: Columns a user may write about themselves. Nothing here is read for
#: authorization, entitlement or billing.
USER_EDITABLE = {
    "email",
    "name",
    "location",
    "phone_number",
    "linkedin_url",
    "website_url",
    "list_min_score",
    "job_score_threshold",
    "job_notifications_enabled",
    "sms_notifications_enabled",
    "sms_score_threshold",
    "sms_daily_limit",
    "resume_style_settings",
    # #866: /jobs filter snapshots — user-owned UI state written via
    # PUT /profile/jobs-filters on the user's own RLS row; the backend
    # never reads it for authorization, entitlement or billing.
    "jobs_filter_prefs",
    "onboarding_path",
    "onboarding_current_step",
    "onboarding_completed_at",
    "onboarding_deferred_at",
    "unsubscribed_at",
    "last_seen_at",
}

#: Columns the BACKEND trusts. Every one of these must be pinned by
#: `protect_user_profiles_entitlements` against a user-client write.
SERVER_TRUSTED = {
    "plan",
    "trial_started_at",
    "llm_enabled",
    "llm_monthly_budget_usd",
    "max_active_targets",
    "stripe_customer_id",
}

#: Identity / bookkeeping the user never sets directly.
SYSTEM = {"id", "user_id", "created_at", "updated_at"}

#: A value that must NOT stick, per trusted column. Chosen to be both
#: type-valid and an obvious escalation, so a passing test means the trigger
#: rejected a plausible attack rather than a malformed one.
_ESCALATION: dict[str, object] = {
    "plan": "pro",
    "trial_started_at": "2099-01-01T00:00:00+00:00",
    "llm_monthly_budget_usd": 1_000_000,
    "max_active_targets": 100_000,
    "stripe_customer_id": "cus_attacker_controlled",
}

#: Booleans get their attack derived from the CURRENT value — a static one can
#: coincide with what is already there, and "unchanged" would then prove
#: nothing. It did: `llm_enabled` seeds True, so a literal True passed while
#: testing nothing.
_FLIP_BOOL = {"llm_enabled"}


def _attack_value(column: str, current: object) -> object:
    if column in _FLIP_BOOL:
        return not bool(current)
    return _ESCALATION[column]


def test_every_user_profiles_column_is_classified(
    service_client: Client, two_seeded_users: tuple[str, str]
) -> None:
    """A NEW column belongs to no set, so this fails the moment one lands.

    That is the whole point: the cost of classifying falls on whoever adds the
    column, which is the only moment the question ("does the backend trust
    this?") can be answered cheaply. Classify it as server-trusted and the
    sibling test then fails until it is pinned.
    """
    uid, _other = two_seeded_users
    row = (
        service_client.table("user_profiles").select("*").eq("user_id", uid).single().execute().data
    )
    live = set(row.keys())
    classified = USER_EDITABLE | SERVER_TRUSTED | SYSTEM

    assert live - classified == set(), (
        "unclassified user_profiles column(s). If the backend reads it for "
        "authorization, entitlement or billing, add it to SERVER_TRUSTED *and* "
        "pin it in protect_user_profiles_entitlements (#873)."
    )
    assert classified - live == set(), (
        "classified column(s) no longer exist on user_profiles — drop them from "
        "the sets above so this stays honest."
    )


def test_every_server_trusted_column_is_pinned(
    service_client: Client,
    user_client_factory: Callable[[str], Client],
    two_seeded_users: tuple[str, str],
) -> None:
    """Each SERVER_TRUSTED column must survive a user-client escalation attempt."""
    uid, _other = two_seeded_users
    user = user_client_factory(uid)

    before = (
        service_client.table("user_profiles")
        .select(",".join(sorted(SERVER_TRUSTED)))
        .eq("user_id", uid)
        .single()
        .execute()
        .data
    )

    for column in sorted(SERVER_TRUSTED):
        attack = _attack_value(column, before[column])
        # Precondition: the attack must actually differ from the current value,
        # or "unchanged" would prove nothing.
        assert before[column] != attack, (
            f"{column}: escalation value equals the seeded value — pick another"
        )
        # PostgREST accepts the PATCH (the row is theirs, RLS passes); the
        # trigger is what must discard the value.
        user.table("user_profiles").update({column: attack}).eq("user_id", uid).execute()

    after = (
        service_client.table("user_profiles")
        .select(",".join(sorted(SERVER_TRUSTED)))
        .eq("user_id", uid)
        .single()
        .execute()
        .data
    )
    assert after == before, "a user client changed a server-trusted column"
