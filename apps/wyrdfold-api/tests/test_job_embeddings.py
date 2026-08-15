"""Pre-scan job-embedding write path (#60, Phase 1).

Covers ``app/services/embeddings/job_embeddings.py``:
``embed_text_for_job`` (truncation + cleaning + empties), ``content_hash``,
and ``upsert_job_embedding`` (cache-hit skip, embed+write, cost-log, edges,
fail-soft). The Supabase client is a hand-rolled fake that records the
upsert payload and serves a configurable existing content_hash.
"""

from __future__ import annotations

from typing import Any

from app.constants import SYSTEM_USER_ID
from app.services.embeddings.job_embeddings import (
    DEFAULT_MODEL,
    EMBED_DIMENSIONS,
    JOB_EMBED_PURPOSE,
    content_hash,
    embed_text_for_job,
    ensure_job_vectors,
    upsert_job_embedding,
)
from app.services.embeddings.mock import MockEmbeddingsClient

# ---------------------------------------------------------------------------
# embed_text_for_job + content_hash (pure)
# ---------------------------------------------------------------------------


def test_embed_text_combines_title_and_clean_description() -> None:
    text = embed_text_for_job("Senior Engineer", "<p>Build <b>things</b></p>")
    assert text == "Senior Engineer\nBuild things"


def test_embed_text_truncates_description_to_4000_chars() -> None:
    body = "<p>" + ("x" * 5000) + "</p>"
    text = embed_text_for_job("T", body)
    # title + "\n" + 4000 chars of cleaned body
    assert text.startswith("T\n")
    assert len(text) == len("T\n") + 4000


def test_embed_text_truncation_is_over_cleaned_text_not_markup() -> None:
    # 6000 chars of real text wrapped in markup → cleaned first, then capped at
    # 4000, so the cap counts real characters (markup is not in the budget).
    body = "<div>" + ("a" * 6000) + "</div>"
    text = embed_text_for_job("", body)
    assert text == "\n" + ("a" * 4000)


def test_embed_text_empty_title_and_description() -> None:
    assert embed_text_for_job("", "") == "\n"
    assert embed_text_for_job(None, None) == "\n"


def test_embed_text_title_only() -> None:
    assert embed_text_for_job("Just A Title", None) == "Just A Title\n"


def test_content_hash_is_stable_and_sensitive() -> None:
    a = content_hash("hello world")
    assert a == content_hash("hello world")
    assert a != content_hash("hello world!")
    assert len(a) == 64  # sha256 hex


# ---------------------------------------------------------------------------
# Fake Supabase
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, data: Any) -> None:
        self.data = data


class _FakeQuery:
    """Records the operation and returns the configured rows on execute()."""

    def __init__(self, table: _FakeTable, op: str) -> None:
        self._table = table
        self._op = op
        self._payload: Any = None

    def select(self, *_a: Any, **_k: Any) -> _FakeQuery:
        return self

    def eq(self, *_a: Any, **_k: Any) -> _FakeQuery:
        return self

    def limit(self, *_a: Any, **_k: Any) -> _FakeQuery:
        return self

    def upsert(self, payload: Any, **_k: Any) -> _FakeQuery:
        self._payload = payload
        return self

    def insert(self, payload: Any, **_k: Any) -> _FakeQuery:
        self._payload = payload
        return self

    def execute(self) -> _FakeResp:
        if self._op == "select":
            return _FakeResp(self._table.existing_rows)
        # write op — record the payload, then return a row shaped like what
        # Postgres RETURNING gives (server-default id + created_at), so
        # cost_log._insert_row's LLMCallRecord.model_validate succeeds.
        self._table.upserts.append(self._payload)
        returned = dict(self._payload) if isinstance(self._payload, dict) else {}
        returned.setdefault("id", "row-0")
        returned.setdefault("created_at", "2026-06-24T00:00:00+00:00")
        return _FakeResp([returned])


class _FakeTable:
    def __init__(self) -> None:
        self.existing_rows: list[dict[str, Any]] = []
        self.upserts: list[Any] = []

    def select(self, *_a: Any, **_k: Any) -> _FakeQuery:
        return _FakeQuery(self, "select").select()

    def upsert(self, payload: Any, **k: Any) -> _FakeQuery:
        return _FakeQuery(self, "upsert").upsert(payload, **k)

    def insert(self, payload: Any, **k: Any) -> _FakeQuery:
        return _FakeQuery(self, "insert").insert(payload, **k)


