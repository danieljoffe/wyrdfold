"""Pre-scan cosine gate FLIP (#90): one-fetch batch verdicts + holdout.

`cosine_gate_batch` returns cosines + threshold from a single vector fetch;
`GateBatch.admit` yields True/False (real verdict) or None ("no opinion" —
DATA absence only, callers admit). Infrastructure errors RAISE so the caller
defers the batch instead of grading it ungated — the 2026-07-12 audit found
the old error→all-None fail-open converting IO timeouts into ungated Sonnet
spend (~20% of a 48h gated sample). `in_prescan_holdout` is the deterministic
slice of would-drop jobs kept for grading so the false-negative rate stays
measurable. The runner-side wiring (defer-on-raise, single-fetch ordering) is
exercised in `test_phase2_runner.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.models.targets import JobTarget, ScoringProfile
from app.services.embeddings.prescan_gate import (
    GateBatch,
    cosine_gate_batch,
    in_prescan_holdout,
)


def _target(target_id: str = "tgt-1") -> JobTarget:
    now = datetime(2026, 6, 24, tzinfo=UTC)
    return JobTarget(
        id=target_id,
        label="Staff Frontend Engineer",
        scoring_profile=ScoringProfile(),
        is_active=True,
        created_at=now,
        updated_at=now,
    )


_VEC_A = [1.0, 0.0, 0.0]
_NEAR = [0.9, 0.1, 0.0]  # cosine to _VEC_A ≈ 0.994 → admit at thr 0.5
_FAR = [0.0, 1.0, 0.0]  # cosine to _VEC_A == 0.0 → drop at thr 0.5


class _Q:
    def __init__(self, rows: Any) -> None:
        self._rows = rows

    def select(self, *_a: Any, **_k: Any) -> _Q:
        return self

    def eq(self, *_a: Any, **_k: Any) -> _Q:
        return self

    def in_(self, *_a: Any, **_k: Any) -> _Q:
        return self

    def limit(self, *_a: Any, **_k: Any) -> _Q:
        return self

    def execute(self) -> Any:
        from unittest.mock import MagicMock

        return MagicMock(data=self._rows)


class _SB:
    def __init__(self, *, target_rows: Any, job_rows: Any, raise_: bool = False) -> None:
        self._target_rows = target_rows
        self._job_rows = job_rows
        self._raise = raise_

    def table(self, name: str) -> _Q:
        if self._raise:
            raise RuntimeError("boom")
        return _Q(self._job_rows if name == "job_embeddings" else self._target_rows)


_CALIBRATED = [{"embedding": _VEC_A, "prescan_cosine_threshold": 0.5}]


@pytest.mark.asyncio
async def test_batch_admits_above_drops_below_threshold() -> None:
    sb = _SB(
        target_rows=_CALIBRATED,
        job_rows=[
            {"job_posting_id": "hi", "embedding": _NEAR},
            {"job_posting_id": "lo", "embedding": _FAR},
        ],
    )
    gate = await cosine_gate_batch(sb, _target(), ["hi", "lo"])
    assert gate.admit("hi") is True
    assert gate.admit("lo") is False
    # The SAME fetch carries the ordering signal — no second read needed.
    assert gate.cosines["hi"] > 0.9
    assert gate.cosines["lo"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_batch_missing_job_vector_is_none_failopen() -> None:
    sb = _SB(target_rows=_CALIBRATED, job_rows=[{"job_posting_id": "hi", "embedding": _NEAR}])
    gate = await cosine_gate_batch(sb, _target(), ["hi", "novec"])
    assert gate.admit("hi") is True
    assert gate.admit("novec") is None  # missing vector → no opinion (admit)
    assert "novec" not in gate.cosines


@pytest.mark.asyncio
async def test_batch_uncalibrated_target_all_none() -> None:
    # No embedding / threshold on the target → gate has no opinion for anything.
    sb = _SB(target_rows=[{"embedding": None, "prescan_cosine_threshold": None}], job_rows=[])
    gate = await cosine_gate_batch(sb, _target(), ["a", "b"])
    assert gate.admit("a") is None
    assert gate.admit("b") is None


@pytest.mark.asyncio
async def test_batch_embedded_but_no_threshold_is_none_with_cosines() -> None:
    # Mid-cutover shape (threshold NULLed, embedding present): no verdicts,
    # but cosines still flow for ordering.
    sb = _SB(
        target_rows=[{"embedding": _VEC_A, "prescan_cosine_threshold": None}],
        job_rows=[{"job_posting_id": "hi", "embedding": _NEAR}],
    )
    gate = await cosine_gate_batch(sb, _target(), ["hi"])
    assert gate.admit("hi") is None
    assert gate.cosines["hi"] > 0.9


@pytest.mark.asyncio
async def test_batch_dim_mismatch_is_none() -> None:
    sb = _SB(
        target_rows=_CALIBRATED,
        job_rows=[{"job_posting_id": "bad", "embedding": [1.0, 0.0]}],  # 2-dim vs 3
    )
    gate = await cosine_gate_batch(sb, _target(), ["bad"])
    assert gate.admit("bad") is None
    assert "bad" not in gate.cosines


@pytest.mark.asyncio
async def test_batch_infra_error_raises_fail_closed() -> None:
    # THE contract flip: an infrastructure error must RAISE (caller defers),
    # never manufacture all-None fail-open admits — that path bought ungated
    # Sonnet grades under IO stress (2026-07-12 audit).
    sb = _SB(target_rows=_CALIBRATED, job_rows=[], raise_=True)
    with pytest.raises(RuntimeError):
        await cosine_gate_batch(sb, _target(), ["a", "b"])


@pytest.mark.asyncio
async def test_batch_empty_ids() -> None:
    sb = _SB(target_rows=_CALIBRATED, job_rows=[])
    gate = await cosine_gate_batch(sb, _target(), [])
    assert gate == GateBatch(cosines={}, threshold=None)


def test_holdout_bounds_and_determinism() -> None:
    assert in_prescan_holdout("j", "t", 0.0) is False  # disabled
    assert in_prescan_holdout("j", "t", 1.0) is True  # always
    # Deterministic: same pair, same answer every call.
    a = in_prescan_holdout("job-42", "tgt-9", 0.5)
    assert a == in_prescan_holdout("job-42", "tgt-9", 0.5)


def test_holdout_fraction_is_approximately_right() -> None:
    frac = 0.1
    n = 5000
    hits = sum(in_prescan_holdout(f"job-{i}", "tgt", frac) for i in range(n))
    # ~10% with tolerance for the hash distribution.
    assert 0.07 * n < hits < 0.13 * n
