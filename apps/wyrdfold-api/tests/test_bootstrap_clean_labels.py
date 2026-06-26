"""Sampling-logic tests for ``scripts/bootstrap_clean_labels.py`` (#60).

Focus: the top-keyword-band oversampling that fixes the calibration's
positive-starvation — the even-width stratified sample surfaced too few graded
fits (~8/target) to fix a recall threshold, because the corpus is bottom-heavy
so equal-width bands leave the fit-rich top band sparse.
"""

from __future__ import annotations

import random

from scripts.bootstrap_clean_labels import (
    _DEFAULT_TOP_BAND_FRACTION,
    _even_band_sample,
    _stratified_sample,
)


def _rows(n_low: int, n_high: int, *, low: int = 1, high: int = 100) -> list[dict]:
    """A bottom-heavy score distribution: ``n_low`` low-score rows + ``n_high``
    high-score rows — the realistic shape (most jobs are off-target). With
    ``n_high`` == a quarter of the total, the top score-quartile is exactly the
    high rows, which makes the assertions below structural rather than lucky."""
    rows = [{"job_posting_id": f"lo-{i}", "score": low} for i in range(n_low)]
    rows += [{"job_posting_id": f"hi-{i}", "score": high} for i in range(n_high)]
    return rows


def _count_high(ids: list[str]) -> int:
    return sum(1 for j in ids if j.startswith("hi-"))


def test_top_band_oversamples_high_scores_vs_even_band() -> None:
    # 750 low + 250 high → the top score-quartile is exactly the 250 high rows.
    rows = _rows(750, 250)
    n = 100

    biased = _stratified_sample(rows, n=n, rng=random.Random(7), top_band_fraction=0.5)
    even = _stratified_sample(rows, n=n, rng=random.Random(7), top_band_fraction=0.0)

    # n_top = round(100*0.5) = 50 ids pulled from the top quartile (all high),
    # so the biased sample carries >= 50 fits deterministically and strictly
    # more than the pure even-band sample — the entire point of the change.
    # Remove the oversampling and this fails.
    assert _count_high(biased) >= 50
    assert _count_high(biased) > _count_high(even)


def test_top_band_fraction_zero_is_pure_even_band() -> None:
    """``top_band_fraction=0`` must be a clean passthrough to the even-band
    sampler (same rng → identical pick), so the old behavior stays reachable."""
    rows = _rows(400, 120)
    n = 80
    via_strat = _stratified_sample(rows, n=n, rng=random.Random(3), top_band_fraction=0.0)
    via_even = _even_band_sample(rows, n=n, rng=random.Random(3))
    assert via_strat == via_even


def test_top_band_fraction_one_draws_only_top_quartile() -> None:
    rows = _rows(750, 250)  # top quartile == the high rows
    ids = _stratified_sample(rows, n=100, rng=random.Random(1), top_band_fraction=1.0)
    assert _count_high(ids) == 100


def test_out_of_range_fraction_is_clamped() -> None:
    rows = _rows(750, 250)
    # > 1.0 clamps to all-top-quartile; < 0 clamps to even-band — neither raises.
    assert (
        _count_high(_stratified_sample(rows, n=100, rng=random.Random(2), top_band_fraction=5.0))
        == 100
    )
    assert _stratified_sample(
        rows, n=80, rng=random.Random(4), top_band_fraction=-1.0
    ) == _even_band_sample(rows, n=80, rng=random.Random(4))


def test_sample_size_and_uniqueness() -> None:
    rows = _rows(500, 200)
    ids = _stratified_sample(
        rows, n=150, rng=random.Random(9), top_band_fraction=_DEFAULT_TOP_BAND_FRACTION
    )
    assert len(ids) == 150
    assert len(set(ids)) == len(ids)  # no dupes across the top + even-band tranches


def test_small_pool_returns_everything() -> None:
    rows = _rows(3, 2)
    ids = _stratified_sample(rows, n=100, rng=random.Random(0))
    assert sorted(ids) == sorted(r["job_posting_id"] for r in rows)


def test_deterministic_under_seed() -> None:
    rows = _rows(300, 100)
    a = _stratified_sample(rows, n=60, rng=random.Random(42), top_band_fraction=0.5)
    b = _stratified_sample(rows, n=60, rng=random.Random(42), top_band_fraction=0.5)
    assert a == b
