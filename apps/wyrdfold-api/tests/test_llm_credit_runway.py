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
    _openrouter_remaining_usd,
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
    monkeypatch: pytest.MonkeyPatch, *, remaining: float | None
) -> list[int]:
    calls: list[int] = []

    async def fake_probe(client: Any = None) -> float | None:
        calls.append(1)
        return remaining

    monkeypatch.setattr(
        "app.services.ingestion_health._openrouter_remaining_usd", fake_probe
    )
    return calls


def _patch_week_spend(monkeypatch: pytest.MonkeyPatch, usd: float) -> list[int]:
    calls: list[int] = []

    def fake_spend(_sb: Any, _since: Any = None) -> float:
        calls.append(1)
        return usd

    monkeypatch.setattr(
        "app.services.ingestion_health.cost_log.total_spend_all", fake_spend
    )
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

        monkeypatch.setattr(
            "app.services.ingestion_health._openrouter_remaining_usd", boom
        )
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
    async def test_key_limit_remaining_wins(self) -> None:
        client = _mock_client(
            {"/api/v1/key": {"data": {"limit": 20, "usage": 7.5, "limit_remaining": 12.5}}}
        )
        assert await _openrouter_remaining_usd(client) == 12.5

    @pytest.mark.asyncio
    async def test_unlimited_key_falls_back_to_account_credits(self) -> None:
        client = _mock_client(
            {
                "/api/v1/key": {"data": {"limit": None, "limit_remaining": None}},
                "/api/v1/credits": {"data": {"total_credits": 100.0, "total_usage": 97.25}},
            }
        )
        assert await _openrouter_remaining_usd(client) == pytest.approx(2.75)

    @pytest.mark.asyncio
    async def test_missing_credit_fields_returns_none(self) -> None:
        client = _mock_client(
            {
                "/api/v1/key": {"data": {"limit_remaining": None}},
                "/api/v1/credits": {"data": {}},
            }
        )
        assert await _openrouter_remaining_usd(client) is None

    @pytest.mark.asyncio
    async def test_http_error_raises_to_caller(self) -> None:
        # The CHECK is fail-soft; the helper itself must surface errors so
        # the check can log them (a swallowed error here would look healthy).
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, content="{}")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with pytest.raises(httpx.HTTPStatusError):
            await _openrouter_remaining_usd(client)
