"""#1105: a budget guard that cannot read spend must BLOCK, never permit.

In plain terms: these guards answer "is there budget left?". When the spend
total cannot be established, the honest answer is "I don't know" — and the
only safe action on "I don't know" is to stop. Permitting is how spending runs
past a cap with nobody noticing, which is strictly worse than an over-cautious
pause: a pause is recoverable, spend is not.

The three guards are independent code paths and each is pinned here, because
fixing one and leaving another permissive would leave the hole open.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.config import settings
from app.services.llm import budget, cost_log
from app.services.llm.cost_log import SpendTotalUnavailableError

pytestmark = pytest.mark.asyncio


def _unavailable(*_a: Any, **_k: Any) -> float:
    raise SpendTotalUnavailableError("total_spend_all_since exceeded the statement timeout")


async def _unavailable_async(*_a: Any, **_k: Any) -> float:
    raise SpendTotalUnavailableError("total_spend_all_since exceeded the statement timeout")


# ---- guard 1: the global circuit breaker ----------------------------------


async def test_the_global_breaker_treats_an_unknown_total_as_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import poller

    monkeypatch.setattr(settings, "global_llm_daily_budget_usd", 10.0)
    monkeypatch.setattr(poller, "_memoized_total_spend", _unavailable_async)

    assert await poller._global_budget_exhausted(MagicMock()) is True


async def test_the_global_breaker_still_permits_when_the_total_is_known(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The precondition: without this, "always True" would pass the test
    above for the wrong reason."""
    from app.services import poller

    monkeypatch.setattr(settings, "global_llm_daily_budget_usd", 10.0)

    async def _cheap(*_a: Any, **_k: Any) -> float:
        return 1.0

    monkeypatch.setattr(poller, "_memoized_total_spend", _cheap)

    assert await poller._global_budget_exhausted(MagicMock()) is False


# ---- guard 2: the per-user budget check -----------------------------------


async def test_the_user_guard_blocks_with_503_when_spend_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cost_log, "total_spend_async", _unavailable_async)

    with pytest.raises(HTTPException) as exc:
        await budget.check_user_budget_async(
            MagicMock(), user_id="u1", daily_limit_usd=5.0, hourly_limit_usd=1.0
        )

    # 503, not 429: the user has not hit a limit, the check itself failed.
    # Telling them they are over budget would be a false accusation.
    assert exc.value.status_code == 503
    assert exc.value.detail["code"] == "llm_budget_check_unavailable"


def test_the_sync_user_guard_blocks_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cost_log, "total_spend", _unavailable)

    with pytest.raises(HTTPException) as exc:
        budget.check_user_budget(
            MagicMock(), user_id="u1", daily_limit_usd=5.0, hourly_limit_usd=1.0
        )

    assert exc.value.status_code == 503


async def test_the_user_guard_still_permits_a_user_under_their_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _cheap(*_a: Any, **_k: Any) -> float:
        return 0.01

    monkeypatch.setattr(cost_log, "total_spend_async", _cheap)

    await budget.check_user_budget_async(
        MagicMock(), user_id="u1", daily_limit_usd=5.0, hourly_limit_usd=1.0
    )


