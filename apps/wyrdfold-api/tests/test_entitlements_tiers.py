"""Phase 3 slice 2 — plan tiers, quota resolution, and the BYOK gate.

Pins the tier model's behavioral contract:
- entitlements resolution (free/starter/pro; unknown → free);
- ``resolve_llm_quota``: self_host unchanged, saas BYOK-payer bypasses
  the managed quota, managed tiers get interactive-only accounting,
  per-user override always wins;
- ``get_client``: saas free tier without a key is refused (402 path),
  managed tiers and self_host fall back to the instance key;
- ``effective_active_target_cap``: override → tier (saas) → default;
- the monthly budget window uses the billable-only sum when excluded
  purposes are set.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.services import entitlements as ent
from app.services.llm import MissingUserKeyError, TrialExpiredError, budget, get_client
from app.services.targets import crud

_UID = "00000000-0000-0000-0000-000000000042"


def _profile_supabase(rows: list[dict[str, Any]]) -> MagicMock:
    """A supabase fake whose user_profiles select returns ``rows``."""
    sb = MagicMock(name="supabase")
    sb.table.return_value.select.return_value.eq.return_value.execute.return_value.data = rows
    return sb


# ---- entitlements_for -------------------------------------------------------


def test_entitlements_ladder_matches_locked_pricing() -> None:
    free = ent.entitlements_for("free")
    assert (free.llm_key_source, free.monthly_billable_budget_usd) == ("byok", None)
    assert free.max_active_targets == settings.free_max_active_targets

    starter = ent.entitlements_for("starter")
    assert starter.llm_key_source == "host"
    assert starter.monthly_billable_budget_usd == settings.starter_monthly_billable_budget_usd
    assert starter.max_active_targets == settings.starter_max_active_targets

    pro = ent.entitlements_for("pro")
    assert pro.llm_key_source == "host"
    assert pro.monthly_billable_budget_usd == settings.pro_monthly_billable_budget_usd
    assert pro.max_active_targets == settings.pro_max_active_targets


def test_unknown_or_missing_plan_resolves_to_free() -> None:
    """The safe-for-cost posture: an unclassified account never spends
    the host key."""
    for value in (None, "", "enterprise", "PRO"):
        assert ent.entitlements_for(value).llm_key_source == "byok"


# ---- resolve_llm_quota ------------------------------------------------------


def _quota(
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode: str,
    has_key: bool,
    rows: list[dict[str, Any]],
) -> budget.ResolvedQuota:
    from app.services.keys import store as keys_store

    monkeypatch.setattr(settings, "deployment_mode", mode)
    monkeypatch.setattr(keys_store, "has_usable_key", lambda sb, *, user_id, provider: has_key)
    return budget.resolve_llm_quota(_profile_supabase(rows), user_id=_UID)


def test_quota_self_host_is_pre_tier_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """self_host ignores plan AND key presence — settings default or the
    per-user override, counting all purposes."""
    q = _quota(
        monkeypatch,
        mode="self_host",
        has_key=True,  # even with a key: self_host semantics unchanged
        rows=[{"llm_monthly_budget_usd": None, "llm_enabled": True, "plan": "free"}],
    )
    assert q == budget.ResolvedQuota(settings.user_llm_monthly_budget_usd, True, None, "host")

    q = _quota(
        monkeypatch,
        mode="self_host",
        has_key=False,
        rows=[{"llm_monthly_budget_usd": 25.0, "llm_enabled": True, "plan": None}],
    )
    assert q.monthly_cap_usd == 25.0


def test_quota_saas_byok_payer_has_no_managed_monthly_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stored key means the USER pays interactive inference (BYOK wins
    in get_client) — the managed monthly quota is disabled (0), whatever
    the plan says. Hourly/daily rails are the caller's concern."""
    q = _quota(
        monkeypatch,
        mode="saas",
        has_key=True,
        rows=[{"llm_monthly_budget_usd": None, "llm_enabled": True, "plan": "pro"}],
    )
    assert q == budget.ResolvedQuota(0.0, True, None, "user")


