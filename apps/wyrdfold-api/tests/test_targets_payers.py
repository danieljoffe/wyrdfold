"""PayerBudgetGate semantics — who pays, and who gets skipped.

A target with NO active ``user_targets`` link is the app's catalog. As of
2026-08-18 its Phase-1/Phase-2 grading DEFERS by default
(``settings.grade_catalog_targets``): grading a target nobody is pursuing
billed the instance key for scores no one read, and the activation fan-out
(``bulk_title_score_for_target``) re-derives them the moment a user activates.

WHY THIS IS NOT THE 2026-07-30 REGRESSION. An earlier "never spend money
nobody will consume" rule starved the public /search corpus down to one
sponsored target's family (one active customer_experience target → 15
jobs/day across 1,145 polled boards). That rule dropped catalog targets from
the ACTIVE SET, which stopped their SOURCE POLLING — the corpus starved for
lack of ingestion. This gate suppresses LLM work only and never touches
polling. Verified on prod before shipping: 28.3% of jobs ingested in the last
7 days have NO promising score at all, so ingestion demonstrably does not
depend on admission, and public /search reads ``jobs`` while skipping
``scores`` entirely (``services/job_search.py``).

Sponsored targets keep the original protections: a payer who is over
allowance, idle, or operator-disabled still defers.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from app.services.poller import _resolve_user_targets_for_stage3
from app.services.targets.payers import PayerBudgetGate


def _gate(**kw: object) -> PayerBudgetGate:
    return PayerBudgetGate(**kw)  # type: ignore[arg-type]


def test_catalog_target_without_payer_defers_by_default() -> None:
    """Catalog-only targets no longer bill the instance key for grading.

    Measured on prod: with ZERO active ``user_targets`` the instance was still
    paying to grade five ``app_active`` catalog targets, and background grading
    was the dominant line item. The scores it wrote served nobody — the
    activation fan-out re-derives them when a user actually activates.
    """
    gate = _gate(payer_by_target={"t-catalog": None})
    assert gate.target_blocked("t-catalog") is True
    assert gate.payer_for("t-catalog") is None  # still no payer to bill


def test_catalog_grading_can_be_re_enabled_by_flag(monkeypatch) -> None:
    """``GRADE_CATALOG_TARGETS=true`` restores the pre-2026-08 behaviour, so
    the change is reversible from the environment without a deploy."""
    from app.services.targets import payers as payers_mod

    monkeypatch.setattr(payers_mod.settings, "grade_catalog_targets", True)
    gate = _gate(payer_by_target={"t-catalog": None})
    assert gate.target_blocked("t-catalog") is False


def test_sponsored_target_is_unaffected_by_the_catalog_rule() -> None:
    """The guard must key on 'has no payer', not on 'is unblocked' — a target
    WITH a healthy payer keeps grading regardless of the catalog flag."""
    gate = _gate(payer_by_target={"t-owned": "u-1"})
    assert gate.target_blocked("t-owned") is False


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

    primary_by_user, user_optimized = await _resolve_user_targets_for_stage3(sb, [target], "(test)")
    assert primary_by_user == {}
    assert user_optimized == {}
