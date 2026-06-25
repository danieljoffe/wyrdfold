"""Pure cosine-threshold calibration for the pre-scan gate (#60, Phase 2).

The math the calibration script (``scripts/calibrate_prescan_threshold.py``) runs
per target, factored out as pure functions so it is unit-testable on synthetic
vectors + labels with no DB or LLM. Phase 3 (#68) admits a job to the expensive
per-target LLM grade iff ``cosine(job_vec, target_vec) >= threshold``; this
chooses that threshold from clean LLM-graded labels.

Threshold policy (recall-tuned, conservative): pick the LARGEST cosine cutoff
that still RETAINS at least ``target_recall`` of the clean-POSITIVE jobs
(``clean_score >= positive_cutoff``). Larger cutoff ⇒ fewer jobs admitted ⇒ more
LLM cost saved, but never at the expense of dropping genuine matches below the
recall floor. With sparse positives the achievable recall is coarse, so we fall
back to a conservative (admit-more) threshold rather than overfit — the LLM grade
downstream is still the real cut, so a loose gate only costs a little extra spend,
while a too-tight gate silently drops real jobs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors. 0.0 if either is zero-norm."""
    if len(a) != len(b):
        raise ValueError(f"vector length mismatch: {len(a)} vs {len(b)}")
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


@dataclass(frozen=True)
class CalibrationResult:
    """Outcome of calibrating one target's cosine threshold."""

    threshold: float
    """The chosen cosine cutoff. Jobs with cosine >= this are admitted."""

    recall: float
    """Fraction of clean-positive jobs retained at ``threshold`` (0-1)."""

    n_positive: int
    """Count of clean-positive labels (clean_score >= positive_cutoff)."""

    n_negative: int
    """Count of clean-negative labels."""

    leakage: float
    """Fraction of clean-NEGATIVE jobs that ALSO pass ``threshold`` (0-1).

    The off-domain false-positive rate at this cutoff — lower is better. A
    high leakage means the gate admits many off-domain jobs (extra LLM spend);
    it never drops a real one, so it's a cost signal, not a correctness one.
    """

    n_labels: int
    """Total labels considered."""

    note: str = ""
    """Human-readable note (e.g. why a fallback threshold was used)."""


def calibrate_threshold(
    *,
    cosines_with_labels: list[tuple[float, float]],
    positive_cutoff: float = 70.0,
    target_recall: float = 0.95,
    conservative_default: float = 0.30,
    min_positives: int = 10,
) -> CalibrationResult:
    """Pick a cosine threshold that keeps ``target_recall`` of clean positives.

    Args:
        cosines_with_labels: ``(cosine, clean_score)`` pairs for one target's
            labelled jobs. ``clean_score`` is the LLM fit grade (0-100).
        positive_cutoff: clean_score at/above which a job is a "real" match
            (the recall denominator). Default 70 — the Phase-2 "solid match"
            band.
        target_recall: minimum fraction of clean positives the threshold must
            retain. Default 0.95.
        conservative_default: threshold used when there are too few positives to
            calibrate reliably (``< min_positives``) — deliberately LOW so the
            gate admits broadly and the LLM grade stays the real cut.
        min_positives: below this many clean positives, fall back to the
            conservative default instead of overfitting a threshold.

    Returns:
        A :class:`CalibrationResult`. With no positives at all, the threshold is
        the conservative default and recall is reported as 0.0.
    """
    positives = [c for c, s in cosines_with_labels if s >= positive_cutoff]
    negatives = [c for c, s in cosines_with_labels if s < positive_cutoff]
    n_pos = len(positives)
    n_neg = len(negatives)
    n_labels = len(cosines_with_labels)

    def _leakage(thr: float) -> float:
        if not negatives:
            return 0.0
        return sum(1 for c in negatives if c >= thr) / len(negatives)

    def _recall(thr: float) -> float:
        if not positives:
            return 0.0
        return sum(1 for c in positives if c >= thr) / len(positives)

    if n_pos < min_positives:
        thr = conservative_default
        return CalibrationResult(
            threshold=thr,
            recall=_recall(thr),
            n_positive=n_pos,
            n_negative=n_neg,
            leakage=_leakage(thr),
            n_labels=n_labels,
            note=(
                f"only {n_pos} clean positive(s) (< {min_positives}); "
                f"using conservative default {conservative_default:.2f}"
            ),
        )

    # Candidate thresholds: every distinct positive-job cosine. Picking AT a
    # positive's cosine keeps that job in (>= is inclusive). Sorting descending
    # and walking down, the first threshold whose recall clears the floor is the
    # LARGEST qualifying cutoff (tightest gate that still hits the recall floor).
    candidates = sorted(set(positives), reverse=True)
    best = candidates[-1]  # smallest positive cosine → recall 1.0 fallback
    for thr in candidates:
        if _recall(thr) >= target_recall:
            best = thr
            break

    return CalibrationResult(
        threshold=best,
        recall=_recall(best),
        n_positive=n_pos,
        n_negative=n_neg,
        leakage=_leakage(best),
        n_labels=n_labels,
        note="",
    )