def test_quota_saas_managed_tier_gets_interactive_only_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    q = _quota(
        monkeypatch,
        mode="saas",
        has_key=False,
        rows=[{"llm_monthly_budget_usd": None, "llm_enabled": True, "plan": "pro"}],
    )
    assert q.monthly_cap_usd == settings.pro_monthly_billable_budget_usd
    assert q.monthly_excluded_purposes == ent.NON_BILLABLE_PURPOSES
    # The background purposes the quota must NOT count.
    assert "fit.job" in ent.NON_BILLABLE_PURPOSES
    assert "relevance.title_triage" in ent.NON_BILLABLE_PURPOSES


def test_quota_saas_override_beats_tier_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator "add credits" lever still wins over the plan quota."""
    q = _quota(
        monkeypatch,
        mode="saas",
        has_key=False,
        rows=[{"llm_monthly_budget_usd": 50.0, "llm_enabled": True, "plan": "starter"}],
    )
    assert q.monthly_cap_usd == 50.0
    assert q.monthly_excluded_purposes == ent.NON_BILLABLE_PURPOSES


def test_quota_saas_broken_key_row_stays_metered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Copilot on #205: a user_api_keys row that exists but can't be
    decrypted (rotated master key) makes get_client fall back to the
    HOST key — so the quota resolver must treat the user as managed,
    not as a BYOK payer. "Who pays" = key usability, not row existence."""
    from app.services.keys import BYOKDecryptError
    from app.services.keys import store as keys_store

    monkeypatch.setattr(settings, "deployment_mode", "saas")
    monkeypatch.setattr(keys_store.crypto, "is_configured", lambda: True)

    def _broken_get_key(sb: Any, *, user_id: str, provider: str) -> str:
        raise BYOKDecryptError("wrong master key")

    monkeypatch.setattr(keys_store, "get_key", _broken_get_key)
    q = budget.resolve_llm_quota(
        _profile_supabase([{"llm_monthly_budget_usd": None, "llm_enabled": True, "plan": "pro"}]),
        user_id=_UID,
    )
    # Managed accounting applies — NOT the unmetered BYOK-payer path.
    assert q.monthly_cap_usd == settings.pro_monthly_billable_budget_usd
    assert q.monthly_excluded_purposes == ent.NON_BILLABLE_PURPOSES


def test_has_usable_key_mirrors_get_client_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.keys import BYOKDecryptError
    from app.services.keys import store as keys_store

    # Master key not configured → host pays regardless of rows.
    monkeypatch.setattr(keys_store.crypto, "is_configured", lambda: False)
    assert keys_store.has_usable_key(MagicMock(), user_id=_UID, provider="openrouter") is False

    # Configured + decryptable → the user's key pays.
    monkeypatch.setattr(keys_store.crypto, "is_configured", lambda: True)
    monkeypatch.setattr(keys_store, "get_key", lambda sb, *, user_id, provider: "sk-x")
    assert keys_store.has_usable_key(MagicMock(), user_id=_UID, provider="openrouter") is True

    # Row present but undecryptable → host pays → NOT usable.
    def _broken(sb: Any, *, user_id: str, provider: str) -> str:
        raise BYOKDecryptError("rotated")

    monkeypatch.setattr(keys_store, "get_key", _broken)
    assert keys_store.has_usable_key(MagicMock(), user_id=_UID, provider="openrouter") is False


