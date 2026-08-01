"""Orchestrator behavior tests with patched service dependencies.

The orchestrator touches 5 service modules + Supabase. We patch the
service module functions (the natural seam) rather than building a full
fake Supabase. LLM interactions use the real MockLLMClient with scripted
responses so the JSON contract is exercised end-to-end.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.conversation import LLMTurnResponse
from app.models.experience import (
    ConversationTurn,
    OptimizedDoc,
    OptimizedPayload,
    ProseDoc,
    Role,
)
from app.services.conversation import orchestrator
from app.services.experience import turns as turns_mod
from app.services.llm import cost_log as cost_log_mod
from app.services.llm.mock import MockLLMClient


def _prose(content: str, version: int = 1) -> ProseDoc:
    return ProseDoc(
        id=f"prose-{version}",
        user_id=None,
        version=version,
        content=content,
        created_at=datetime.now(UTC),
    )


def _turn(role: str, content: str, skipped: bool = False, idx: int = 1) -> ConversationTurn:
    return ConversationTurn(
        id=f"turn-{idx}",
        user_id=None,
        conversation_type="onboarding",
        turn_index=idx,
        role=role,  # type: ignore[arg-type]
        content=content,
        skipped=skipped,
        prose_doc_id=None,
        metadata={},
        created_at=datetime.now(UTC),
    )


def _llm_response(
    assistant_message: str = "Next question?",
    prose_append: str | None = None,
    done: bool = False,
) -> str:
    return LLMTurnResponse(
        assistant_message=assistant_message,
        prose_append=prose_append,
        done=done,
    ).model_dump_json()


@pytest.fixture
def mock_service_layer(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    """Patch the async service seams the orchestrator now calls (#57 slice 4).

    Since slice 4 the orchestrator runs on the async client via its own inline
    helpers (``_prose_latest`` / ``_prose_create_version``) and awaits
    ``turns.*`` / ``cost_log.record_async``, so these are AsyncMocks. Return a
    dict of mocks so each test can tune return values and assert on call args.
    """
    appended: list[dict[str, Any]] = []

    def fake_append(*_args: Any, **kwargs: Any) -> ConversationTurn:
        appended.append(kwargs)
        return _turn(kwargs["role"], kwargs["content"], kwargs.get("skipped", False))

    mocks: dict[str, MagicMock] = {
        "turns_list": AsyncMock(return_value=[]),
        "turns_append": AsyncMock(side_effect=fake_append),
        "prose_get_latest": AsyncMock(return_value=None),
        "prose_create_version": AsyncMock(
            side_effect=lambda _s, user_id, content: _prose(content, version=2)
        ),
        "cost_log_record": AsyncMock(return_value=None),
        "_appended_turns": appended,  # type: ignore[dict-item]
    }

    monkeypatch.setattr(turns_mod, "list_turns", mocks["turns_list"])
    # handle_turn reads the capped LLM window via list_recent_turns; same mock.
    monkeypatch.setattr(turns_mod, "list_recent_turns", mocks["turns_list"])
    monkeypatch.setattr(turns_mod, "append", mocks["turns_append"])
    monkeypatch.setattr(orchestrator, "_prose_latest", mocks["prose_get_latest"])
    monkeypatch.setattr(orchestrator, "_prose_create_version", mocks["prose_create_version"])
    monkeypatch.setattr(cost_log_mod, "record_async", mocks["cost_log_record"])
    return mocks


async def test_handle_turn_persists_user_then_assistant(
    mock_service_layer: dict[str, Any],
) -> None:
    llm = MockLLMClient(scripted={orchestrator.PURPOSE_TURN_ONBOARDING: _llm_response()})
    await orchestrator.handle_turn(
        MagicMock(),
        llm,
        user_id=None,
        conversation_type="onboarding",
        user_content="I worked at FightCamp",
        skipped=False,
    )
    appended = mock_service_layer["_appended_turns"]
    assert len(appended) == 2
    assert appended[0]["role"] == "user"
    assert appended[0]["content"] == "I worked at FightCamp"
    assert appended[1]["role"] == "assistant"


async def test_handle_turn_appends_prose_when_llm_requests(
    mock_service_layer: dict[str, Any],
) -> None:
    llm = MockLLMClient(
        scripted={
            orchestrator.PURPOSE_TURN_ONBOARDING: _llm_response(
                prose_append="Worked at FightCamp 2021-11 to 2024-04."
            )
        }
    )
    mock_service_layer["prose_get_latest"].return_value = _prose("existing prose.")

    result = await orchestrator.handle_turn(
        MagicMock(),
        llm,
        user_id=None,
        conversation_type="onboarding",
        user_content="...",
        skipped=False,
    )

    assert result.prose_updated is True
    assert result.prose_version == 2
    create_call = mock_service_layer["prose_create_version"].call_args
    assert "existing prose." in create_call.kwargs["content"]
    assert "FightCamp" in create_call.kwargs["content"]


async def test_handle_turn_flags_prose_append_with_unsaid_number_and_name(
    mock_service_layer: dict[str, Any],
) -> None:
    # The LLM invents a number (40%) and a company (Stripe) the user never
    # mentioned, then concatenates them verbatim into the source-of-truth doc
    # on Haiku (#47). The guard must flag both — without dropping the append,
    # which may contain real content — and still surface them to the user.
    llm = MockLLMClient(
        scripted={
            orchestrator.PURPOSE_TURN_ONBOARDING: _llm_response(
                prose_append="At Stripe, grew revenue 40%."
            )
        }
    )
    result = await orchestrator.handle_turn(
        MagicMock(),
        llm,
        user_id=None,
        conversation_type="onboarding",
        user_content="I helped grow the business a lot.",
        skipped=False,
    )
    # The append is still persisted (flag, not drop).
    assert result.prose_updated is True
    joined = " ".join(result.prose_warnings)
    assert "40" in joined
    assert "Stripe" in joined


async def test_handle_turn_does_not_flag_faithful_prose_append(
    mock_service_layer: dict[str, Any],
) -> None:
    # Every number and name in the append was in what the user said this turn:
    # a faithful restatement must produce no warnings.
    llm = MockLLMClient(
        scripted={
            orchestrator.PURPOSE_TURN_ONBOARDING: _llm_response(
                prose_append="Worked at FightCamp and cut load times to 2s."
            )
        }
    )
    result = await orchestrator.handle_turn(
        MagicMock(),
        llm,
        user_id=None,
        conversation_type="onboarding",
        user_content="At FightCamp I cut load times to 2s.",
        skipped=False,
    )
    assert result.prose_updated is True
    assert result.prose_warnings == []


async def test_handle_turn_caches_prose_doc_prefix(
    mock_service_layer: dict[str, Any],
) -> None:
    """The prose-doc context message carries a cache breakpoint over its whole
    content (#73), so system + prose are cached across conversation turns while
    the volatile turns that follow are billed normally."""
    llm = MockLLMClient(
        scripted={orchestrator.PURPOSE_TURN_ONBOARDING: _llm_response(prose_append=None)}
    )
    mock_service_layer["prose_get_latest"].return_value = _prose("existing prose.")

    await orchestrator.handle_turn(
        MagicMock(),
        llm,
        user_id=None,
        conversation_type="onboarding",
        user_content="hi",
        skipped=False,
    )

    prose_msg = llm.calls[-1]["messages"][0]  # type: ignore[index]
    assert prose_msg.content.startswith("[context: current prose doc]\n")
    assert "existing prose." in prose_msg.content
    assert prose_msg.cache_prefix_chars == len(prose_msg.content)


async def test_handle_turn_does_not_append_when_no_prose_content(
    mock_service_layer: dict[str, Any],
) -> None:
    llm = MockLLMClient(
        scripted={orchestrator.PURPOSE_TURN_ONBOARDING: _llm_response(prose_append=None)}
    )
    result = await orchestrator.handle_turn(
        MagicMock(),
        llm,
        user_id=None,
        conversation_type="onboarding",
        user_content="skip",
        skipped=False,
    )
    assert result.prose_updated is False
    mock_service_layer["prose_create_version"].assert_not_called()


async def test_handle_turn_cost_logs_with_correct_purpose_for_update_mode(
    mock_service_layer: dict[str, Any],
) -> None:
    llm = MockLLMClient(scripted={orchestrator.PURPOSE_TURN_UPDATE: _llm_response()})
    await orchestrator.handle_turn(
        MagicMock(),
        llm,
        user_id=None,
        conversation_type="update",
        user_content="shipped the audit tool",
        skipped=False,
    )
    call = mock_service_layer["cost_log_record"].call_args
    assert call.kwargs["purpose"] == orchestrator.PURPOSE_TURN_UPDATE


async def test_handle_turn_annotates_skipped_history(
    mock_service_layer: dict[str, Any],
) -> None:
    mock_service_layer["turns_list"].return_value = [
        _turn("assistant", "what was the team size?"),
        _turn("user", "should-not-appear", skipped=True, idx=2),
    ]

    seen: dict[str, list[Any]] = {}

    def responder(_latest: str, messages: list[Any]) -> str:
        seen["messages"] = messages
        return _llm_response()

    llm = MockLLMClient(scripted={orchestrator.PURPOSE_TURN_ONBOARDING: responder})
    await orchestrator.handle_turn(
        MagicMock(),
        llm,
        user_id=None,
        conversation_type="onboarding",
        user_content="next",
        skipped=False,
    )
    contents = [m.content for m in seen["messages"]]
    assert "[skipped question]" in contents
    assert "should-not-appear" not in contents


async def test_next_probe_returns_default_when_no_optimized_doc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orchestrator, "_optimized_latest", AsyncMock(return_value=None))
    result = await orchestrator.next_probe(MagicMock(), MockLLMClient(), user_id=None)
    assert result.gap is None
    assert "most recent role" in result.question.lower()


async def test_next_probe_phrases_gap_via_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = OptimizedPayload(
        roles=[
            Role(
                id="fc",
                company="FC",
                title="E",
                start="2020-01",
                end="2024-01",
                summary=None,
                skills=[],
                outcome_refs=[],
            )
        ],
    )
    opt_doc = OptimizedDoc(
        id="o-1",
        user_id=None,
        prose_doc_id=None,
        version=1,
        payload=payload,
        markdown_view=None,
        source="llm",
        created_at=datetime.now(UTC),
    )
    monkeypatch.setattr(orchestrator, "_optimized_latest", AsyncMock(return_value=opt_doc))
    monkeypatch.setattr(cost_log_mod, "record_async", AsyncMock())

    llm = MockLLMClient(
        scripted={orchestrator.PURPOSE_PROBE: "What number would you lead with from FC?"}
    )
    result = await orchestrator.next_probe(MagicMock(), llm, user_id=None)
    assert result.gap is not None
    assert result.gap.kind == "role.missing_outcomes"
    assert result.question.startswith("What")


def _reset_supabase_mock() -> MagicMock:
    """A MagicMock whose ``.table(t).delete().eq(...).execute()`` is awaitable
    and yields an empty result set (#57 slice 4 — reset_content is now async)."""
    supabase = MagicMock()
    delete_chain = supabase.table.return_value.delete.return_value
    delete_chain.eq.return_value.execute = AsyncMock(
        return_value=SimpleNamespace(data=[])
    )
    return supabase


async def test_reset_content_deletes_three_tables() -> None:
    supabase = _reset_supabase_mock()

    result = await orchestrator.reset_content(supabase, user_id=None)

    tables_deleted = [c.args[0] for c in supabase.table.call_args_list]
    assert "experience_prose_docs" in tables_deleted
    assert "experience_optimized_docs" in tables_deleted
    assert "experience_conversation_turns" in tables_deleted
    assert result.prose_versions_deleted == 0


async def test_reset_content_keeps_turns_when_include_turns_false() -> None:
    """Deleting just the master document (DELETE /experience/prose) wipes prose
    + the derived optimized doc but preserves conversation turns."""
    supabase = _reset_supabase_mock()

    result = await orchestrator.reset_content(supabase, user_id=None, include_turns=False)

    tables_deleted = [c.args[0] for c in supabase.table.call_args_list]
    assert "experience_prose_docs" in tables_deleted
    assert "experience_optimized_docs" in tables_deleted
    assert "experience_conversation_turns" not in tables_deleted
    assert result.turns_deleted == 0


def test_llm_turn_response_contract_is_parseable() -> None:
    """Sanity check: the JSON shape we ask the LLM for round-trips."""
    raw = _llm_response(
        assistant_message="Team size?",
        prose_append="Worked at Acme.",
        done=False,
    )
    parsed = LLMTurnResponse.model_validate_json(raw)
    assert parsed.assistant_message == "Team size?"
    assert parsed.prose_append == "Worked at Acme."
    assert parsed.done is False
    assert json.loads(raw)["done"] is False


# ---- Conversation-history cap (#29: unbounded LLM context) ----------------


class _RecordingQuery:
    """Stub supabase chain that records order/limit args and returns canned
    rows — enough to pin list_recent_turns' query shape without a live DB."""

    def __init__(self, rows: list[dict[str, Any]], calls: dict[str, Any]) -> None:
        self._rows = rows
        self._calls = calls

    def table(self, _name: str) -> _RecordingQuery:
        return self

    def select(self, *_a: Any, **_kw: Any) -> _RecordingQuery:
        return self

    def order(self, column: str, desc: bool = False) -> _RecordingQuery:
        self._calls["order"] = (column, desc)
        return self

    def limit(self, n: int) -> _RecordingQuery:
        self._calls["limit"] = n
        return self

    def eq(self, *_a: Any, **_kw: Any) -> _RecordingQuery:
        return self

    async def execute(self) -> Any:
        return SimpleNamespace(data=self._rows)


def _turn_row(idx: int) -> dict[str, Any]:
    return {
        "id": f"00000000-0000-4000-8000-{idx:012d}",
        "user_id": "00000000-0000-4000-8000-000000000001",
        "conversation_type": "onboarding",
        "turn_index": idx,
        "role": "user" if idx % 2 == 0 else "assistant",
        "content": f"turn {idx}",
        "skipped": False,
        "prose_doc_id": None,
        "metadata": {},
        "created_at": f"2026-07-02T10:{idx:02d}:00Z",
    }


async def test_list_recent_turns_queries_newest_first_and_returns_ascending() -> None:
    """The window must be the LAST N turns (query desc + limit), handed back
    in conversation order (ascending) for the prompt."""
    calls: dict[str, Any] = {}
    # DB answers newest-first, as the desc query would.
    stub = _RecordingQuery([_turn_row(5), _turn_row(4), _turn_row(3)], calls)

    result = await turns_mod.list_recent_turns(
        cast(Any, stub), user_id="u", conversation_type="onboarding", limit=3
    )

    assert calls["order"] == ("created_at", True), "must fetch newest-first"
    assert calls["limit"] == 3
    assert [t.content for t in result] == ["turn 3", "turn 4", "turn 5"], (
        "window must be re-reversed to conversation order"
    )


async def test_handle_turn_caps_history_to_the_configured_window(
    mock_service_layer: dict[str, Any],
) -> None:
    """handle_turn must request exactly settings.conversation_history_max_turns
    turns — never the old unbounded 1M (#29)."""
    from app.config import settings

    llm = MockLLMClient(scripted={orchestrator.PURPOSE_TURN_ONBOARDING: _llm_response()})
    await orchestrator.handle_turn(
        MagicMock(),
        llm,
        user_id=None,
        conversation_type="onboarding",
        user_content="hello",
        skipped=False,
    )
    call = mock_service_layer["turns_list"].call_args
    assert call.kwargs["limit"] == settings.conversation_history_max_turns
    assert call.kwargs["limit"] <= 1000, "cap must be bounded, not the old 1M"


def test_default_history_cap_is_fifty() -> None:
    from app.config import Settings

    assert Settings(supabase_url="", allowed_hosts="*").conversation_history_max_turns == 50


# ---- Unverified-marker net + ask-first prompt (audit C+B decision) ---------


async def test_flagged_append_persists_under_unverified_marker(
    mock_service_layer: dict[str, Any],
) -> None:
    """A flagged append must persist MARKED, not verbatim: content is kept
    (may be real) but visibly quarantined, and derive is instructed not to
    mint outcomes from marked blocks."""
    from app.constants import UNVERIFIED_MARKER
    from tests.support.llm_edges import UNSUPPORTED_SPECIFICS_APPEND

    llm = MockLLMClient(
        scripted={
            orchestrator.PURPOSE_TURN_ONBOARDING: _llm_response(
                prose_append=UNSUPPORTED_SPECIFICS_APPEND
            )
        }
    )
    result = await orchestrator.handle_turn(
        MagicMock(),
        llm,
        user_id=None,
        conversation_type="onboarding",
        user_content="I helped grow the business a lot.",
        skipped=False,
    )
    assert result.prose_updated is True
    assert result.prose_warnings, "the guard must still flag"
    persisted = mock_service_layer["prose_create_version"].call_args.kwargs["content"]
    assert persisted.startswith(UNVERIFIED_MARKER), "flagged append must be marked"
    assert UNSUPPORTED_SPECIFICS_APPEND in persisted, "content kept, not dropped"


async def test_faithful_append_persists_verbatim_without_marker(
    mock_service_layer: dict[str, Any],
) -> None:
    from app.constants import UNVERIFIED_MARKER

    llm = MockLLMClient(
        scripted={
            orchestrator.PURPOSE_TURN_ONBOARDING: _llm_response(
                prose_append="Worked at FightCamp and cut load times to 2s."
            )
        }
    )
    await orchestrator.handle_turn(
        MagicMock(),
        llm,
        user_id=None,
        conversation_type="onboarding",
        user_content="At FightCamp I cut load times to 2s.",
        skipped=False,
    )
    persisted = mock_service_layer["prose_create_version"].call_args.kwargs["content"]
    assert UNVERIFIED_MARKER not in persisted, "faithful append must stay verbatim"


def test_turn_prompts_carry_the_ask_first_rule() -> None:
    """C (primary control): both turn systems must instruct asking instead of
    recording unstated specifics."""
    from app.services.conversation.prompts import ONBOARDING_SYSTEM, UPDATE_SYSTEM

    for system in (ONBOARDING_SYSTEM, UPDATE_SYSTEM):
        assert "ask for it in assistant_message instead" in system


def test_derive_prompt_pins_the_exact_marker() -> None:
    """Producer/consumer drift guard: derive's instruction must reference the
    EXACT marker the orchestrator stamps — if either side changes, this fails."""
    from app.constants import UNVERIFIED_MARKER
    from app.services.experience.derive import SYSTEM_PROMPT

    assert UNVERIFIED_MARKER in SYSTEM_PROMPT
