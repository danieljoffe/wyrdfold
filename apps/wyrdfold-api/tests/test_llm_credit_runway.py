"""LLM credit-runway alarm (ingestion health check #4).

Three OpenRouter credit drains (2026-06-25, 2026-07-04, 2026-07-13) were each
discovered only after grading silently died. The alarm probes the key's
remaining credit each poll cycle and pages when the runway (remaining ÷
trailing 7-day daily spend) or an absolute USD floor is breached. Key edge:
a 402-dead pipeline spends ~$0/day, which makes any balance look like
infinite runway — that's what the floor rule exists for.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from app.config import settings
from app.services.ingestion_health import (
    OpenRouterBudget,
    _openrouter_budget,
    check_ingestion_health,
)

_NOW = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)


@pytest.fixture
def credit_check_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Arm ONLY check #4: master switch on, checks 1-3 disabled, operator
    OpenRouter key paying, default thresholds (3 days / $2 floor)."""
    monkeypatch.setattr(settings, "ingestion_health_check_enabled", True)
    monkeypatch.setattr(settings, "ingestion_max_job_age_hours", 0)
    monkeypatch.setattr(settings, "ingestion_mass_disable_ratio", 0.0)
    monkeypatch.setattr(settings, "discovery_scheduler_enabled", False)
    monkeypatch.setattr(settings, "llm_provider", "openrouter")
    monkeypatch.setattr(settings, "openrouter_api_key", "sk-or-test")
    monkeypatch.setattr(settings, "llm_credit_min_runway_days", 3.0)
    monkeypatch.setattr(settings, "llm_credit_min_remaining_usd", 2.0)


def _patch_probe(
    monkeypatch: pytest.MonkeyPatch,
    *,
    remaining: float | None,
    usage_weekly: float | None = None,
) -> list[int]:
    """Patch the budget probe. ``remaining`` lands on the ACCOUNT limit so the
    existing cases keep their meaning; ``usage_weekly`` opts a case into
    OpenRouter's own trailing rate (None → the ledger fallback)."""
    calls: list[int] = []

    async def fake_probe(client: Any = None) -> OpenRouterBudget:
        calls.append(1)
        return OpenRouterBudget(account_remaining=remaining, usage_weekly=usage_weekly)

    monkeypatch.setattr("app.services.ingestion_health._openrouter_budget", fake_probe)
    return calls


def _patch_week_spend(monkeypatch: pytest.MonkeyPatch, usd: float) -> list[int]:
    calls: list[int] = []

    async def fake_spend(_sb: Any, _since: Any = None) -> float:
        calls.append(1)
        return usd

    monkeypatch.setattr("app.services.ingestion_health.cost_log.total_spend_all_async", fake_spend)
    return calls


