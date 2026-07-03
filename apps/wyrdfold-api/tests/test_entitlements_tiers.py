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

from typing import Any
from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.services import entitlements as ent
from app.services.llm import MissingUserKeyError, budget, get_client
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
    monkeypatch.setattr(
        keys_store, "has_key", lambda sb, *, user_id, provider: has_key
    )
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
    assert q == budget.ResolvedQuota(
        settings.user_llm_monthly_budget_usd, True, None
    )

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
    assert q == budget.ResolvedQuota(0.0, True, None)


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

    monkeypatch.setattr(
        cost_log, "total_spend", lambda sb, user_id, since=None: 0.0
    )
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
    client = _client_probe(
        monkeypatch, mode="saas", plan_rows=[{"plan": "pro"}]
    )
    assert client is not None  # instance-key client, no refusal


def test_get_client_self_host_free_plan_keeps_env_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """self_host ignores the plan column entirely (the instance env key
    IS the owner's BYOK)."""
    client = _client_probe(
        monkeypatch, mode="self_host", plan_rows=[{"plan": "free"}]
    )
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
