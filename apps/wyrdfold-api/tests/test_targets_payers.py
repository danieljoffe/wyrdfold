"""PayerBudgetGate semantics — the app-owned-catalog contract.

The load-bearing pin: a target with NO active ``user_targets`` link is the
app's catalog, and its Phase-1 work is NOT blocked (it bills the instance
key via ``_resolve_payer_client(None)``). Before this contract, an
unsponsored active target deferred triage forever, which starved the public
/search corpus down to the one sponsored target's family (2026-07-30: one
active customer_experience target → 15 jobs/day across 1,145 polled boards).

Sponsored targets keep the original protections: a payer who is over
allowance, idle, or operator-disabled still defers.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from app.services.poller import _resolve_user_targets_for_stage3
from app.services.targets.payers import PayerBudgetGate


def _gate(**kw: object) -> PayerBudgetGate:
    return PayerBudgetGate(**kw)  # type: ignore[arg-type]


def test_catalog_target_without_payer_is_not_blocked() -> None:
    gate = _gate(payer_by_target={"t-catalog": None})
    assert gate.target_blocked("t-catalog") is False
    assert gate.payer_for("t-catalog") is None  # → instance key downstream


def test_unknown_target_in_healthy_snapshot_is_not_blocked() -> None:
    # A target activated after the cycle snapshot: same catalog semantics,
    # at most one cycle of instance-billed drift (snapshot contract).
    gate = _gate(payer_by_target={"t-known": "u-1"})
    assert gate.target_blocked("t-new") is False


def test_empty_gate_stays_fail_closed() -> None:
    # PayerBudgetGate() is the breaker / build-failure sentinel: with no
    # payer snapshot at all, refuse ALL spend — catalog semantics must
    # never fail-open an error path.
    gate = _gate(payer_by_target={})
    assert gate.target_blocked("t-anything") is True


def test_sponsored_target_with_healthy_payer_is_not_blocked() -> None:
    gate = _gate(payer_by_target={"t": "u-1"})
    assert gate.target_blocked("t") is False


def test_sponsored_target_blocks_on_over_budget_idle_or_disabled() -> None:
    for field in ("over_budget_users", "idle_users", "disabled_users"):
        gate = _gate(payer_by_target={"t": "u-1"}, **{field: frozenset({"u-1"})})
        assert gate.target_blocked("t") is True, field
        assert gate.user_blocked("u-1") is True, field


async def test_no_active_user_links_means_no_stage3_grading() -> None:
    """The other half of the catalog contract: user-scoped Phase-2/stage-3
    NEVER runs for targets nobody has actively joined — ``primary_by_user``
    comes from active ``user_targets`` rows only. This is what makes broad
    catalog ingestion safe for users who keep their links inactive on
    purpose (no grading spend against their profile)."""
    target = MagicMock()
    target.id = "t-catalog"

    ut_chain = MagicMock()
    ut_chain.select.return_value = ut_chain
    ut_chain.eq.return_value = ut_chain
    ut_chain.in_.return_value = ut_chain
    ut_chain.execute.return_value.data = []  # zero active links

    sb = MagicMock()
    sb.table.return_value = ut_chain

    primary_by_user, user_optimized = await _resolve_user_targets_for_stage3(
        sb, [target], "(test)"
    )
    assert primary_by_user == {}
    assert user_optimized == {}
