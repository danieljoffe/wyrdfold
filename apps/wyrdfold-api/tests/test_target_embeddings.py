"""Pre-scan target-embedding write path (#60, Phase 2).

Covers ``app/services/embeddings/target_embeddings.py``: ``embed_text_for_target``
(label + keywords, NO description), ``content_hash``, and
``upsert_target_embedding`` (cache-hit skip, embed+write with the QUERY input
type, cost-log, fail-soft). Mirrors ``test_job_embeddings.py`` — a hand-rolled
fake Supabase records the update payload and serves a configurable existing hash.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.constants import SYSTEM_USER_ID
from app.models.targets import JobTarget, ScoringProfile
from app.services.embeddings.mock import MockEmbeddingsClient
from app.services.embeddings.target_embeddings import (
    TARGET_EMBED_PURPOSE,
    content_hash,
    embed_text_for_target,
    upsert_target_embedding,
)


def _target(
    *,
    target_id: str = "tgt-1",
    label: str = "Staff Frontend Engineer",
    keywords: list[str] | None = None,
    description: str | None = None,
) -> JobTarget:
    """Minimal valid JobTarget for the embed-text + write tests."""
    now = datetime(2026, 6, 24, tzinfo=UTC)
    return JobTarget(
        id=target_id,
        label=label,
        description=description,
        scoring_profile=ScoringProfile(),
        search_keywords=keywords if keywords is not None else ["React", "TypeScript"],
        is_active=True,
        created_at=now,
        updated_at=now,
    )


# ---------------------------------------------------------------------------
# embed_text_for_target + content_hash (pure)
# ---------------------------------------------------------------------------


def test_embed_text_combines_label_and_keywords() -> None:
    text = embed_text_for_target(_target(label="Staff Frontend Engineer", keywords=["React", "TypeScript"]))
    assert text == "Staff Frontend Engineer. Related roles and skills: React, TypeScript"


def test_embed_text_label_only_when_no_keywords() -> None:
    assert embed_text_for_target(_target(label="Data Scientist", keywords=[])) == "Data Scientist"


def test_embed_text_ignores_description() -> None:
    # The description must NOT leak into the embedded text (#60: it hurt separation).
    with_desc = embed_text_for_target(
        _target(label="PM", keywords=["roadmap"], description="A very long product description")
    )
    assert with_desc == "PM. Related roles and skills: roadmap"
    assert "description" not in with_desc.lower()


def test_embed_text_strips_and_drops_blank_keywords() -> None:
    text = embed_text_for_target(_target(label="  SRE  ", keywords=["  Kubernetes  ", "", "  "]))
    assert text == "SRE. Related roles and skills: Kubernetes"


def test_content_hash_is_stable_and_sensitive() -> None:
    a = content_hash("hello world")
    assert a == content_hash("hello world")
    assert a != content_hash("hello world!")
    assert len(a) == 64  # sha256 hex


# ---------------------------------------------------------------------------
# Fake Supabase (mirrors test_job_embeddings)
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

    def update(self, payload: Any, **_k: Any) -> _FakeQuery:
        self._payload = payload
        return self

    def insert(self, payload: Any, **_k: Any) -> _FakeQuery:
        self._payload = payload
        return self

    def execute(self) -> _FakeResp:
        if self._op == "select":
            return _FakeResp(self._table.existing_rows)
        # write op — record the payload, then return a row shaped like Postgres
        # RETURNING (server-default id + created_at) so cost_log's
        # LLMCallRecord.model_validate succeeds on the llm_costs insert.
        self._table.writes.append(self._payload)
        returned = dict(self._payload) if isinstance(self._payload, dict) else {}
        returned.setdefault("id", "row-0")
        returned.setdefault("created_at", "2026-06-24T00:00:00+00:00")
        return _FakeResp([returned])


class _FakeTable:
    def __init__(self) -> None:
        self.existing_rows: list[dict[str, Any]] = []
        self.writes: list[Any] = []

    def select(self, *_a: Any, **_k: Any) -> _FakeQuery:
        return _FakeQuery(self, "select").select()

    def update(self, payload: Any, **k: Any) -> _FakeQuery:
        return _FakeQuery(self, "update").update(payload, **k)

    def insert(self, payload: Any, **k: Any) -> _FakeQuery:
        return _FakeQuery(self, "insert").insert(payload, **k)


class _FakeSupabase:
    def __init__(self) -> None:
        self.targets = _FakeTable()
        self.llm_costs = _FakeTable()
        self._tables = {"targets": self.targets, "llm_costs": self.llm_costs}

    def table(self, name: str) -> _FakeTable:
        return self._tables[name]


# ---------------------------------------------------------------------------
# upsert_target_embedding
# ---------------------------------------------------------------------------


async def test_new_target_is_embedded_as_query_and_written() -> None:
    sb = _FakeSupabase()
    client = MockEmbeddingsClient()

    status = await upsert_target_embedding(sb, client, _target())  # type: ignore[arg-type]

    assert status == "embedded"
    # Exactly one embed call, on the QUERY side (the key Phase-2 invariant).
    assert len(client.calls) == 1
    assert client.calls[0]["input_type"] == "query"
    assert client.calls[0]["purpose"] == TARGET_EMBED_PURPOSE
    # One row written onto targets with the vector + the text hash.
    assert len(sb.targets.writes) == 1
    row = sb.targets.writes[0]
    assert row["embedding_text_hash"] == content_hash(embed_text_for_target(_target()))
    assert len(row["embedding"]) == 1024
    # Cost row logged under the pre-scan purpose, instance key.
    assert len(sb.llm_costs.writes) == 1
    assert sb.llm_costs.writes[0]["purpose"] == TARGET_EMBED_PURPOSE
    # Cron-authored embedding cost → the SYSTEM principal, not NULL (#88 groundwork).
    assert sb.llm_costs.writes[0]["user_id"] == SYSTEM_USER_ID


async def test_unchanged_target_is_a_cache_hit_no_embed() -> None:
    sb = _FakeSupabase()
    client = MockEmbeddingsClient()
    text = embed_text_for_target(_target())
    sb.targets.existing_rows = [{"embedding_text_hash": content_hash(text)}]

    status = await upsert_target_embedding(sb, client, _target())  # type: ignore[arg-type]

    assert status == "cache_hit"
    assert client.calls == []  # NO embed call
    assert sb.targets.writes == []  # NO write
    assert sb.llm_costs.writes == []  # NO cost row


async def test_changed_target_text_re_embeds() -> None:
    sb = _FakeSupabase()
    client = MockEmbeddingsClient()
    # Stored hash is for the OLD keywords → mismatch → re-embed.
    old = embed_text_for_target(_target(keywords=["Vue"]))
    sb.targets.existing_rows = [{"embedding_text_hash": content_hash(old)}]

    status = await upsert_target_embedding(sb, client, _target(keywords=["React"]))  # type: ignore[arg-type]

    assert status == "embedded"
    assert len(client.calls) == 1
    assert len(sb.targets.writes) == 1


async def test_empty_label_and_keywords_is_skipped_without_embed() -> None:
    sb = _FakeSupabase()
    client = MockEmbeddingsClient()

    status = await upsert_target_embedding(sb, client, _target(label="  ", keywords=[]))  # type: ignore[arg-type]

    assert status == "skipped_empty"
    assert client.calls == []
    assert sb.targets.writes == []


class _BoomClient:
    """Embeds raise — exercises the fail-soft path."""

    async def embed(self, **_k: Any) -> Any:
        raise RuntimeError("voyage down")


async def test_embed_failure_is_swallowed_and_returns_error() -> None:
    sb = _FakeSupabase()

    status = await upsert_target_embedding(sb, _BoomClient(), _target())  # type: ignore[arg-type]

    assert status == "error"  # no raise
    assert sb.targets.writes == []  # nothing written
