"""#285 f/u — Phase-1 budget-deferral.

Regression guard for the fail-open bug that made prod admit ~86% of jobs (only
~3% on-target): when the daily LLM budget was hit, the poller stopped triaging
and a MISSING verdict fail-opened to promising=true. The fix distinguishes:

  * ATTEMPTED (the target actually triaged this title) — a missing verdict is an
    LLM hiccup and still fail-opens, so a rare glitch can't drop a good posting.
  * NOT attempted (budget/payer-deferred) — DEFER (None): excluded now,
    re-triaged after the budget resets. No admit-everything fallback on budget.
"""

from app.services.poller import _phase1_promising
from app.services.relevance.title_triage import TitleVerdict


def _v(promising: bool, confidence: int | None = 90) -> TitleVerdict:
    return TitleVerdict(id=1, promising=promising, confidence=confidence)


def test_attempted_promising_admits() -> None:
    assert _phase1_promising(_v(True), attempted=True, gate_active=True, min_confidence=40) is True


def test_attempted_reject_excludes() -> None:
    assert _phase1_promising(_v(False), attempted=True, gate_active=True, min_confidence=40) is False


def test_attempted_missing_verdict_failopens() -> None:
    # An LLM hiccup (dropped id) for a job we DID triage still fail-opens —
    # a rare model glitch must not drop a relevant posting.
    assert _phase1_promising(None, attempted=True, gate_active=True, min_confidence=40) is True


def test_budget_deferred_defers_not_admits() -> None:
    # THE regression: a job the budget never triaged (not attempted) must DEFER
    # (None), NOT fail-open admit. This was ~55% of prod's "promising" rows.
    assert _phase1_promising(None, attempted=False, gate_active=True, min_confidence=40) is None


def test_attempted_low_confidence_excludes() -> None:
    # A promising verdict below the confidence floor is not admitted (#47).
    assert (
        _phase1_promising(_v(True, confidence=20), attempted=True, gate_active=True, min_confidence=40)
        is False
    )


def test_gate_inactive_is_legacy_admit() -> None:
    # Triage disabled → legacy admit-all (a missing verdict admits), unchanged.
    assert _phase1_promising(None, attempted=False, gate_active=False, min_confidence=40) is True
