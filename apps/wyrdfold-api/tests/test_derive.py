"""Derivation of OptimizedPayload from prose via a mocked LLM."""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.experience import OptimizedDoc, OptimizedPayload, ProseDoc
from app.models.llm import LLMStreamDelta
from app.services.experience.derive import (
    DEFAULT_MODEL,
    DEFAULT_PURPOSE,
    SYSTEM_PROMPT,
    derive_from_prose,
)
from app.services.llm.mock import MockLLMClient


def _sample_payload_json() -> str:
    return json.dumps(
        {
            "summary": "Senior frontend with a decade of shipped work.",
            "roles": [
                {
                    "id": "fightcamp-senior-fe",
                    "company": "FightCamp",
                    "title": "Senior Frontend Engineer",
                    "start": "2021-11",
                    "end": "2024-04",
                    "summary": "Cut mobile load times and drove the PDP rebuild.",
                    "skills": ["React", "Next.js", "TypeScript"],
                    "outcome_refs": ["Cut mobile load times from 10s to 2s"],
                }
            ],
            "skills": [
                {"name": "React", "evidence_refs": [], "years": 8.0},
                {"name": "Next.js", "evidence_refs": [], "years": 5.0},
            ],
            "outcomes": [
                {
                    "description": "Cut mobile load times from 10s to 2s",
                    "metric": "LCP",
                    "value": "2s",
                    "role_ref": "fightcamp-senior-fe",
                }
            ],
        }
    )


async def test_returns_parsed_optimized_payload() -> None:
    client = MockLLMClient(scripted={DEFAULT_PURPOSE: _sample_payload_json()})
    payload, _ = await derive_from_prose(client, prose_text="some prose")
    assert isinstance(payload, OptimizedPayload)
    assert payload.summary is not None
    assert len(payload.roles) == 1
    assert payload.roles[0].company == "FightCamp"
    assert len(payload.skills) == 2
    assert len(payload.outcomes) == 1


def _payload_json_null_role_ref(*, with_reverse_link: bool = True) -> str:
    """Outcome with a null role_ref. When ``with_reverse_link`` the owning
    role lists it in outcome_refs so the backfill can recover the owner;
    otherwise ownership is genuinely unknown and must stay null (#87)."""
    return json.dumps(
        {
            "summary": "s",
            "roles": [
                {
                    "id": "ib-fe",
                    "company": "Internet Brands",
                    "title": "Senior Frontend Engineer",
                    "start": "2018-03",
                    "end": "2019-08",
                    "summary": None,
                    "skills": [],
                    "outcome_refs": (
                        ["Grew the component library from 12 to 30 components"]
                        if with_reverse_link
                        else []
                    ),
                }
            ],
            "skills": [],
            "outcomes": [
                {
                    "description": "Grew the component library from 12 to 30 components",
                    "metric": None,
                    "value": None,
                    "role_ref": None,
                }
            ],
        }
    )


async def test_backfills_null_role_ref_from_reverse_link() -> None:
    client = MockLLMClient(scripted={DEFAULT_PURPOSE: _payload_json_null_role_ref()})
    payload, _ = await derive_from_prose(client, prose_text="prose")
    assert payload.outcomes[0].role_ref == "ib-fe"


async def test_leaves_role_ref_null_when_unresolvable() -> None:
    client = MockLLMClient(
        scripted={DEFAULT_PURPOSE: _payload_json_null_role_ref(with_reverse_link=False)}
    )
    payload, _ = await derive_from_prose(client, prose_text="prose")
    assert payload.outcomes[0].role_ref is None


async def test_passes_default_model_and_purpose() -> None:
    client = MockLLMClient(scripted={DEFAULT_PURPOSE: _sample_payload_json()})
    await derive_from_prose(client, prose_text="prose")
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["model"] == DEFAULT_MODEL
    assert call["purpose"] == DEFAULT_PURPOSE


async def test_enables_system_cache() -> None:
    client = MockLLMClient(scripted={DEFAULT_PURPOSE: _sample_payload_json()})
    await derive_from_prose(client, prose_text="prose")
    assert client.calls[0]["cache_system"] is True


