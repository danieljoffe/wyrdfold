"""Pre-scan cosine gate decision (#60/#68, Phase 3 — SHADOW MODE).

Covers ``app/services/embeddings/prescan_gate.py``:

- ``cosine_gate_decision`` admits / drops across the per-target threshold
  (``admit = cosine >= threshold``) and returns the actual cosine.
- Fail-soft on EACH missing input — no job vector, no target embedding, a NULL
  threshold, an empty result set, a dim mismatch, or a raising client — all
  yield ``(None, None)`` ("no opinion") and never raise.
- ``parse_vector`` coerces the two pgvector wire shapes (list / text) and
  rejects garbage.

The gate was RETIRED in R2 on its own shadow data (docs/decisions.md
2026-07-30) and has no app-code callers; these tests pin the computation only.
The companion poller-inertness suite and the ``prescan_shadow`` table it
observed are both gone (R3 §1, #557) — this module is the last of the gate
still standing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.models.targets import JobTarget, ScoringProfile
from app.services.embeddings.prescan_gate import parse_vector


def _target(target_id: str = "tgt-1") -> JobTarget:
    now = datetime(2026, 6, 24, tzinfo=UTC)
    return JobTarget(
        id=target_id,
        label="Staff Frontend Engineer",
        scoring_profile=ScoringProfile(),
        search_keywords=["React", "TypeScript"],
        app_active=True,
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


@pytest.mark.asyncio
async def test_vector_batch_read_chunks_stay_url_safe(monkeypatch) -> None:
    """Regression for the 2026-07-15 backfill find: the vectors read rode ONE
    ``.in_()`` with the whole candidate set — thousands of UUIDs build a URL
    httpx itself refuses (``InvalidURL: query too long``), and the gate's
    fail-closed contract then silently deferred every batch. Reads must chunk
    at ``_VECTOR_READ_CHUNK_SIZE``."""
    from unittest.mock import MagicMock

    from app.services.embeddings import prescan_gate as pg

    seen_chunks: list[int] = []

    class _Chain:
        def select(self, *_a, **_k):
            return self

        def in_(self, _col, ids):
            seen_chunks.append(len(ids))
            return self

        def eq(self, *_a, **_k):
            return self

        async def execute(self):
            return MagicMock(data=[])

    sb = MagicMock()
    sb.table.return_value = _Chain()

    ids = [f"00000000-0000-4000-8000-{i:012d}" for i in range(400)]
    out = await pg._fetch_job_vectors_batch(sb, job_ids=ids, model="m")

    assert out == {}
    assert len(seen_chunks) == 3  # 400 at chunk 150 → 150+150+100
    assert all(n <= pg._VECTOR_READ_CHUNK_SIZE for n in seen_chunks)
    assert sum(seen_chunks) == 400


# ---------------------------------------------------------------------------
# cosine_scores_batch — the surviving (ordering) read path. Load-bearing since
# R2: it feeds the Phase-2 grade-queue priority unconditionally, so it gets
# direct coverage now that the gate tests it used to hide behind are gone.


def _vec_supabase(target_vec: object, job_rows: list[dict]) -> object:
    from unittest.mock import MagicMock

    sb = MagicMock()

    class _Targets:
        def select(self, *_a, **_k):
            return self

        def eq(self, *_a, **_k):
            return self

        def limit(self, *_a, **_k):
            return self

        async def execute(self):
            return MagicMock(data=[{"embedding": target_vec}] if target_vec is not None else [])

    class _Vectors:
        def select(self, *_a, **_k):
            return self

        def in_(self, *_a, **_k):
            return self

        def eq(self, *_a, **_k):
            return self

        async def execute(self):
            return MagicMock(data=job_rows)

    def _table(name: str):
        return _Targets() if name == "targets" else _Vectors()

    sb.table.side_effect = _table
    return sb


@pytest.mark.asyncio
async def test_cosine_scores_batch_returns_similarity_per_job() -> None:
    from app.services.embeddings.prescan_gate import cosine_scores_batch

    sb = _vec_supabase(
        [1.0, 0.0],
        [
            {"job_posting_id": "j-same", "embedding": [1.0, 0.0]},
            {"job_posting_id": "j-orth", "embedding": [0.0, 1.0]},
        ],
    )
    out = await cosine_scores_batch(sb, _target(), ["j-same", "j-orth"])

    assert out["j-same"] == pytest.approx(1.0)
    assert out["j-orth"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_cosine_scores_batch_fails_open_without_target_vector() -> None:
    """No target embedding → {} — the runner's sort falls back to its
    tie-breakers; ordering degradation must never drop candidates."""
    from app.services.embeddings.prescan_gate import cosine_scores_batch

    sb = _vec_supabase(None, [])
    assert await cosine_scores_batch(sb, _target(), ["j-1"]) == {}


@pytest.mark.asyncio
async def test_cosine_scores_batch_omits_dim_mismatch() -> None:
    from app.services.embeddings.prescan_gate import cosine_scores_batch

    sb = _vec_supabase([1.0, 0.0], [{"job_posting_id": "j-bad", "embedding": [1.0, 0.0, 0.0]}])
    assert await cosine_scores_batch(sb, _target(), ["j-bad"]) == {}