def test_quota_saas_free_without_key_keeps_default_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Belt-and-suspenders: get_client 402s this user before any spend,
    but if a path slipped through, the legacy default cap still binds."""
    q = _quota(
        monkeypatch,
        mode="saas",
        has_key=False,
        rows=[{"llm_monthly_budget_usd": None, "llm_enabled": True, "plan": "free"}],
    )
    assert q.monthly_cap_usd == settings.user_llm_monthly_budget_usd
    assert q.monthly_excluded_purposes is None
    # #858: nobody CAN pay here — saas byok-tier with no usable key 402s at
    # get_client, so this cap is a never-consulted fallback. The /settings
    # allowance widget renders from this field; "host" here would resurrect
    # the phantom "$0.00 of $5.00" a free account can never spend.
    assert q.key_source == "none"


# ---- resolve_llm_quota_async / get_llm_account_async (#57 PR-G2c) ------------
# Async twins on the pooled async service client. Mirror the sync resolution
# contract; the profile read is awaited, so ``.execute()`` is an AsyncMock.


def _profile_supabase_async(rows: list[dict[str, Any]]) -> MagicMock:
    """Async supabase fake whose user_profiles select awaits to ``rows``."""
    sb = MagicMock(name="async_supabase")
    sb.table.return_value.select.return_value.eq.return_value.execute = AsyncMock(
        return_value=MagicMock(data=rows)
    )
    return sb


async def _quota_async(
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode: str,
    has_key: bool,
    rows: list[dict[str, Any]],
) -> budget.ResolvedQuota:
    from app.services.keys import store as keys_store

    monkeypatch.setattr(settings, "deployment_mode", mode)
    monkeypatch.setattr(keys_store, "has_usable_key_async", AsyncMock(return_value=has_key))
    return await budget.resolve_llm_quota_async(_profile_supabase_async(rows), user_id=_UID)


@pytest.mark.asyncio
async def test_quota_async_self_host_matches_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    """self_host ignores plan AND key presence — same as the sync resolver."""
    q = await _quota_async(
        monkeypatch,
        mode="self_host",
        has_key=True,  # ignored in self_host
        rows=[{"llm_monthly_budget_usd": None, "llm_enabled": True, "plan": "free"}],
    )
    assert q == budget.ResolvedQuota(settings.user_llm_monthly_budget_usd, True, None, "host")


@pytest.mark.asyncio
async def test_quota_async_saas_byok_payer_has_no_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """A usable key (async check) disables the managed monthly quota (0)."""
    q = await _quota_async(
        monkeypatch,
        mode="saas",
        has_key=True,
        rows=[{"llm_monthly_budget_usd": None, "llm_enabled": True, "plan": "pro"}],
    )
    assert q == budget.ResolvedQuota(0.0, True, None, "user")


@pytest.mark.asyncio
async def test_quota_async_saas_managed_tier_interactive_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    q = await _quota_async(
        monkeypatch,
        mode="saas",
        has_key=False,
        rows=[{"llm_monthly_budget_usd": None, "llm_enabled": True, "plan": "pro"}],
    )
    assert q.monthly_cap_usd == settings.pro_monthly_billable_budget_usd
    assert q.monthly_excluded_purposes == ent.NON_BILLABLE_PURPOSES


async def test_get_llm_account_async_reads_profile_row() -> None:
    sb = _profile_supabase_async(
        [{"llm_monthly_budget_usd": 25.0, "llm_enabled": False, "plan": "starter"}]
    )
    account = await budget.get_llm_account_async(sb, user_id=_UID)
    assert account == budget.LlmAccount(25.0, False, "starter")


async def test_get_llm_account_async_defaults_on_missing_row() -> None:
    account = await budget.get_llm_account_async(_profile_supabase_async([]), user_id=_UID)
    assert account == budget.LlmAccount(None, True, None)


# ---- check_user_budget monthly accounting ----------------------------------


def test_monthly_window_uses_billable_sum_when_excluded_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.llm import cost_log

    billable_calls: list[tuple] = []
    monkeypatch.setattr(
        cost_log,
        "total_spend",
        lambda sb, user_id, since=None: 0.0,
    )
    monkeypatch.setattr(
        cost_log,
        "total_billable_spend",
        lambda sb, user_id, since=None, *, excluded_purposes: (
            billable_calls.append((user_id, excluded_purposes)) or 0.0
        ),
    )

    budget.check_user_budget(
        MagicMock(),
        user_id=_UID,
        daily_limit_usd=5.0,
        hourly_limit_usd=1.0,
        monthly_limit_usd=6.0,
        monthly_excluded_purposes=ent.NON_BILLABLE_PURPOSES,
    )
    assert billable_calls == [(_UID, ent.NON_BILLABLE_PURPOSES)]


def test_monthly_429_fires_on_billable_spend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative: a managed user past their interactive quota is refused."""
    from fastapi import HTTPException

    from app.services.llm import cost_log

    monkeypatch.setattr(cost_log, "total_spend", lambda sb, user_id, since=None: 0.0)
    monkeypatch.setattr(
        cost_log,
        "total_billable_spend",
        lambda sb, user_id, since=None, *, excluded_purposes: 6.01,
    )

    with pytest.raises(HTTPException) as exc:
        budget.check_user_budget(
            MagicMock(),
            user_id=_UID,
            daily_limit_usd=0.0,
            hourly_limit_usd=0.0,
            monthly_limit_usd=6.0,
            monthly_excluded_purposes=ent.NON_BILLABLE_PURPOSES,
        )
    assert exc.value.status_code == 429


