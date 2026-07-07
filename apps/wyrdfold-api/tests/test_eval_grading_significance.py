"""#193: significance testing for the grading eval.

The grading eval reports a single-run Spearman ρ vs the production baseline, so a
before/after ρ change could be real or just resampling noise. ``_bootstrap_ci``
attaches a percentile-bootstrap confidence interval. These exercise the pure
stats (no LLM, no PII snapshot) and pin the ρ/rank helpers they build on.
"""

from __future__ import annotations

import pytest

from scripts.eval_grading_prompts import _bootstrap_ci, _rank, _spearman


def test_spearman_and_rank_helpers() -> None:
    assert _spearman([1, 2, 3], [1, 2, 3]) == pytest.approx(1.0)
    assert _spearman([1, 2, 3], [3, 2, 1]) == pytest.approx(-1.0)
    # Ties get the average rank.
    assert _rank([10, 20, 20, 30]) == [1.0, 2.5, 2.5, 4.0]


def test_bootstrap_ci_is_deterministic_by_seed() -> None:
    xs = [1, 2, 3, 4, 5, 6, 7, 8]
    ys = [2, 3, 1, 5, 4, 7, 6, 8]
    a = _bootstrap_ci(xs, ys, _spearman, seed=42)
    b = _bootstrap_ci(xs, ys, _spearman, seed=42)
    assert a == b
    assert a[0] <= a[1]


def test_bootstrap_ci_tight_for_perfect_correlation() -> None:
    xs = [1, 2, 3, 4, 5, 6, 7, 8]
    ys = [10, 20, 30, 40, 50, 60, 70, 80]  # perfectly monotone
    lo, hi = _bootstrap_ci(xs, ys, _spearman)
    assert hi == pytest.approx(1.0)
    assert lo >= 0.8  # near-perfect; the rare all-same resample is tolerated


def test_bootstrap_ci_wider_for_noise_than_for_signal() -> None:
    xs = [1, 2, 3, 4, 5, 6, 7, 8]
    perfect = _bootstrap_ci(xs, [10, 20, 30, 40, 50, 60, 70, 80], _spearman)
    noisy = _bootstrap_ci(xs, [5, 2, 8, 1, 7, 3, 6, 4], _spearman)
    # A noisy relationship has a genuinely wider uncertainty band.
    assert (noisy[1] - noisy[0]) > (perfect[1] - perfect[0])


def test_bootstrap_ci_degenerate_below_two_pairs() -> None:
    # < 2 pairs can't be resampled → the CI collapses to the point estimate.
    assert _bootstrap_ci([1.0], [2.0], _spearman) == (0.0, 0.0)
    assert _bootstrap_ci([], [], _spearman) == (0.0, 0.0)
