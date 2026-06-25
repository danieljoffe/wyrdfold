"""Pure cosine-threshold calibration (#60, Phase 2).

Unit tests for ``app/services/embeddings/prescan_calibration.py`` on SYNTHETIC
vectors + labels — no DB, no LLM. Asserts the two regimes the plan calls out:
a clearly-separable target yields a clean (high) threshold at full recall with
no off-domain leakage, while an overlapping target is forced to a LOWER threshold
to keep the recall floor (admitting some off-domain jobs). Plus the cosine math
and the conservative fallback for sparse positives.
"""

from __future__ import annotations

import math

from app.services.embeddings.prescan_calibration import calibrate_threshold, cosine

# ---------------------------------------------------------------------------
# cosine
# ---------------------------------------------------------------------------


def test_cosine_identical_is_one() -> None:
    assert cosine([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == 1.0


def test_cosine_orthogonal_is_zero() -> None:
    assert cosine([1.0, 0.0], [0.0, 1.0]) == 0.0


def test_cosine_opposite_is_negative_one() -> None:
    assert math.isclose(cosine([1.0, 0.0], [-1.0, 0.0]), -1.0)


def test_cosine_zero_norm_is_zero_not_nan() -> None:
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_cosine_length_mismatch_raises() -> None:
    try:
        cosine([1.0], [1.0, 2.0])
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected ValueError on length mismatch")


# ---------------------------------------------------------------------------
# calibrate_threshold — separable vs overlapping
# ---------------------------------------------------------------------------


def test_separable_yields_high_threshold_full_recall_no_leakage() -> None:
    # Clean positives cluster at high cosine (~0.85-0.95); negatives sit far
    # below (~0.1-0.25). A threshold at the bottom of the positive cluster keeps
    # every positive AND admits none of the negatives. >= min_positives (10)
    # positives so the real recall-walk runs (not the sparse fallback).
    positives = [(0.85 + i * 0.01, 80.0 + i) for i in range(12)]  # 0.85..0.96, all >=80
    negatives = [(0.10 + i * 0.01, 5.0 + i) for i in range(12)]  # 0.10..0.21, all <70
    res = calibrate_threshold(
        cosines_with_labels=positives + negatives,
        positive_cutoff=70.0,
        target_recall=0.95,
    )
    assert res.n_positive == 12
    assert res.n_negative == 12
    assert res.note == ""  # real calibration, not the fallback
    assert res.recall >= 0.95
    # Threshold lands in the gap: above every negative (<=0.21), at/below the
    # lowest positive (0.85).
    assert 0.21 < res.threshold <= 0.85
    assert res.leakage == 0.0  # no off-domain job passes


def test_overlapping_yields_lower_threshold_to_keep_recall() -> None:
    # Positives are SPREAD from ~0.4 to ~0.9 and negatives reach up to ~0.6, so
    # the bands overlap. Keeping 95% recall forces the threshold DOWN toward the
    # low positives — which necessarily admits some negatives (non-zero leakage).
    positives = [(0.40 + i * 0.045, 75.0 + i) for i in range(12)]  # 0.40..0.895
    negatives = [(0.30 + i * 0.027, 10.0 + i) for i in range(12)]  # 0.30..0.597
    res = calibrate_threshold(
        cosines_with_labels=positives + negatives,
        positive_cutoff=70.0,
        target_recall=0.95,
    )
    assert res.note == ""
    assert res.recall >= 0.95
    # Forced down near the low positives to retain >=95% of them.
    assert res.threshold <= 0.50
    # Overlap ⇒ some off-domain jobs leak through (a cost signal, not correctness).
    assert res.leakage > 0.0


def test_overlapping_threshold_is_lower_than_separable() -> None:
    # The same recall floor produces a STRICTLY lower threshold when the bands
    # overlap than when they're cleanly separable — the core calibration claim.
    # Both cases use 12 positives so the real recall-walk runs in each.
    sep = calibrate_threshold(
        cosines_with_labels=(
            [(0.85 + i * 0.01, 80.0 + i) for i in range(12)]  # tight high cluster
            + [(0.10 + i * 0.01, 5.0 + i) for i in range(12)]  # far-below negatives
        ),
        positive_cutoff=70.0,
        target_recall=0.95,
    )
    overlap = calibrate_threshold(
        cosines_with_labels=(
            [(0.40 + i * 0.045, 75.0 + i) for i in range(12)]  # spread positives
            + [(0.30 + i * 0.027, 10.0 + i) for i in range(12)]  # overlapping negatives
        ),
        positive_cutoff=70.0,
        target_recall=0.95,
    )
    assert sep.note == "" and overlap.note == ""
    assert overlap.threshold < sep.threshold


def test_recall_floor_is_respected_when_relaxed() -> None:
    # A lower recall floor (0.6) lets the threshold RISE — fewer positives need
    # to survive, so a tighter gate is allowed. 12 positives spread across the
    # cosine range so dropping the floor genuinely changes the chosen cutoff.
    data = [(0.30 + i * 0.05, 75.0 + i) for i in range(12)] + [(0.15, 10.0), (0.12, 5.0)]
    strict = calibrate_threshold(cosines_with_labels=data, positive_cutoff=70.0, target_recall=0.95)
    relaxed = calibrate_threshold(cosines_with_labels=data, positive_cutoff=70.0, target_recall=0.60)
    assert strict.note == "" and relaxed.note == ""
    assert relaxed.threshold >= strict.threshold
    assert relaxed.recall >= 0.60


# ---------------------------------------------------------------------------
# sparse / degenerate
# ---------------------------------------------------------------------------


def test_sparse_positives_fall_back_to_conservative_default() -> None:
    # Only 2 positives (< min_positives=10) → don't overfit; use the low default.
    res = calibrate_threshold(
        cosines_with_labels=[(0.9, 90.0), (0.8, 85.0), (0.2, 10.0), (0.1, 5.0)],
        positive_cutoff=70.0,
        target_recall=0.95,
        conservative_default=0.30,
        min_positives=10,
    )
    assert res.threshold == 0.30
    assert res.n_positive == 2
    assert "conservative default" in res.note


def test_no_positives_reports_zero_recall_and_default() -> None:
    res = calibrate_threshold(
        cosines_with_labels=[(0.3, 10.0), (0.2, 5.0)],
        positive_cutoff=70.0,
        conservative_default=0.25,
    )
    assert res.threshold == 0.25
    assert res.n_positive == 0
    assert res.recall == 0.0


def test_calibration_from_synthetic_vectors_end_to_end() -> None:
    # Drive the full vectors→cosines→threshold path the script uses, so the
    # cosine + calibrate composition is covered without a DB. A 3-dim target and
    # jobs that are either aligned (high fit) or orthogonal (low fit).
    target = [1.0, 0.0, 0.0]
    aligned = [1.0, 0.05, 0.0]  # ~1.0 cosine
    off = [0.0, 1.0, 0.0]  # ~0.0 cosine
    labelled_jobs = (
        [(aligned, 90.0)] * 12  # 12 clean positives, all high cosine
        + [(off, 5.0)] * 12  # 12 clean negatives, all low cosine
    )
    cosines_with_labels = [(cosine(v, target), score) for v, score in labelled_jobs]
    res = calibrate_threshold(
        cosines_with_labels=cosines_with_labels, positive_cutoff=70.0, target_recall=0.95
    )
    assert res.n_positive == 12
    assert res.recall == 1.0
    assert res.leakage == 0.0
    assert res.threshold > 0.5  # well above the orthogonal negatives
