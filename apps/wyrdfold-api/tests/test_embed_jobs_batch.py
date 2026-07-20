"""Batched embed path (`embed_jobs_batch`) — the sweep's IO diet.

Verifies the batching arithmetic (one Voyage call per 96 texts, 8-row write
chunks), no-spend on empty rows, and the write circuit breaker: consecutive
chunk failures abort the WHOLE run — including further Voyage calls — so a
throttled disk can't turn the sweep into a paid retry loop (2026-07-12:
3,550 embeds bought for ~680 landed vectors).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.services.embeddings.job_embeddings import embed_jobs_batch
from app.services.embeddings.mock import MockEmbeddingsClient


class _FakeTable:
    def __init__(self, sb: _FakeSupabase):
        self._sb = sb
        self._pending: list[dict[str, Any]] | None = None

    def upsert(self, rows: list[dict[str, Any]], **_: Any) -> _FakeTable:
        self._pending = rows
        return self

    def execute(self) -> SimpleNamespace:
        if self._sb.next_write_fails():
            raise RuntimeError("statement timeout")
        self._sb.vector_writes.append(self._pending)
        return SimpleNamespace(data=self._pending)


class _FakeSupabase:
    """``fail_plan`` scripts write outcomes: one bool per write attempt
    (True = fail), False once exhausted."""

    def __init__(self, fail_plan: list[bool] | None = None):
        self.vector_writes: list[list[dict[str, Any]]] = []
        self._fail_plan = list(fail_plan or [])

    def next_write_fails(self) -> bool:
        return self._fail_plan.pop(0) if self._fail_plan else False

    def table(self, name: str) -> _FakeTable:
        assert name == "job_embeddings"
        return _FakeTable(self)


@pytest.fixture(autouse=True)
def _cost_log(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Cost logging is not the unit under test — record calls, skip the DB."""
    recorded: list[dict[str, Any]] = []

    def fake_record(_sb: Any, **kwargs: Any) -> None:
        recorded.append(kwargs)

    monkeypatch.setattr(
        "app.services.embeddings.job_embeddings.cost_log.record_embedding",
        fake_record,
    )
    return recorded


def _rows(n: int) -> list[dict[str, Any]]:
    return [
        {"id": f"j{i}", "title": f"Job {i}", "description_html": "<p>desc</p>"} for i in range(n)
    ]


class TestEmbedJobsBatch:
    @pytest.mark.asyncio
    async def test_batches_calls_and_chunks_writes(self) -> None:
        sb = _FakeSupabase()
        client = MockEmbeddingsClient()

        counts = await embed_jobs_batch(sb, client, _rows(100))

        assert counts == {
            "embedded": 100,
            "skipped_empty": 0,
            "error": 0,
            "aborted": 0,
        }
        # 100 rows → 2 Voyage calls (96 + 4), not 100.
        assert len(client.calls) == 2
        assert client.calls[0]["input_count"] == 96
        assert client.calls[1]["input_count"] == 4
        # Vector writes chunked at 8 (~160KB/statement — the size that stays
        # under the statement timeout on the small prod instance).
        chunk_sizes = [len(w) for w in sb.vector_writes]
        assert chunk_sizes == [8] * 12 + [4]
        # Every written row carries the model key + a hash + a vector.
        sample = sb.vector_writes[0][0]
        assert set(sample) == {"job_posting_id", "model", "content_hash", "embedding"}

    @pytest.mark.asyncio
    async def test_empty_rows_are_skipped_without_spend(self) -> None:
        sb = _FakeSupabase()
        client = MockEmbeddingsClient()
        rows = [{"id": "empty", "title": "", "description_html": None}]

        counts = await embed_jobs_batch(sb, client, rows)

        assert counts == {
            "embedded": 0,
            "skipped_empty": 1,
            "error": 0,
            "aborted": 0,
        }
        assert client.calls == []  # no Voyage spend
        assert sb.vector_writes == []

    @pytest.mark.asyncio
    async def test_breaker_trips_on_consecutive_failures_and_defers_rest(
        self,
    ) -> None:
        # Every write fails: chunk 1 fails (1/2), chunk 2 fails (2/2) → abort.
        sb = _FakeSupabase(fail_plan=[True] * 10)
        client = MockEmbeddingsClient()

        counts = await embed_jobs_batch(sb, client, _rows(30))

        assert counts["embedded"] == 0
        assert counts["error"] == 16  # two failed 8-row chunks
        assert counts["aborted"] == 14  # never attempted — deferred, not spent
        assert len(client.calls) == 1  # the one embed call before the trip

    @pytest.mark.asyncio
    async def test_breaker_stops_further_voyage_spend(self) -> None:
        # 200 rows = 3 embed batches. Writes always fail → the breaker trips
        # inside batch 1 and batches 2-3 must NOT be embedded (that's the paid
        # retry loop this exists to break).
        sb = _FakeSupabase(fail_plan=[True] * 50)
        client = MockEmbeddingsClient()

        counts = await embed_jobs_batch(sb, client, _rows(200))

        assert len(client.calls) == 1  # no further spend after the trip
        assert counts["aborted"] == 200 - 16  # everything past the 2 failed chunks

    @pytest.mark.asyncio
    async def test_non_consecutive_failures_do_not_trip(self) -> None:
        # Alternating fail/success never reaches 2 consecutive → run completes.
        sb = _FakeSupabase(fail_plan=[True, False, True, False, True, False])
        client = MockEmbeddingsClient()

        counts = await embed_jobs_batch(sb, client, _rows(48))  # 6 chunks of 8

        assert counts["aborted"] == 0
        assert counts["error"] == 24  # 3 failed chunks
        assert counts["embedded"] == 24  # 3 landed chunks
        assert len(client.calls) == 1

    @pytest.mark.asyncio
    async def test_embed_failure_isolated_to_its_batch(self) -> None:
        sb = _FakeSupabase()

        class _FlakyClient(MockEmbeddingsClient):
            def __init__(self) -> None:
                super().__init__()
                self._first = True

            async def embed(self, **kwargs: Any) -> Any:
                if self._first:
                    self._first = False
                    raise RuntimeError("voyage down")
                return await super().embed(**kwargs)

        counts = await embed_jobs_batch(sb, _FlakyClient(), _rows(100))

        # First batch (96) lost, second (4) landed; Voyage errors don't feed
        # the WRITE breaker (they carry no spend-for-nothing risk).
        assert counts["error"] == 96
        assert counts["embedded"] == 4
        assert counts["aborted"] == 0