class TestCreditRunwayCheck:
    @pytest.mark.asyncio
    async def test_healthy_runway_no_alarm(
        self, credit_check_only: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_probe(monkeypatch, remaining=50.0)
        _patch_week_spend(monkeypatch, 35.0)  # $5/day → 10-day runway

        report = await check_ingestion_health(object(), now=_NOW)

        assert report.alerts == []
        assert report.low_credit is False
        assert report.credit_remaining_usd == 50.0
        assert report.credit_runway_days == pytest.approx(10.0)

    @pytest.mark.asyncio
    async def test_low_runway_alarms(
        self, credit_check_only: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_probe(monkeypatch, remaining=10.0)
        _patch_week_spend(monkeypatch, 35.0)  # $5/day → 2-day runway < 3

        report = await check_ingestion_health(object(), now=_NOW)

        assert report.low_credit is True
        assert len(report.alerts) == 1
        assert "credit runway low" in report.alerts[0]
        assert "$10.00" in report.alerts[0]
        assert "openrouter.ai/settings/credits" in report.alerts[0]

    @pytest.mark.asyncio
    async def test_absolute_floor_catches_starved_pipeline(
        self, credit_check_only: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # THE 402-dead case: everything has been failing, so the trailing
        # rate is $0/day and runway is uncomputable — the floor must page.
        _patch_probe(monkeypatch, remaining=1.50)
        _patch_week_spend(monkeypatch, 0.0)

        report = await check_ingestion_health(object(), now=_NOW)

        assert report.low_credit is True
        assert report.credit_runway_days is None
        assert len(report.alerts) == 1

    @pytest.mark.asyncio
    async def test_zero_rate_with_healthy_balance_stays_quiet(
        self, credit_check_only: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A deliberately idle pipeline with plenty of credit must NOT page.
        _patch_probe(monkeypatch, remaining=50.0)
        _patch_week_spend(monkeypatch, 0.0)

        report = await check_ingestion_health(object(), now=_NOW)

        assert report.alerts == []
        assert report.low_credit is False

    @pytest.mark.asyncio
    async def test_probe_failure_is_fail_soft(
        self, credit_check_only: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def boom(client: Any = None) -> float | None:
            raise httpx.ConnectError("dns down")

        monkeypatch.setattr("app.services.ingestion_health._openrouter_budget", boom)
        spend_calls = _patch_week_spend(monkeypatch, 35.0)

        report = await check_ingestion_health(object(), now=_NOW)  # must not raise

        assert report.alerts == []
        assert report.low_credit is False
        assert spend_calls == []  # never got past the probe

    @pytest.mark.asyncio
    async def test_unlimited_key_none_remaining_skips_quietly(
        self, credit_check_only: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_probe(monkeypatch, remaining=None)
        spend_calls = _patch_week_spend(monkeypatch, 35.0)

        report = await check_ingestion_health(object(), now=_NOW)

        assert report.alerts == []
        assert report.credit_remaining_usd is None
        assert spend_calls == []  # no rate math without a balance

    @pytest.mark.asyncio
    async def test_not_armed_for_non_openrouter_provider(
        self, credit_check_only: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "llm_provider", "anthropic")
        probe_calls = _patch_probe(monkeypatch, remaining=0.01)

        report = await check_ingestion_health(object(), now=_NOW)

        assert probe_calls == []
        assert report.alerts == []

    @pytest.mark.asyncio
    async def test_disabled_when_both_thresholds_zero(
        self, credit_check_only: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "llm_credit_min_runway_days", 0.0)
        monkeypatch.setattr(settings, "llm_credit_min_remaining_usd", 0.0)
        probe_calls = _patch_probe(monkeypatch, remaining=0.01)

        report = await check_ingestion_health(object(), now=_NOW)

        assert probe_calls == []
        assert report.alerts == []

    @pytest.mark.asyncio
    async def test_master_switch_off_skips_everything(
        self, credit_check_only: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "ingestion_health_check_enabled", False)
        probe_calls = _patch_probe(monkeypatch, remaining=0.01)

        report = await check_ingestion_health(object(), now=_NOW)

        assert probe_calls == []
        assert report.alerts == []


def _mock_client(routes: dict[str, dict[str, Any]]) -> httpx.AsyncClient:
    """httpx client whose transport serves canned JSON by URL path."""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = routes.get(request.url.path)
        assert payload is not None, f"unexpected call to {request.url.path}"
        return httpx.Response(200, content=json.dumps(payload))

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestOpenrouterRemainingParse:
    @pytest.mark.asyncio
    async def test_key_cap_binds_when_tighter_than_the_account(self) -> None:
        """The 2026-08-12 incident in miniature: the key's own cap runs out
        while the account is still funded. `remaining` must follow the tighter
        limit, and `binding_limit` must name it — they fail identically (403)
        but have opposite remedies."""
        client = _mock_client(
            {
                "/api/v1/key": {"data": {"limit": 20, "usage": 7.5, "limit_remaining": 12.5}},
                "/api/v1/credits": {"data": {"total_credits": 100.0, "total_usage": 20.0}},
            }
        )
        budget = await _openrouter_budget(client)
        assert budget.remaining == 12.5
        assert budget.binding_limit == "key"
        assert "raise it" in budget.remedy()
        assert "settings/credits" not in budget.remedy()

    @pytest.mark.asyncio
    async def test_unlimited_key_falls_back_to_account_credits(self) -> None:
        client = _mock_client(
            {
                "/api/v1/key": {"data": {"limit": None, "limit_remaining": None}},
                "/api/v1/credits": {"data": {"total_credits": 100.0, "total_usage": 97.25}},
            }
        )
        budget = await _openrouter_budget(client)
        assert budget.remaining == pytest.approx(2.75)
        assert budget.binding_limit == "account"
        assert "top up" in budget.remedy()

    @pytest.mark.asyncio
    async def test_missing_credit_fields_returns_none(self) -> None:
        client = _mock_client(
            {
                "/api/v1/key": {"data": {"limit_remaining": None}},
                "/api/v1/credits": {"data": {}},
            }
        )
        budget = await _openrouter_budget(client)
        assert budget.remaining is None
        assert budget.binding_limit is None

    @pytest.mark.asyncio
    async def test_http_error_raises_to_caller(self) -> None:
        # The CHECK is fail-soft; the helper itself must surface errors so
        # the check can log them (a swallowed error here would look healthy).
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, content="{}")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with pytest.raises(httpx.HTTPStatusError):
            await _openrouter_budget(client)


class TestBudgetTruthSource:
    """2026-08-12 incident. The alarm mixed two sources: `remaining` from
    OpenRouter, `daily_rate` from our own `llm_costs`. The ledger records only
    calls that COMPLETE — a generation billed and then failed downstream is
    spend we never wrote down — so on the day it read $3.01 against
    OpenRouter's $10.00 and every runway estimate ran long.
    """

    @pytest.mark.asyncio
    async def test_trailing_rate_comes_from_openrouter_not_our_ledger(
        self, monkeypatch: pytest.MonkeyPatch, credit_check_only: None
    ) -> None:
        # OpenRouter: $70/wk → $10/day. Ledger: $7/wk → $1/day. With $9 left
        # the honest runway is 0.9 days (alarm); the ledger's says 9 (quiet).
        _patch_probe(monkeypatch, remaining=9.0, usage_weekly=70.0)
        ledger_calls = _patch_week_spend(monkeypatch, 7.0)

        report = await check_ingestion_health(object(), now=_NOW)

        assert report.low_credit is True
        assert report.credit_runway_days == pytest.approx(0.9)
        # The ledger must not even be consulted — reading it at all is what
        # let the two sources disagree.
        assert ledger_calls == []
        assert "per OpenRouter" in report.alerts[0]

    @pytest.mark.asyncio
    async def test_falls_back_to_the_ledger_only_when_openrouter_withholds_usage(
        self, monkeypatch: pytest.MonkeyPatch, credit_check_only: None
    ) -> None:
        """A stale estimate beats no alarm: if OpenRouter omits usage_weekly we
        still want a runway number rather than silence."""
        _patch_probe(monkeypatch, remaining=1.0, usage_weekly=None)
        ledger_calls = _patch_week_spend(monkeypatch, 7.0)

        report = await check_ingestion_health(object(), now=_NOW)

        assert ledger_calls == [1]
        assert report.low_credit is True

    @pytest.mark.asyncio
    async def test_alert_names_the_key_cap_and_does_not_say_top_up(
        self, monkeypatch: pytest.MonkeyPatch, credit_check_only: None
    ) -> None:
        """The actual failure on 2026-08-12: a $10/day KEY cap was exhausted
        while the ACCOUNT still held $6.69, and the alarm said "top up credits"
        — which would not have cleared it. The remedy must match the limit."""

        async def fake_probe(client: Any = None) -> OpenRouterBudget:
            return OpenRouterBudget(
                key_remaining=0.0,
                key_limit=10.0,
                key_limit_reset="daily",
                account_remaining=6.69,
                usage_weekly=25.59,
            )

        monkeypatch.setattr("app.services.ingestion_health._openrouter_budget", fake_probe)

        report = await check_ingestion_health(object(), now=_NOW)

        alert = report.alerts[0]
        assert report.low_credit is True
        assert "key" in alert and "resets daily" in alert
        assert "$10.00 cap" in alert
        # The other limit is reported for context but NOT prescribed as the fix.
        assert "account $6.69" in alert
        assert "topping up credits will NOT clear this" in alert

    @pytest.mark.asyncio
    async def test_alert_says_top_up_when_the_account_is_the_binding_limit(
        self, monkeypatch: pytest.MonkeyPatch, credit_check_only: None
    ) -> None:
        """The mirror case — the remedy has to flip with the limit, or naming
        it is theatre."""

        async def fake_probe(client: Any = None) -> OpenRouterBudget:
            return OpenRouterBudget(
                key_remaining=50.0,
                key_limit=100.0,
                key_limit_reset="daily",
                account_remaining=1.0,
                usage_weekly=70.0,
            )

        monkeypatch.setattr("app.services.ingestion_health._openrouter_budget", fake_probe)

        report = await check_ingestion_health(object(), now=_NOW)

        alert = report.alerts[0]
        assert "account" in alert
        assert "settings/credits" in alert
        assert "NOT clear this" not in alert

    @pytest.mark.asyncio
    async def test_one_endpoint_down_degrades_but_both_down_is_loud(self) -> None:
        """Partial data still beats blindness; total blindness must NOT read as
        'no limit breached'."""
        partial = _mock_client(
            {"/api/v1/key": {"data": {"limit_remaining": 5.0, "usage_weekly": 14.0}}}
        )
        budget = await _openrouter_budget(partial)
        assert budget.remaining == 5.0
        assert budget.daily_rate == pytest.approx(2.0)

        def dead(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, content="{}")

        with pytest.raises(httpx.HTTPStatusError):
            await _openrouter_budget(httpx.AsyncClient(transport=httpx.MockTransport(dead)))