# ---- get_client BYOK gate ---------------------------------------------------


def _client_probe(
    monkeypatch: pytest.MonkeyPatch, *, mode: str, plan_rows: list[dict[str, Any]]
) -> Any:
    import app.services.llm as llm_mod

    monkeypatch.setattr(settings, "llm_provider", "openrouter")
    monkeypatch.setattr(settings, "deployment_mode", mode)
    monkeypatch.setattr(settings, "byok_require_user_keys", False)
    monkeypatch.setattr(llm_mod, "_user_byok_key", lambda sb, uid: None)
    return get_client(_profile_supabase(plan_rows), _UID)


def test_get_client_saas_free_without_key_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative: the free tier can NOT spend the host key — 402 path."""
    with pytest.raises(MissingUserKeyError):
        _client_probe(
            monkeypatch,
            mode="saas",
            plan_rows=[{"plan": "free"}],
        )


def test_get_client_saas_missing_profile_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative: an account with no profile row resolves to free — never
    bill the host key for an unknown account."""
    with pytest.raises(MissingUserKeyError):
        _client_probe(monkeypatch, mode="saas", plan_rows=[])


def test_get_client_saas_managed_tier_uses_host_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client_probe(monkeypatch, mode="saas", plan_rows=[{"plan": "pro"}])
    assert client is not None  # instance-key client, no refusal


def test_get_client_self_host_free_plan_keeps_env_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """self_host ignores the plan column entirely (the instance env key
    IS the owner's BYOK)."""
    client = _client_probe(monkeypatch, mode="self_host", plan_rows=[{"plan": "free"}])
    assert client is not None


# ---- effective_active_target_cap -------------------------------------------


def test_target_cap_tier_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "deployment_mode", "saas")
    # Tier default (pro).
    sb = _profile_supabase([{"max_active_targets": None, "plan": "pro"}])
    assert crud.effective_active_target_cap(sb, _UID) == settings.pro_max_active_targets
    # Per-user override wins over the tier.
    sb = _profile_supabase([{"max_active_targets": 9, "plan": "free"}])
    assert crud.effective_active_target_cap(sb, _UID) == 9
    # self_host: plan ignored, global default.
    monkeypatch.setattr(settings, "deployment_mode", "self_host")
    sb = _profile_supabase([{"max_active_targets": None, "plan": "pro"}])
    assert crud.effective_active_target_cap(sb, _UID) == crud.MAX_ACTIVE_TARGETS_PER_USER


# ---- trial tier (#841) ------------------------------------------------------
#
# The trial exists because 'free' is BYOK and this deployment has no
# BYOK_MASTER_KEY, so a new account could not complete onboarding at all.
# These pin the two properties that make a trial safe to hand a stranger:
# a HOST key (so it works) and a ceiling that counts BACKGROUND spend
# (so an idle account cannot sit there costing money for free).


def test_trial_uses_host_keys_so_onboarding_works() -> None:
    """The whole point of #841: a trial account must NOT be BYOK."""
    trial = ent.entitlements_for("trial")
    assert trial.llm_key_source == "host"
    assert trial.monthly_billable_budget_usd == settings.trial_billable_budget_usd
    assert trial.max_active_targets == settings.trial_max_active_targets


def test_trial_counts_background_against_its_ceiling() -> None:
    """A trial has no subscription behind it, so background spend must
    consume its own ceiling — otherwise the ceiling bounds only the cheap
    half. Paid tiers deliberately differ."""
    trial = ent.entitlements_for("trial")
    # Assert the precondition too: `quota_excluded_purposes is None` is also
    # true of 'free', so on its own it would pass even if the trial branch
    # were deleted entirely and 'trial' fell through to free.
    assert trial.llm_key_source == "host"
    assert trial.quota_excluded_purposes is None
    for paid in ("starter", "pro"):
        assert ent.entitlements_for(paid).quota_excluded_purposes == ent.NON_BILLABLE_PURPOSES