async def test_an_over_cap_user_still_gets_429_not_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two failure modes must stay distinguishable: genuinely over the cap
    is still a 429, so the new 503 cannot mask it."""

    async def _expensive(*_a: Any, **_k: Any) -> float:
        return 99.0

    monkeypatch.setattr(cost_log, "total_spend_async", _expensive)

    with pytest.raises(HTTPException) as exc:
        await budget.check_user_budget_async(
            MagicMock(), user_id="u1", daily_limit_usd=5.0, hourly_limit_usd=1.0
        )

    assert exc.value.status_code == 429


# ---- guard 3: the payer gate ----------------------------------------------


async def test_the_payer_gate_marks_a_user_over_budget_when_spend_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.targets.payers as payers_mod
    from app.services.targets.payers import build_budget_gate

    monkeypatch.setattr(payers_mod, "resolve_target_payers", AsyncMock(return_value={"t-1": "u-1"}))
    sb = MagicMock()
    sb.table.return_value.select.return_value.in_.return_value.execute = AsyncMock(
        return_value=MagicMock(
            data=[
                {
                    "user_id": "u-1",
                    "llm_monthly_budget_usd": None,
                    "last_seen_at": datetime.now(UTC).isoformat(),
                }
            ]
        )
    )
    monkeypatch.setattr(payers_mod.settings, "idle_defer_days", 0)
    monkeypatch.setattr(payers_mod.settings, "user_llm_monthly_budget_usd", 5.0)
    monkeypatch.setattr(payers_mod.settings, "payer_daily_budget_usd", 0.0)
    monkeypatch.setattr(payers_mod.cost_log, "total_spend_async", _unavailable_async)

    gate = await build_budget_gate(sb, ["t-1"])

    assert "u-1" in gate.over_budget_users
    assert gate.target_blocked("t-1") is True


async def test_the_payer_gate_still_admits_a_payer_under_their_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Precondition for the test above: the same fixture with a READABLE
    total must NOT block, or "always blocked" would pass it for free."""
    import app.services.targets.payers as payers_mod
    from app.services.targets.payers import build_budget_gate

    monkeypatch.setattr(payers_mod, "resolve_target_payers", AsyncMock(return_value={"t-1": "u-1"}))
    sb = MagicMock()
    sb.table.return_value.select.return_value.in_.return_value.execute = AsyncMock(
        return_value=MagicMock(
            data=[
                {
                    "user_id": "u-1",
                    "llm_monthly_budget_usd": None,
                    "last_seen_at": datetime.now(UTC).isoformat(),
                }
            ]
        )
    )
    monkeypatch.setattr(payers_mod.settings, "idle_defer_days", 0)
    monkeypatch.setattr(payers_mod.settings, "user_llm_monthly_budget_usd", 5.0)
    monkeypatch.setattr(payers_mod.settings, "payer_daily_budget_usd", 0.0)
    monkeypatch.setattr(payers_mod.cost_log, "total_spend_async", AsyncMock(return_value=0.0))

    gate = await build_budget_gate(sb, ["t-1"])

    assert "u-1" not in gate.over_budget_users
    assert gate.target_blocked("t-1") is False


# ---- the amplifier the release gate caught ---------------------------------


async def test_a_failing_spend_read_is_not_re_issued_on_every_breaker_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Found by the #1088+#1105 release gate, not by either PR alone.

    ``_global_budget_exhausted`` is called once per job inside the triage
    loops. Its 60s memo used to cache only SUCCESS, which was harmless while a
    failed spend read fell back to a number. #1105 made it raise — correctly —
    and that turned the memo into an amplifier: 25 breaker calls produced 25
    failing queries, each running until the 8-second statement timeout killed
    it, against the database that was already too slow to answer.

    The breaker must still hold closed throughout; the point is that it does
    so from the remembered answer instead of re-asking.
    """
    from app.services import poller

    calls = 0

    async def failing(*_a: Any, **_k: Any) -> float:
        nonlocal calls
        calls += 1
        raise SpendTotalUnavailableError("exceeded the statement timeout")

    monkeypatch.setattr(settings, "global_llm_daily_budget_usd", 10.0)
    monkeypatch.setattr(poller, "total_llm_spend_all_async", failing)
    monkeypatch.setitem(poller._spend_memo, "at", 0.0)
    monkeypatch.setitem(poller._spend_memo, "midnight", None)
    monkeypatch.setitem(poller._spend_memo, "error", None)

    results = [await poller._global_budget_exhausted(MagicMock()) for _ in range(25)]

    assert all(results), "the breaker must stay closed for every call"
    assert calls == 1, f"the failing read was re-issued {calls} times; the memo must hold it"


async def test_the_memo_still_re_reads_once_the_ttl_lapses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half: remembering a failure must not wedge the breaker shut
    forever. Once the TTL lapses it tries again, and a recovered database
    re-opens it."""
    from app.services import poller

    monkeypatch.setattr(settings, "global_llm_daily_budget_usd", 10.0)
    monkeypatch.setitem(poller._spend_memo, "at", 0.0)
    monkeypatch.setitem(poller._spend_memo, "midnight", None)
    monkeypatch.setitem(poller._spend_memo, "error", None)

    monkeypatch.setattr(poller, "total_llm_spend_all_async", _unavailable_async)
    assert await poller._global_budget_exhausted(MagicMock()) is True

    # The database recovers; expire the memo the way the clock would.
    async def _cheap(*_a: Any, **_k: Any) -> float:
        return 1.0

    monkeypatch.setattr(poller, "total_llm_spend_all_async", _cheap)
    monkeypatch.setitem(poller._spend_memo, "at", 0.0)

    assert await poller._global_budget_exhausted(MagicMock()) is False