class _FakeSupabase:
    def __init__(self) -> None:
        self.job_embeddings = _FakeTable()
        self.llm_costs = _FakeTable()
        self._tables = {
            "job_embeddings": self.job_embeddings,
            "llm_costs": self.llm_costs,
        }

    def table(self, name: str) -> _FakeTable:
        return self._tables[name]


# ---------------------------------------------------------------------------
# upsert_job_embedding
# ---------------------------------------------------------------------------


async def test_new_job_is_embedded_and_written() -> None:
    sb = _FakeSupabase()
    client = MockEmbeddingsClient()

    status = await upsert_job_embedding(
        sb,  # type: ignore[arg-type]
        client,
        job_id="job-1",
        title="Frontend Engineer",
        description_html="<p>React, TypeScript</p>",
    )

    assert status == "embedded"
    # One embed call, document side.
    assert len(client.calls) == 1
    assert client.calls[0]["input_type"] == "document"
    # One vector row written, with the right key + hash + an EMBED_DIMENSIONS-d vector.
    assert len(sb.job_embeddings.upserts) == 1
    row = sb.job_embeddings.upserts[0]
    assert row["job_posting_id"] == "job-1"
    # Tracks the module default (voyage-3.5 since the voyage-3 retirement) —
    # the row must be keyed by whatever model actually embedded it.
    assert row["model"] == DEFAULT_MODEL
    assert row["content_hash"] == content_hash(
        embed_text_for_job("Frontend Engineer", "<p>React, TypeScript</p>")
    )
    assert len(row["embedding"]) == EMBED_DIMENSIONS  # 512-d Matryoshka space
    # Cost row logged under the pre-scan purpose, instance key.
    assert len(sb.llm_costs.upserts) == 1
    cost_row = sb.llm_costs.upserts[0]
    assert cost_row["purpose"] == JOB_EMBED_PURPOSE
    # Cron-authored embedding cost → the SYSTEM principal, not NULL (#88 groundwork).
    assert cost_row["user_id"] == SYSTEM_USER_ID


async def test_unchanged_content_is_a_cache_hit_no_embed() -> None:
    sb = _FakeSupabase()
    client = MockEmbeddingsClient()
    # Stored hash matches the text we'll compute → skip.
    text = embed_text_for_job("Same Title", "<p>same body</p>")
    sb.job_embeddings.existing_rows = [{"content_hash": content_hash(text)}]

    status = await upsert_job_embedding(
        sb,  # type: ignore[arg-type]
        client,
        job_id="job-1",
        title="Same Title",
        description_html="<p>same body</p>",
    )

    assert status == "cache_hit"
    assert client.calls == []  # NO embed call
    assert sb.job_embeddings.upserts == []  # NO write
    assert sb.llm_costs.upserts == []  # NO cost row


async def test_changed_content_re_embeds() -> None:
    sb = _FakeSupabase()
    client = MockEmbeddingsClient()
    # Stored hash is for the OLD body → mismatch → re-embed.
    old = embed_text_for_job("Title", "<p>old body</p>")
    sb.job_embeddings.existing_rows = [{"content_hash": content_hash(old)}]

    status = await upsert_job_embedding(
        sb,  # type: ignore[arg-type]
        client,
        job_id="job-1",
        title="Title",
        description_html="<p>NEW body</p>",
    )

    assert status == "embedded"
    assert len(client.calls) == 1
    assert len(sb.job_embeddings.upserts) == 1


async def test_empty_title_and_description_is_skipped_without_embed() -> None:
    sb = _FakeSupabase()
    client = MockEmbeddingsClient()

    status = await upsert_job_embedding(
        sb,  # type: ignore[arg-type]
        client,
        job_id="job-1",
        title="",
        description_html="",
    )

    assert status == "skipped_empty"
    assert client.calls == []
    assert sb.job_embeddings.upserts == []


async def test_null_title_and_description_is_skipped() -> None:
    sb = _FakeSupabase()
    client = MockEmbeddingsClient()

    status = await upsert_job_embedding(
        sb,  # type: ignore[arg-type]
        client,
        job_id="job-1",
        title=None,
        description_html=None,
    )

    assert status == "skipped_empty"
    assert client.calls == []


async def test_title_only_with_empty_description_is_embedded() -> None:
    sb = _FakeSupabase()
    client = MockEmbeddingsClient()

    status = await upsert_job_embedding(
        sb,  # type: ignore[arg-type]
        client,
        job_id="job-1",
        title="Only A Title",
        description_html=None,
    )

    assert status == "embedded"
    assert len(client.calls) == 1