async def test_returns_result_with_positive_cost() -> None:
    client = MockLLMClient(scripted={DEFAULT_PURPOSE: _sample_payload_json()})
    _, result = await derive_from_prose(
        client, prose_text="some reasonably long narrative " * 50
    )
    assert result.cost_usd > 0
    assert result.usage.input_tokens > 0
    assert result.usage.output_tokens > 0


async def test_sends_prose_as_user_message() -> None:
    seen: dict[str, str] = {}

    def responder(latest_user: str, _messages: object) -> str:
        seen["latest"] = latest_user
        return _sample_payload_json()

    client = MockLLMClient(scripted={DEFAULT_PURPOSE: responder})
    await derive_from_prose(client, prose_text="the prose goes here")
    assert seen["latest"] == "the prose goes here"


async def test_model_override_is_respected() -> None:
    client = MockLLMClient(scripted={DEFAULT_PURPOSE: _sample_payload_json()})
    await derive_from_prose(
        client, prose_text="prose", model="claude-haiku-4-5"
    )
    assert client.calls[0]["model"] == "claude-haiku-4-5"


async def test_invalid_json_response_raises() -> None:
    client = MockLLMClient(scripted={DEFAULT_PURPOSE: "not valid json"})
    with pytest.raises(Exception):
        await derive_from_prose(client, prose_text="prose")


def test_system_prompt_mentions_schema_rules() -> None:
    assert "null" in SYSTEM_PROMPT
    assert "Do not invent" in SYSTEM_PROMPT
    assert "Return ONLY the JSON" in SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Endpoint: POST /experience/derive
# ---------------------------------------------------------------------------


