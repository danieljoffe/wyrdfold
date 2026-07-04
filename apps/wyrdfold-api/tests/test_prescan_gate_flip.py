"""Pre-scan cosine gate FLIP (#90): batch verdicts + exploration holdout.

`cosine_gate_admits_batch` returns per-job admit/drop/None ("no opinion",
fail-open) for one target in two queries. `in_prescan_holdout` is the
deterministic slice of would-drop jobs kept for grading so the false-negative
rate stays measurable. The poller-side wiring is exercised in
`test_phase2_runner.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.models.targets import JobTarget, ScoringProfile
from app.services.embeddings.prescan_gate import (
    cosine_gate_admits_batch,
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
    out = await cosine_gate_admits_batch(sb, _target(), ["hi", "lo"])
    assert out == {"hi": True, "lo": False}


@pytest.mark.asyncio
async def test_batch_missing_job_vector_is_none_failopen() -> None:
    sb = _SB(target_rows=_CALIBRATED, job_rows=[{"job_posting_id": "hi", "embedding": _NEAR}])
    out = await cosine_gate_admits_batch(sb, _target(), ["hi", "novec"])
    assert out == {"hi": True, "novec": None}  # missing vector → no opinion (admit)


@pytest.mark.asyncio
async def test_batch_uncalibrated_target_all_none() -> None:
    # No embedding / threshold on the target → gate has no opinion for anything.
    sb = _SB(target_rows=[{"embedding": None, "prescan_cosine_threshold": None}], job_rows=[])
    out = await cosine_gate_admits_batch(sb, _target(), ["a", "b"])
    assert out == {"a": None, "b": None}


@pytest.mark.asyncio
async def test_batch_dim_mismatch_is_none() -> None:
    sb = _SB(
        target_rows=_CALIBRATED,
        job_rows=[{"job_posting_id": "bad", "embedding": [1.0, 0.0]}],  # 2-dim vs 3
    )
    out = await cosine_gate_admits_batch(sb, _target(), ["bad"])
    assert out == {"bad": None}


@pytest.mark.asyncio
async def test_batch_error_is_failopen() -> None:
    sb = _SB(target_rows=_CALIBRATED, job_rows=[], raise_=True)
    out = await cosine_gate_admits_batch(sb, _target(), ["a", "b"])
    assert out == {"a": None, "b": None}


@pytest.mark.asyncio
async def test_batch_empty_ids() -> None:
    sb = _SB(target_rows=_CALIBRATED, job_rows=[])
    assert await cosine_gate_admits_batch(sb, _target(), []) == {}


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
