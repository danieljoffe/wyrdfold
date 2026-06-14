"""Global LLM circuit breaker: when the day's total spend across ALL
users hits ``global_llm_daily_budget_usd``, the poll cycle's budget gate
goes empty (every target defers) while jobs keep ingesting."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.config import settings as live_settings
from app.services import poller as poller_mod
from app.services.llm import cost_log


class _Resp:
    def __init__(self, data: Any) -> None:
        self.data = data


# ---- cost_log.total_spend_all ----------------------------------------------


def test_total_spend_all_uses_rpc_when_available() -> None:
    sb = MagicMock()
    sb.rpc.return_value.execute.return_value = _Resp(0.85)

    result = cost_log.total_spend_all(sb, since=datetime.now(UTC))

    assert result == pytest.approx(0.85)
    args, _ = sb.rpc.call_args
    assert args[0] == "total_spend_all_since"
    # No per-user filter — the RPC sums across ALL users.
    assert "p_user_id" not in args[1]
    # The select-table API should NOT be touched on the RPC path.
    sb.table.assert_not_called()


def test_total_spend_all_passes_none_since_through() -> None:
    sb = MagicMock()
    sb.rpc.return_value.execute.return_value = _Resp(1.0)

    assert cost_log.total_spend_all(sb) == pytest.approx(1.0)
    args, _ = sb.rpc.call_args
    assert args[1]["p_since"] is None


def test_total_spend_all_zero_when_rpc_returns_none() -> None:
    sb = MagicMock()
    sb.rpc.return_value.execute.return_value = _Resp(None)

    assert cost_log.total_spend_all(sb, since=datetime.now(UTC)) == 0.0


def test_total_spend_all_falls_back_to_python_when_rpc_unavailable() -> None:
    sb = MagicMock()
    sb.rpc.side_effect = Exception("function does not exist")

    sel = sb.table.return_value.select.return_value
    sel.gte.return_value.execute.return_value = _Resp(
        [{"cost_usd": 0.50}, {"cost_usd": 0.25}, {"cost_usd": 0.10}]
    )

    result = cost_log.total_spend_all(sb, since=datetime.now(UTC))

    assert result == pytest.approx(0.85)
    sb.table.assert_called_once_with("llm_costs")
    # Fallback still sums across ALL users — no per-user partition.
    sel.eq.assert_not_called()
    sel.is_.assert_not_called()


# ---- _cycle_budget_gate breaker integration ---------------------------------


def _active_target() -> MagicMock:
    target = MagicMock()
    target.id = "t-1"
    return target


@pytest.mark.asyncio
async def test_breaker_tripped_returns_empty_gate(monkeypatch) -> None:
    """Spend at/over the cap → EMPTY gate (all targets blocked), no
    payer-gate build, but has_active_targets stays truthful."""
    monkeypatch.setattr(live_settings, "global_llm_daily_budget_usd", 10.0)
    spend = MagicMock(return_value=12.34)
    build = MagicMock()
    monkeypatch.setattr(poller_mod, "total_llm_spend_all", spend)
    monkeypatch.setattr(poller_mod, "build_budget_gate", build)
    monkeypatch.setattr(
        poller_mod, "get_active_target", lambda _sb: [_active_target()]
    )

    gate, has_active = await poller_mod._cycle_budget_gate(MagicMock())

    assert has_active is True
    assert gate.target_blocked("t-1") is True
    assert gate.target_blocked("any-other-target") is True
    build.assert_not_called()
    # The window starts at UTC midnight.
    since = spend.call_args.kwargs["since"]
    assert (since.hour, since.minute, since.second) == (0, 0, 0)
    assert since.tzinfo == UTC


@pytest.mark.asyncio
async def test_breaker_under_cap_builds_normal_gate(monkeypatch) -> None:
    monkeypatch.setattr(live_settings, "global_llm_daily_budget_usd", 10.0)
    monkeypatch.setattr(
        poller_mod, "total_llm_spend_all", MagicMock(return_value=1.25)
    )
    real_gate = MagicMock()
    monkeypatch.setattr(
        poller_mod, "build_budget_gate", MagicMock(return_value=real_gate)
    )
    monkeypatch.setattr(
        poller_mod, "get_active_target", lambda _sb: [_active_target()]
    )

    gate, has_active = await poller_mod._cycle_budget_gate(MagicMock())

    assert gate is real_gate
    assert has_active is True


@pytest.mark.asyncio
async def test_breaker_disabled_when_cap_is_zero(monkeypatch) -> None:
    monkeypatch.setattr(live_settings, "global_llm_daily_budget_usd", 0.0)
    spend = MagicMock(return_value=999.0)
    monkeypatch.setattr(poller_mod, "total_llm_spend_all", spend)
    real_gate = MagicMock()
    monkeypatch.setattr(
        poller_mod, "build_budget_gate", MagicMock(return_value=real_gate)
    )
    monkeypatch.setattr(
        poller_mod, "get_active_target", lambda _sb: [_active_target()]
    )

    gate, _ = await poller_mod._cycle_budget_gate(MagicMock())

    assert gate is real_gate
    spend.assert_not_called()


@pytest.mark.asyncio
async def test_breaker_warns_at_80_percent_under_cap(monkeypatch) -> None:
    """At ≥80% of the global cap (under the trip threshold) the breaker
    emits a Sentry warning so the operator sees the run-up before LLM
    work actually defers (#26 F3)."""
    monkeypatch.setattr(live_settings, "global_llm_daily_budget_usd", 10.0)
    monkeypatch.setattr(live_settings, "sentry_dsn", "https://x@sentry/1")
    # 8.5 / 10 = 85% — over the 80% threshold, under the cap.
    monkeypatch.setattr(
        poller_mod, "total_llm_spend_all", MagicMock(return_value=8.5)
    )
    # Reset the per-day dedup so the warning can fire this test.
    monkeypatch.setattr(poller_mod, "_GLOBAL_APPROACHING_DAY", None)

    import sentry_sdk

    captured: list[tuple[str, str]] = []
    monkeypatch.setattr(
        sentry_sdk,
        "capture_message",
        lambda msg, level=None: captured.append((msg, level)),
    )

    tripped = poller_mod._global_circuit_breaker_tripped(MagicMock())

    assert tripped is False
    assert len(captured) == 1
    msg, level = captured[0]
    assert level == "warning"
    assert "approaching" in msg.lower()
    assert "85%" in msg


@pytest.mark.asyncio
async def test_breaker_query_failure_never_crashes_the_poll(monkeypatch) -> None:
    """A spend-meter read failure falls into the existing fail-closed
    arm: empty gate, has_active=True, no exception escapes."""
    monkeypatch.setattr(live_settings, "global_llm_daily_budget_usd", 10.0)
    monkeypatch.setattr(
        poller_mod,
        "total_llm_spend_all",
        MagicMock(side_effect=RuntimeError("db down")),
    )
    monkeypatch.setattr(
        poller_mod, "get_active_target", lambda _sb: [_active_target()]
    )

    gate, has_active = await poller_mod._cycle_budget_gate(MagicMock())

    assert has_active is True
    assert gate.target_blocked("t-1") is True
