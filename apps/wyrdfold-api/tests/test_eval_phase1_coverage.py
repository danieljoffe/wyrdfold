"""#47: the Phase-1 multi-model eval must not report high agreement while a
candidate silently drops titles.

``_agreement_report`` compares verdicts only on ids BOTH models answered, so a
model that fails to emit a verdict for 1/3 of titles could still show 100%
agreement on the 2/3 it answered. These tests pin the coverage honesty check:
coverage is surfaced, an un-answered PROMISING title counts toward a
coverage-adjusted FNR, and low coverage is flagged.
"""

from __future__ import annotations

from typing import Any

from scripts.eval_phase1_triage import _MIN_COVERAGE, _agreement_report


def _result(model: str, verdicts: dict[int, bool]) -> dict[str, Any]:
    return {
        "model": model,
        "target_id": "t1",
        "chunk_idx": 0,
        "verdicts": verdicts,
        "cost_usd": 0.0,
        "latency_ms": 1,
        "error": None,
    }


def test_coverage_penalizes_dropped_verdicts() -> None:
    # Reference grades 3 titles (2 promising, 1 not); the candidate answers only
    # 2 of them — it drops id 3, which the reference marked PROMISING.
    results = [
        _result("haiku-4.5", {1: True, 2: False, 3: True}),
        _result("cheap", {1: True, 2: False}),
    ]
    report = _agreement_report(
        results,
        titles_by_target={"t1": ["a", "b", "c"]},
        models={"haiku-4.5": "ref-id", "cheap": "cheap-id"},
        reference="haiku-4.5",
    )
    stats = report["per_model"]["cheap"]

    # Agreement on the answered pairs is a perfect 100% — the trap the old
    # metric fell into.
    assert stats["agreement_rate"] == 1.0
    assert stats["compared"] == 2

    # ...but coverage exposes the drop: only 2 of 3 reference verdicts answered.
    assert stats["coverage"] == round(2 / 3, 4)
    assert stats["coverage"] < _MIN_COVERAGE
    assert stats["missing_verdicts_in_model"] == 1
    assert stats["missing_promising_in_model"] == 1

    # Answered-only FNR is 0 (the one promising it DID answer was a TP), but the
    # coverage-adjusted FNR counts the dropped promising title as a miss.
    assert stats["false_negative_rate"] == 0.0
    assert stats["false_negative_rate_with_coverage"] == 0.5


def test_full_coverage_is_clean() -> None:
    results = [
        _result("haiku-4.5", {1: True, 2: False}),
        _result("cheap", {1: True, 2: False}),
    ]
    report = _agreement_report(
        results,
        titles_by_target={"t1": ["a", "b"]},
        models={"haiku-4.5": "ref-id", "cheap": "cheap-id"},
        reference="haiku-4.5",
    )
    stats = report["per_model"]["cheap"]
    assert stats["coverage"] == 1.0
    assert stats["coverage"] >= _MIN_COVERAGE
    assert stats["missing_verdicts_in_model"] == 0
    assert stats["missing_promising_in_model"] == 0
    assert stats["false_negative_rate_with_coverage"] == stats["false_negative_rate"]
