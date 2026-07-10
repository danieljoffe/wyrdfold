"""Batched embed path (`embed_jobs_batch`) — the sweep's IO diet.

Verifies the batching arithmetic that makes a 200-job sweep ~15 IO
operations instead of ~800: one Voyage call + one cost row per 96 texts,
vector writes in 24-row chunks, empty rows skipped without spend, and
failures isolated to their own batch/chunk.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.services.embeddings.job_embeddings import embed_jobs_batch
from app.services.embeddings.mock import MockEmbeddingsClient


class _FakeTable:
    def __init__(self, log: list[Any], fail_writes: bool = False):
        self._log = log
        self._fail = fail_writes
        self._pending: list[dict[str, Any]] | None = None

    def upsert(self, rows: list[dict[str, Any]], **_: Any) -> _FakeTable:
        self._pending = rows
        return self

    def execute(self) -> SimpleNamespace:
        if self._fail:
            raise RuntimeError("write failed")
        self._log.append(self._pending)
        return SimpleNamespace(data=self._pending)


class _FakeSupabase:
    def __init__(self, fail_writes: bool = False):
        self.vector_writes: list[list[dict[str, Any]]] = []
        self._fail = fail_writes

    def table(self, name: str) -> _FakeTable:
        assert name == "job_embeddings"
        return _FakeTable(self.vector_writes, fail_writes=self._fail)


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
        {"id": f"j{i}", "title": f"Job {i}", "description_html": "<p>desc</p>"}
        for i in range(n)
    ]


class TestEmbedJobsBatch:
    @pytest.mark.asyncio
    async def test_batches_calls_and_chunks_writes(self) -> None:
        sb = _FakeSupabase()
        client = MockEmbeddingsClient()

        counts = await embed_jobs_batch(sb, client, _rows(100))

        assert counts == {"embedded": 100, "skipped_empty": 0, "error": 0}
        # 100 rows → 2 Voyage calls (96 + 4), not 100.
        assert len(client.calls) == 2
        assert client.calls[0]["input_count"] == 96
        assert client.calls[1]["input_count"] == 4
        # Vector writes chunked at 24: 96 → 4 chunks, then 4 → 1 chunk.
        chunk_sizes = [len(w) for w in sb.vector_writes]
        assert chunk_sizes == [24, 24, 24, 24, 4]
        # Every written row carries the model key + a hash + a vector.
        sample = sb.vector_writes[0][0]
        assert set(sample) == {"job_posting_id", "model", "content_hash", "embedding"}

    @pytest.mark.asyncio
    async def test_empty_rows_are_skipped_without_spend(self) -> None:
        sb = _FakeSupabase()
        client = MockEmbeddingsClient()
        rows = [{"id": "empty", "title": "", "description_html": None}]

        counts = await embed_jobs_batch(sb, client, rows)

        assert counts == {"embedded": 0, "skipped_empty": 1, "error": 0}
        assert client.calls == []  # no Voyage spend
        assert sb.vector_writes == []

    @pytest.mark.asyncio
    async def test_write_failures_lose_only_their_chunk(self) -> None:
        sb = _FakeSupabase(fail_writes=True)
        client = MockEmbeddingsClient()

        counts = await embed_jobs_batch(sb, client, _rows(30))

        # Embeds succeeded; both write chunks (24 + 6) failed independently.
        assert counts["embedded"] == 0
        assert counts["error"] == 30
        assert len(client.calls) == 1  # spend happened once, not retried

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

        # First batch (96) lost, second (4) landed.
        assert counts["error"] == 96
        assert counts["embedded"] == 4