class _BoomClient:
    """Embeds raise — exercises the fail-soft path."""

    async def embed(self, **_k: Any) -> Any:
        raise RuntimeError("voyage down")


async def test_embed_failure_is_swallowed_and_returns_error() -> None:
    sb = _FakeSupabase()

    status = await upsert_job_embedding(
        sb,  # type: ignore[arg-type]
        _BoomClient(),  # type: ignore[arg-type]
        job_id="job-1",
        title="Engineer",
        description_html="<p>body</p>",
    )

    assert status == "error"  # no raise
    assert sb.job_embeddings.upserts == []  # nothing written


# ---------------------------------------------------------------------------
# ensure_job_vectors (lazy grade-time materialization — Disk IO slim-down)
# ---------------------------------------------------------------------------


class _VectorStoreQuery:
    def __init__(self, store: _VectorStoreSupabase, table: str) -> None:
        self._store = store
        self._table = table
        self._write: Any = None

    def select(self, *_a: Any, **_k: Any) -> _VectorStoreQuery:
        return self

    def eq(self, *_a: Any, **_k: Any) -> _VectorStoreQuery:
        return self

    def in_(self, *_a: Any, **_k: Any) -> _VectorStoreQuery:
        return self

    def limit(self, *_a: Any, **_k: Any) -> _VectorStoreQuery:
        return self

    def upsert(self, payload: Any, **_k: Any) -> _VectorStoreQuery:
        self._write = payload
        return self

    def insert(self, payload: Any, **_k: Any) -> _VectorStoreQuery:
        self._write = payload
        return self

    def execute(self) -> _FakeResp:
        if self._write is not None:
            rows = self._write if isinstance(self._write, list) else [self._write]
            if self._table == "job_embeddings":
                self._store.vectors.extend(dict(r) for r in rows)
            returned = [
                {**dict(r), "id": "row-0", "created_at": "2026-06-24T00:00:00+00:00"} for r in rows
            ]
            return _FakeResp(returned)
        if self._table == "job_embeddings":
            return _FakeResp(list(self._store.vectors))
        return _FakeResp([])


class _VectorStoreSupabase:
    """A fake whose SELECTs see earlier upserts — the re-fetch after the lazy
    embed must find exactly what ``embed_jobs_batch`` just wrote."""

    def __init__(self) -> None:
        self.vectors: list[dict[str, Any]] = []

    def table(self, name: str) -> _VectorStoreQuery:
        return _VectorStoreQuery(self, name)


async def test_ensure_returns_existing_vector_without_embedding() -> None:
    sb = _VectorStoreSupabase()
    sb.vectors.append(
        {"job_posting_id": "j-hit", "model": DEFAULT_MODEL, "embedding": "[0.5, 0.25]"}
    )
    client = MockEmbeddingsClient()

    out = await ensure_job_vectors(
        sb,  # type: ignore[arg-type]
        client,
        [{"id": "j-hit", "title": "T", "description_html": "<p>b</p>"}],
    )

    assert out == {"j-hit": [0.5, 0.25]}
    assert client.calls == []  # cache hit — no embed spend


async def test_ensure_materializes_missing_vectors_on_demand() -> None:
    sb = _VectorStoreSupabase()
    client = MockEmbeddingsClient()

    out = await ensure_job_vectors(
        sb,  # type: ignore[arg-type]
        client,
        [{"id": "j-miss", "title": "Engineer", "description_html": "<p>React</p>"}],
    )

    assert set(out) == {"j-miss"}
    assert len(out["j-miss"]) == EMBED_DIMENSIONS
    # The lazy embed is the batched path at the pinned Matryoshka size.
    assert len(client.calls) == 1
    assert client.calls[0]["output_dimension"] == EMBED_DIMENSIONS
    # The vector landed in the store (subsequent ensures are cache hits).
    assert any(v["job_posting_id"] == "j-miss" for v in sb.vectors)


async def test_ensure_is_fail_soft_when_embedding_errors() -> None:
    class _Boom:
        async def embed(self, **_k: Any) -> Any:
            raise RuntimeError("provider down")

    sb = _VectorStoreSupabase()
    out = await ensure_job_vectors(
        sb,  # type: ignore[arg-type]
        _Boom(),  # type: ignore[arg-type]
        [{"id": "j-1", "title": "T", "description_html": "<p>b</p>"}],
    )
    assert out == {}  # absent → callers fail open; never raises