def test_quota_saas_trial_counts_every_purpose(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end through the resolver, not just the entitlement: a trial's
    monthly sum excludes NOTHING."""
    q = _quota(
        monkeypatch,
        mode="saas",
        has_key=False,
        rows=[{"llm_monthly_budget_usd": None, "llm_enabled": True, "plan": "trial"}],
    )
    assert q.monthly_cap_usd == settings.trial_billable_budget_usd
    assert q.monthly_excluded_purposes is None, "background must count against a trial"


# ---- trial_expired ----------------------------------------------------------

_NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


def test_trial_expired_only_after_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "trial_days", 3)
    fresh = _NOW - timedelta(days=2, hours=23)
    lapsed = _NOW - timedelta(days=3, seconds=1)

    assert ent.trial_expired("trial", fresh, now=_NOW) is False
    assert ent.trial_expired("trial", lapsed, now=_NOW) is True


def test_trial_expired_is_false_for_every_other_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    """A paid account has no clock, and 'free' is refused by the BYOK gate
    rather than by time — neither may be told their trial ended."""
    monkeypatch.setattr(settings, "trial_days", 3)
    ancient = _NOW - timedelta(days=999)
    for plan in ("free", "starter", "pro", None, "enterprise"):
        assert ent.trial_expired(plan, ancient, now=_NOW) is False


def test_missing_stamp_is_not_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail OPEN on a data anomaly: walling a user mid-evaluation is worse
    than letting the spend ceiling do the bounding."""
    monkeypatch.setattr(settings, "trial_days", 3)
    assert ent.trial_expired("trial", None, now=_NOW) is False


def test_naive_stamp_is_treated_as_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "trial_days", 3)
    naive = (_NOW - timedelta(days=10)).replace(tzinfo=None)
    assert ent.trial_expired("trial", naive, now=_NOW) is True


def test_parse_trial_stamp_degrades_to_none() -> None:
    """Unparseable → None → 'not expired'. Never raises into the gate."""
    assert ent.parse_trial_stamp(None) is None
    assert ent.parse_trial_stamp("") is None
    assert ent.parse_trial_stamp("not-a-date") is None
    parsed = ent.parse_trial_stamp("2026-08-19T12:00:00Z")
    assert parsed == _NOW


# ---- get_client: the trial gate --------------------------------------------


def test_get_client_active_trial_gets_the_host_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The regression that #841 is about: an active trial must be SERVED."""
    monkeypatch.setattr(settings, "trial_days", 3)
    client = _client_probe(
        monkeypatch,
        mode="saas",
        plan_rows=[{"plan": "trial", "trial_started_at": datetime.now(UTC).isoformat()}],
    )
    assert client is not None


def test_get_client_expired_trial_says_subscribe_not_add_a_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lapsed trial raises TrialExpiredError, NOT MissingUserKeyError —
    the two produce different user-facing instructions and a trial user
    cannot act on 'add your own API key'."""
    monkeypatch.setattr(settings, "trial_days", 3)
    stale = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    with pytest.raises(TrialExpiredError):
        _client_probe(
            monkeypatch, mode="saas", plan_rows=[{"plan": "trial", "trial_started_at": stale}]
        )


def test_expired_trial_still_served_when_they_have_their_own_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stored BYOK key wins over every plan verdict — their key, their
    bill, so an expired trial is irrelevant."""
    import app.services.llm as llm_mod

    monkeypatch.setattr(settings, "trial_days", 3)
    monkeypatch.setattr(settings, "llm_provider", "openrouter")
    monkeypatch.setattr(settings, "deployment_mode", "saas")
    monkeypatch.setattr(settings, "byok_require_user_keys", False)
    monkeypatch.setattr(llm_mod, "_user_byok_key", lambda sb, uid: "sk-or-THEIRS")
    stale = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    client = get_client(_profile_supabase([{"plan": "trial", "trial_started_at": stale}]), _UID)
    assert client is not None


def test_self_host_ignores_the_trial_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """self_host has no tiers at all — the instance key IS the owner's."""
    monkeypatch.setattr(settings, "trial_days", 3)
    stale = (datetime.now(UTC) - timedelta(days=999)).isoformat()
    client = _client_probe(
        monkeypatch, mode="self_host", plan_rows=[{"plan": "trial", "trial_started_at": stale}]
    )
    assert client is not None
