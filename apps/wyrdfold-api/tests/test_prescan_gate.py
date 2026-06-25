"""Pre-scan cosine gate decision (#60/#68, Phase 3 — SHADOW MODE).

Covers ``app/services/embeddings/prescan_gate.py``:

- ``cosine_gate_decision`` admits / drops across the per-target threshold
  (``admit = cosine >= threshold``) and returns the actual cosine.
- Fail-soft on EACH missing input — no job vector, no target embedding, a NULL
  threshold, an empty result set, a dim mismatch, or a raising client — all
  yield ``(None, None)`` ("no opinion") and never raise.
- ``parse_vector`` coerces the two pgvector wire shapes (list / text) and
  rejects garbage.

The gate is OBSERVATION ONLY in this phase — these tests pin the computation;
the poller-side inertness lives in ``test_poller_prescan_shadow.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.models.targets import JobTarget, ScoringProfile
from app.services.embeddings import prescan_gate
from app.services.embeddings.prescan_gate import cosine_gate_decision, parse_vector


def _target(target_id: str = "tgt-1") -> JobTarget:
    now = datetime(2026, 6, 24, tzinfo=UTC)
    return JobTarget(
        id=target_id,
        label="Staff Frontend Engineer",
        scoring_profile=ScoringProfile(),
        search_keywords=["React", "TypeScript"],
        is_active=True,
        created_at=now,
        updated_at=now,
    )


class _FakeQuery:
    """Records the chained calls and returns a pre-set response on execute()."""

    def __init__(self, response: Any, calls: list[str]) -> None:
        self._response = response
        self._calls = calls

    def select(self, cols: str) -> _FakeQuery:
        self._calls.append(f"select:{cols}")
        return self

    def eq(self, col: str, val: Any) -> _FakeQuery:
        self._calls.append(f"eq:{col}={val}")
        return self

    def limit(self, n: int) -> _FakeQuery:
        return self

    def execute(self) -> Any:
        return self._response


class _Resp:
    def __init__(self, data: Any) -> None:
        self.data = data


class _FakeSupabase:
    """Serves configurable rows per table; records which tables were hit.

    ``job_rows`` answers ``job_embeddings`` selects, ``target_rows`` answers
    ``targets`` selects. ``raise_on`` forces a table's query to raise (to drive
    the swallow-everything path).
    """

    def __init__(
        self,
        *,
        job_rows: Any = None,
        target_rows: Any = None,
        raise_on: str | None = None,
    ) -> None:
        self._job_rows = job_rows
        self._target_rows = target_rows
        self._raise_on = raise_on
        self.tables_hit: list[str] = []
        self.calls: list[str] = []

    def table(self, name: str) -> _FakeQuery:
        self.tables_hit.append(name)
        if name == self._raise_on:
            raise RuntimeError(f"boom: {name}")
        rows = self._job_rows if name == "job_embeddings" else self._target_rows
        return _FakeQuery(_Resp(rows), self.calls)


_VEC_A = [1.0, 0.0, 0.0]
_VEC_NEAR = [0.9, 0.1, 0.0]  # cosine to _VEC_A ~0.994
_VEC_FAR = [0.0, 1.0, 0.0]  # cosine to _VEC_A == 0.0


# --------------------------------------------------------------------------- #
# parse_vector
# --------------------------------------------------------------------------- #
def test_parse_vector_list() -> None:
    assert parse_vector([1, 2, 3]) == [1.0, 2.0, 3.0]


def test_parse_vector_text_form() -> None:
    assert parse_vector("[0.1, 0.2, 0.3]") == [0.1, 0.2, 0.3]


def test_parse_vector_none_and_garbage() -> None:
    assert parse_vector(None) is None
    assert parse_vector("not a vector") is None
    assert parse_vector("{1,2}") is None  # set literal, not list/tuple
    assert parse_vector(["x", "y"]) is None  # non-numeric list


# --------------------------------------------------------------------------- #
# cosine_gate_decision — admit / drop across the threshold
# --------------------------------------------------------------------------- #
async def test_admits_when_cosine_at_or_above_threshold() -> None:
    sb = _FakeSupabase(
        job_rows=[{"embedding": _VEC_A}],
        target_rows=[{"embedding": _VEC_NEAR, "prescan_cosine_threshold": 0.5}],
    )
    cosine, admit = await cosine_gate_decision(sb, job_id="job-1", target=_target())
    assert cosine is not None and cosine > 0.99
    assert admit is True


async def test_drops_when_cosine_below_threshold() -> None:
    sb = _FakeSupabase(
        job_rows=[{"embedding": _VEC_A}],
        target_rows=[{"embedding": _VEC_FAR, "prescan_cosine_threshold": 0.5}],
    )
    cosine, admit = await cosine_gate_decision(sb, job_id="job-1", target=_target())
    assert cosine == pytest.approx(0.0)
    assert admit is False


async def test_admit_is_inclusive_at_exact_threshold() -> None:
    # cosine(_VEC_A, _VEC_A) == 1.0; threshold 1.0 ⇒ admit (>= is inclusive).
    sb = _FakeSupabase(
        job_rows=[{"embedding": _VEC_A}],
        target_rows=[{"embedding": _VEC_A, "prescan_cosine_threshold": 1.0}],
    )
    cosine, admit = await cosine_gate_decision(sb, job_id="job-1", target=_target())
    assert cosine == pytest.approx(1.0)
    assert admit is True


# --------------------------------------------------------------------------- #
# fail-soft on each missing input ⇒ (None, None)
# --------------------------------------------------------------------------- #
async def test_failsoft_no_job_vector() -> None:
    sb = _FakeSupabase(
        job_rows=[],  # no row for this (job, model)
        target_rows=[{"embedding": _VEC_NEAR, "prescan_cosine_threshold": 0.5}],
    )
    assert await cosine_gate_decision(sb, job_id="job-1", target=_target()) == (None, None)
    # Short-circuits before touching targets.
    assert sb.tables_hit == ["job_embeddings"]


async def test_failsoft_no_target_embedding() -> None:
    sb = _FakeSupabase(
        job_rows=[{"embedding": _VEC_A}],
        target_rows=[{"embedding": None, "prescan_cosine_threshold": 0.5}],
    )
    assert await cosine_gate_decision(sb, job_id="job-1", target=_target()) == (None, None)


async def test_failsoft_null_threshold() -> None:
    sb = _FakeSupabase(
        job_rows=[{"embedding": _VEC_A}],
        target_rows=[{"embedding": _VEC_NEAR, "prescan_cosine_threshold": None}],
    )
    assert await cosine_gate_decision(sb, job_id="job-1", target=_target()) == (None, None)


async def test_failsoft_no_target_row() -> None:
    sb = _FakeSupabase(job_rows=[{"embedding": _VEC_A}], target_rows=[])
    assert await cosine_gate_decision(sb, job_id="job-1", target=_target()) == (None, None)


async def test_failsoft_dim_mismatch() -> None:
    sb = _FakeSupabase(
        job_rows=[{"embedding": [1.0, 0.0]}],  # 2-dim
        target_rows=[{"embedding": _VEC_NEAR, "prescan_cosine_threshold": 0.5}],  # 3-dim
    )
    assert await cosine_gate_decision(sb, job_id="job-1", target=_target()) == (None, None)


async def test_failsoft_client_raises() -> None:
    sb = _FakeSupabase(raise_on="job_embeddings")
    # Any unexpected error is swallowed — never propagates to the (best-effort) caller.
    assert await cosine_gate_decision(sb, job_id="job-1", target=_target()) == (None, None)


async def test_uses_default_model_in_query() -> None:
    sb = _FakeSupabase(
        job_rows=[{"embedding": _VEC_A}],
        target_rows=[{"embedding": _VEC_A, "prescan_cosine_threshold": 0.5}],
    )
    await cosine_gate_decision(sb, job_id="job-xyz", target=_target("tgt-9"))
    # The job lookup is keyed by (job_posting_id, model) and the target by id.
    assert "eq:job_posting_id=job-xyz" in sb.calls
    assert f"eq:model={prescan_gate.DEFAULT_MODEL}" in sb.calls
    assert "eq:id=tgt-9" in sb.calls
