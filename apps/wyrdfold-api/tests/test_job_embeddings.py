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
    JOB_EMBED_PURPOSE,
    content_hash,
    embed_text_for_job,
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
    # One vector row written, with the right key + hash + a 1024-d vector.
    assert len(sb.job_embeddings.upserts) == 1
    row = sb.job_embeddings.upserts[0]
    assert row["job_posting_id"] == "job-1"
    assert row["model"] == "voyage-3"
    assert row["content_hash"] == content_hash(
        embed_text_for_job("Frontend Engineer", "<p>React, TypeScript</p>")
    )
    assert len(row["embedding"]) == 1024
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
