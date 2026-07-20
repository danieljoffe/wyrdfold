"""#193: Phase-1 eval measures correctness vs ground-truth labels, not just
cross-model agreement. These exercise the pure metric on synthetic verdicts
(no LLM / no key) — the labeled fixture + this metric turn the drift eval into
a correctness one.
"""

from __future__ import annotations

from scripts.eval_phase1_triage import _correctness_report, _labels_by_target


def test_labels_by_target_extracts_ground_truth() -> None:
    fixture = {
        "cases": [
            {"target_id": "t1", "title": "A", "expected_promising": True},
            {"target_id": "t1", "title": "B", "expected_promising": False},
            {"target_id": "t2", "title": "C", "expected_promising": True},
            {"target_id": "t2", "title": "D"},  # unlabeled → ignored
        ]
    }
    assert _labels_by_target(fixture) == {
        "t1": {"A": True, "B": False},
        "t2": {"C": True},
    }


def test_labels_by_target_empty_when_no_labels() -> None:
    """A real-data snapshot (no expected_promising) yields no labels, so the
    correctness report is skipped and drift-only evals keep working."""
    fixture = {"cases": [{"target_id": "t1", "title": "A"}]}
    assert _labels_by_target(fixture) == {}


def test_correctness_report_scores_against_labels() -> None:
    # One target, one chunk of 4 titles (1-based verdict ids map to positions).
    titles_by_target = {"t1": ["A", "B", "C", "D"]}
    labels = {"t1": {"A": True, "B": True, "C": False, "D": False}}
    # Model caught A (TP), missed B (FN), wrongly flagged C (FP), correctly
    # passed D (TN).
    results = [
        {
            "model": "m1",
            "target_id": "t1",
            "chunk_idx": 0,
            "verdicts": {1: True, 2: False, 3: True, 4: False},
        }
    ]
    rep = _correctness_report(results, titles_by_target, labels, batch_size=25)
    m1 = rep["m1"]
    assert m1["confusion"] == {"tp": 1, "fp": 1, "tn": 1, "fn": 1}
    assert m1["recall"] == 0.5  # caught 1 of 2 truly-promising
    assert m1["precision"] == 0.5  # 1 of 2 flagged were right
    assert m1["accuracy"] == 0.5
    assert m1["false_negative_rate"] == 0.5
    assert m1["scored"] == 4


def test_correctness_report_respects_chunk_boundaries() -> None:
    """Verdict ids are 1-based *within a chunk*; the metric must map them via
    the same chunking, not a global index."""
    titles_by_target = {"t1": ["A", "B", "C"]}  # batch_size 2 → chunks [A,B],[C]
    labels = {"t1": {"A": True, "B": False, "C": True}}
    results = [
        {"model": "m", "target_id": "t1", "chunk_idx": 0, "verdicts": {1: True, 2: False}},
        {"model": "m", "target_id": "t1", "chunk_idx": 1, "verdicts": {1: True}},  # C
    ]
    rep = _correctness_report(results, titles_by_target, labels, batch_size=2)
    # A(TP), B(TN), C(TP) — all correct.
    assert rep["m"]["confusion"] == {"tp": 2, "fp": 0, "tn": 1, "fn": 0}
    assert rep["m"]["accuracy"] == 1.0


def test_correctness_report_empty_without_labels() -> None:
    results = [{"model": "m", "target_id": "t1", "chunk_idx": 0, "verdicts": {1: True}}]
    assert _correctness_report(results, {"t1": ["A"]}, {}, batch_size=25) == {}


def test_correctness_report_counts_unlabeled_separately() -> None:
    titles_by_target = {"t1": ["A", "B"]}
    labels = {"t1": {"A": True}}  # B unlabeled
    results = [{"model": "m", "target_id": "t1", "chunk_idx": 0, "verdicts": {1: True, 2: True}}]
    rep = _correctness_report(results, titles_by_target, labels, batch_size=25)
    assert rep["m"]["scored"] == 1
    assert rep["m"]["unlabeled_verdicts"] == 1