class TestDeriveEndpoint:
    @pytest.mark.asyncio
    async def test_skips_llm_when_prose_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Repeat derives on the same prose return the cached doc — no LLM call."""
        from app.routers import experience as exp_router

        prose_doc = ProseDoc(
            id="prose-1",
            user_id=None,
            version=3,
            content="some prose",
            created_at=datetime.now(UTC),
        )
        cached = OptimizedDoc(
            id="opt-1",
            user_id=None,
            prose_doc_id="prose-1",  # matches prose_doc.id
            version=5,
            payload=OptimizedPayload(),
            markdown_view=None,
            source="llm",
            created_at=datetime.now(UTC),
        )

        monkeypatch.setattr(
            "app.services.experience.prose.get_latest", lambda *a, **kw: prose_doc
        )
        monkeypatch.setattr(
            "app.services.experience.optimized.get_latest", lambda *a, **kw: cached
        )

        llm = MockLLMClient(scripted={DEFAULT_PURPOSE: _sample_payload_json()})
        result = await exp_router.derive_optimized(
            request=MagicMock(),
            supabase=MagicMock(),
            llm=llm,
            embeddings=MagicMock(),
        )

        assert result is cached
        assert llm.calls == []  # LLM never invoked

    @pytest.mark.asyncio
    async def test_does_not_skip_when_previous_is_user_edit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """User-edited optimized docs always trigger a fresh LLM derive."""
        from app.routers import experience as exp_router

        prose_doc = ProseDoc(
            id="prose-1",
            user_id=None,
            version=3,
            content="some prose",
            created_at=datetime.now(UTC),
        )
        previous_user_edit = OptimizedDoc(
            id="opt-1",
            user_id=None,
            prose_doc_id="prose-1",
            version=5,
            payload=OptimizedPayload(),
            markdown_view=None,
            source="user_edit",
            created_at=datetime.now(UTC),
        )
        new_doc = OptimizedDoc(
            id="opt-2",
            user_id=None,
            prose_doc_id="prose-1",
            version=6,
            payload=OptimizedPayload(),
            markdown_view=None,
            source="llm",
            created_at=datetime.now(UTC),
        )

        monkeypatch.setattr(
            "app.services.experience.prose.get_latest", lambda *a, **kw: prose_doc
        )
        monkeypatch.setattr(
            "app.services.experience.optimized.get_latest",
            lambda *a, **kw: previous_user_edit,
        )
        monkeypatch.setattr(
            "app.services.experience.optimized.create_version",
            lambda *a, **kw: new_doc,
        )

        async def fake_upsert(*a: object, **kw: object) -> None:
            return None

        monkeypatch.setattr(
            "app.services.experience.chunks.upsert_for_optimized", fake_upsert
        )
        monkeypatch.setattr("app.services.llm.cost_log.record", MagicMock())

        llm = MockLLMClient(scripted={DEFAULT_PURPOSE: _sample_payload_json()})
        result = await exp_router.derive_optimized(
            request=MagicMock(),
            supabase=MagicMock(),
            llm=llm,
            embeddings=MagicMock(),
        )

        assert result is new_doc
        assert len(llm.calls) == 1  # LLM was called
        assert llm.calls[0]["purpose"] == DEFAULT_PURPOSE


# ---------------------------------------------------------------------------
# Endpoint: POST /experience/derive/stream
# ---------------------------------------------------------------------------


def _parse_sse(raw: bytes) -> list[tuple[str, dict[str, Any]]]:
    """Parse an SSE byte stream into (event_name, data_dict) tuples."""
    events: list[tuple[str, dict[str, Any]]] = []
    for frame in raw.decode("utf-8").split("\n\n"):
        if not frame.strip():
            continue
        event_name = ""
        data = ""
        for line in frame.split("\n"):
            if line.startswith("event: "):
                event_name = line[len("event: ") :]
            elif line.startswith("data: "):
                data = line[len("data: ") :]
        events.append((event_name, json.loads(data)))
    return events


async def _drain(streaming_response: object) -> bytes:
    body_iterator = streaming_response.body_iterator
    chunks: list[bytes] = []
    async for chunk in body_iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))
    return b"".join(chunks)


def _request(disconnected: bool = False) -> MagicMock:
    """A stand-in Request whose async ``is_disconnected()`` resolves to
    ``disconnected`` (the SSE handler polls it between deltas, #29 M-r2-3)."""
    req = MagicMock()
    req.is_disconnected = AsyncMock(return_value=disconnected)
    return req


class _MidStreamErrorLLM:
    """Yields one delta, then raises — exercises the SSE terminal-error path."""

    async def stream(self, **_kwargs: object) -> AsyncIterator[object]:
        yield LLMStreamDelta(text='{"summary": "partial')
        raise RuntimeError("provider boom")


class TestDeriveStreamEndpoint:
    @pytest.mark.asyncio
    async def test_404_when_no_prose(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from fastapi import HTTPException

        from app.routers import experience as exp_router

        monkeypatch.setattr(
            "app.services.experience.prose.get_latest", lambda *a, **kw: None
        )

        with pytest.raises(HTTPException) as exc_info:
            await exp_router.derive_optimized_stream(
                request=MagicMock(),
                supabase=MagicMock(),
                llm=MockLLMClient(),
                embeddings=MagicMock(),
            )
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_cached_path_emits_single_done_event(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Skip-when-unchanged: stream emits exactly one done event with cached=true."""
        from app.routers import experience as exp_router

        prose_doc = ProseDoc(
            id="prose-1",
            user_id=None,
            version=3,
            content="some prose",
            created_at=datetime.now(UTC),
        )
        cached = OptimizedDoc(
            id="opt-1",
            user_id=None,
            prose_doc_id="prose-1",
            version=5,
            payload=OptimizedPayload(),
            markdown_view=None,
            source="llm",
            created_at=datetime.now(UTC),
        )

        monkeypatch.setattr(
            "app.services.experience.prose.get_latest", lambda *a, **kw: prose_doc
        )
        monkeypatch.setattr(
            "app.services.experience.optimized.get_latest", lambda *a, **kw: cached
        )

        llm = MockLLMClient(scripted={DEFAULT_PURPOSE: _sample_payload_json()})
        response = await exp_router.derive_optimized_stream(
            request=MagicMock(),
            supabase=MagicMock(),
            llm=llm,
            embeddings=MagicMock(),
        )

        events = _parse_sse(await _drain(response))
        assert len(events) == 1
        name, data = events[0]
        assert name == "done"
        assert data["cached"] is True
        assert data["doc"]["id"] == "opt-1"
        assert llm.calls == []  # streaming bypassed

    @pytest.mark.asyncio
    async def test_full_stream_emits_deltas_then_done(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fresh derive: deltas precede a done event with the persisted doc."""
        from app.routers import experience as exp_router

        prose_doc = ProseDoc(
            id="prose-1",
            user_id=None,
            version=3,
            content="some prose",
            created_at=datetime.now(UTC),
        )
        new_doc = OptimizedDoc(
            id="opt-2",
            user_id=None,
            prose_doc_id="prose-1",
            version=6,
            payload=OptimizedPayload(),
            markdown_view=None,
            source="llm",
            created_at=datetime.now(UTC),
        )

        monkeypatch.setattr(
            "app.services.experience.prose.get_latest", lambda *a, **kw: prose_doc
        )
        monkeypatch.setattr(
            "app.services.experience.optimized.get_latest", lambda *a, **kw: None
        )
        monkeypatch.setattr(
            "app.services.experience.optimized.create_version",
            lambda *a, **kw: new_doc,
        )

        async def fake_upsert(*a: object, **kw: object) -> None:
            return None

        monkeypatch.setattr(
            "app.services.experience.chunks.upsert_for_optimized", fake_upsert
        )
        monkeypatch.setattr("app.services.llm.cost_log.record", MagicMock())

        llm = MockLLMClient(scripted={DEFAULT_PURPOSE: _sample_payload_json()})
        response = await exp_router.derive_optimized_stream(
            request=_request(),
            supabase=MagicMock(),
            llm=llm,
            embeddings=MagicMock(),
        )

        events = _parse_sse(await _drain(response))

        delta_events = [e for e in events if e[0] == "delta"]
        done_events = [e for e in events if e[0] == "done"]
        assert len(delta_events) > 0
        assert len(done_events) == 1
        # Reassembled deltas must match the scripted JSON output.
        joined = "".join(str(e[1]["text"]) for e in delta_events)
        assert joined == _sample_payload_json()
        # Done event carries the persisted doc, not cached.
        _, done_data = done_events[0]
        assert done_data["cached"] is False
        assert done_data["doc"]["id"] == "opt-2"

    @pytest.mark.asyncio
    async def test_aborts_when_client_disconnects_mid_stream(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#29 M-r2-3: a disconnected client stops the stream — no done event,
        nothing persisted — so an abandoned derive stops spending LLM tokens."""
        from app.routers import experience as exp_router

        prose_doc = ProseDoc(
            id="prose-1", user_id=None, version=3, content="some prose",
            created_at=datetime.now(UTC),
        )
        monkeypatch.setattr(
            "app.services.experience.prose.get_latest", lambda *a, **kw: prose_doc
        )
        monkeypatch.setattr(
            "app.services.experience.optimized.get_latest", lambda *a, **kw: None
        )
        create = MagicMock()
        monkeypatch.setattr(
            "app.services.experience.optimized.create_version", create
        )

        llm = MockLLMClient(scripted={DEFAULT_PURPOSE: _sample_payload_json()})
        response = await exp_router.derive_optimized_stream(
            request=_request(disconnected=True),
            supabase=MagicMock(),
            llm=llm,
            embeddings=MagicMock(),
        )
        events = _parse_sse(await _drain(response))
        assert not any(e[0] == "done" for e in events)  # aborted before completion
        create.assert_not_called()  # nothing persisted for the gone client

    @pytest.mark.asyncio
    async def test_emits_error_frame_on_midstream_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#29 M-r2-4: a provider error mid-stream closes the SSE with a
        terminal ``error`` frame, not a truncated stream."""
        from app.routers import experience as exp_router

        prose_doc = ProseDoc(
            id="prose-1", user_id=None, version=3, content="some prose",
            created_at=datetime.now(UTC),
        )
        monkeypatch.setattr(
            "app.services.experience.prose.get_latest", lambda *a, **kw: prose_doc
        )
        monkeypatch.setattr(
            "app.services.experience.optimized.get_latest", lambda *a, **kw: None
        )

        response = await exp_router.derive_optimized_stream(
            request=_request(),
            supabase=MagicMock(),
            llm=_MidStreamErrorLLM(),
            embeddings=MagicMock(),
        )
        events = _parse_sse(await _drain(response))
        assert any(e[0] == "delta" for e in events)  # the partial delta arrived first
        error_events = [e for e in events if e[0] == "error"]
        assert len(error_events) == 1
        assert "failed" in error_events[0][1]["detail"]
        assert not any(e[0] == "done" for e in events)  # never completed
