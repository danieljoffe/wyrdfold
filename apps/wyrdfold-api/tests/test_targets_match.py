"""Deterministic tests for target matching (#553 / audit F0-E).

Covers normalize/exact/fuzzy/RPC-fallback paths in find_matching_target,
and the user-already-linked exclusion in suggest_and_match.

#57 PR-G2b: match runs on the pooled async service client — the DB reads are
``await``ed, so ``.execute()`` is stubbed with ``AsyncMock`` and
``find_matching_target`` is awaited. The any-status membership read is the
module-inline ``_user_target_ids`` (async), monkeypatched here.
"""

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.experience import OptimizedPayload
from app.services.llm.mock import MockLLMClient
from app.services.targets import match as match_module
from app.services.targets.match import (
    _normalize_label,
    find_matching_target,
    suggest_and_match,
)
from app.services.targets.suggest import DEFAULT_PURPOSE


def _target_row(*, id: str = "t1", label: str = "Senior Frontend Engineer") -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    return {
        "id": id,
        "label": label,
        "description": None,
        "normalized_label": label.lower().strip(),
        "scoring_profile": {},
        "search_keywords": [],
        "activation_status": "idle",
        "profile_version": 1,
        "app_active": False,
        "created_at": now,
        "updated_at": now,
    }


def _scripted_suggestions() -> str:
    return json.dumps(
        {
            "suggestions": [
                {
                    "label": "Senior Frontend Engineer",
                    "description": "Existing match.",
                    "core_skills": ["React"],
                },
                {
                    "label": "Staff DevOps Engineer",
                    "description": "Brand new direction.",
                    "core_skills": ["Kubernetes"],
                },
            ]
        }
    )


# ---- _normalize_label -------------------------------------------------------


def test_normalize_label_lowercases_and_trims() -> None:
    assert _normalize_label("  Senior Frontend Engineer  ") == "senior frontend engineer"


def test_normalize_label_collapses_whitespace() -> None:
    assert _normalize_label("Senior\t\tFrontend\n  Engineer") == "senior frontend engineer"


# ---- find_matching_target ---------------------------------------------------


@pytest.mark.asyncio
async def test_find_matching_target_exact_match() -> None:
    supabase = MagicMock()
    supabase.table.return_value.select.return_value.eq.return_value.limit.return_value.execute = (
        AsyncMock(
            return_value=MagicMock(
                data=[_target_row(id="t1", label="Senior Frontend Engineer")]
            )
        )
    )

    result = await find_matching_target(supabase, "  senior frontend engineer  ")
    assert result is not None
    assert result.id == "t1"


@pytest.mark.asyncio
async def test_find_matching_target_no_match_returns_none() -> None:
    supabase = MagicMock()
    supabase.table.return_value.select.return_value.eq.return_value.limit.return_value.execute = (
        AsyncMock(return_value=MagicMock(data=[]))
    )
    supabase.rpc.return_value.execute = AsyncMock(return_value=MagicMock(data=[]))

    assert await find_matching_target(supabase, "Some Unique Role") is None


@pytest.mark.asyncio
async def test_find_matching_target_falls_back_to_rpc() -> None:
    supabase = MagicMock()
    supabase.table.return_value.select.return_value.eq.return_value.limit.return_value.execute = (
        AsyncMock(return_value=MagicMock(data=[]))
    )
    supabase.rpc.return_value.execute = AsyncMock(
        return_value=MagicMock(data=[_target_row(id="t-fuzzy", label="Sr. Frontend Eng")])
    )

    result = await find_matching_target(supabase, "Senior Frontend Engineer")
    assert result is not None
    assert result.id == "t-fuzzy"


@pytest.mark.asyncio
async def test_find_matching_target_swallows_rpc_failure() -> None:
    """If the trgm RPC isn't installed, the function logs and returns None."""
    supabase = MagicMock()
    supabase.table.return_value.select.return_value.eq.return_value.limit.return_value.execute = (
        AsyncMock(return_value=MagicMock(data=[]))
    )
    supabase.rpc.return_value.execute = AsyncMock(side_effect=RuntimeError("RPC missing"))

    assert await find_matching_target(supabase, "Some Role") is None


# ---- suggest_and_match ------------------------------------------------------


@pytest.mark.asyncio
async def test_suggest_and_match_excludes_users_existing_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A suggestion that exact-matches a target the user already has is dropped;
    a brand-new suggestion is kept with is_new=True."""
    supabase = MagicMock()

    # Sequence the two find_matching_target calls: first hits t1, second misses.
    supabase.table.return_value.select.return_value.eq.return_value.limit.return_value.execute = (
        AsyncMock(
            side_effect=[
                MagicMock(data=[_target_row(id="t1", label="Senior Frontend Engineer")]),
                MagicMock(data=[]),
            ]
        )
    )
    supabase.rpc.return_value.execute = AsyncMock(return_value=MagicMock(data=[]))

    # Bypass the supabase chain for the membership + label reads — not the
    # SUT here. Labels empty so the ID-exclusion path is tested in
    # isolation from the containment check (covered separately below).
    monkeypatch.setattr(match_module, "_user_target_ids", AsyncMock(return_value={"t1"}))
    monkeypatch.setattr(match_module, "_user_target_labels", AsyncMock(return_value=set()))

    llm = MockLLMClient(scripted={DEFAULT_PURPOSE: _scripted_suggestions()})

    matched, _ = await suggest_and_match(
        supabase, llm, payload=OptimizedPayload(), user_id="user-1"
    )

    assert len(matched.matches) == 1
    assert matched.matches[0].suggestion.label == "Staff DevOps Engineer"
    assert matched.matches[0].is_new is True
    assert matched.matches[0].matched_target is None


@pytest.mark.asyncio
async def test_suggest_and_match_marks_unmatched_suggestions_as_new(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supabase = MagicMock()
    supabase.table.return_value.select.return_value.eq.return_value.limit.return_value.execute = (
        AsyncMock(return_value=MagicMock(data=[]))
    )
    supabase.rpc.return_value.execute = AsyncMock(return_value=MagicMock(data=[]))

    monkeypatch.setattr(match_module, "_user_target_ids", AsyncMock(return_value=set()))

    llm = MockLLMClient(scripted={DEFAULT_PURPOSE: _scripted_suggestions()})

    matched, _ = await suggest_and_match(
        supabase, llm, payload=OptimizedPayload(), user_id="user-1"
    )

    assert len(matched.matches) == 2
    assert all(m.is_new for m in matched.matches)
    assert all(m.matched_target is None for m in matched.matches)


# ---- _near_duplicate_of_existing (sweep 2026-08-14 A4) ----------------------


def test_near_duplicate_catches_word_extension_labels() -> None:
    """The prod repro: 'Founding Engineer / Head of Engineering' offered while
    the user already follows 'Founding Engineer' — trigram similarity dips
    below 0.7 (extra words dilute), containment must catch it."""
    assert match_module._near_duplicate_of_existing(
        "founding engineer / head of engineering", {"founding engineer"}
    )
    # Symmetric: existing label is the longer one.
    assert match_module._near_duplicate_of_existing(
        "founding engineer", {"founding engineer / head of engineering"}
    )


def test_near_duplicate_requires_word_boundaries() -> None:
    # "end engineer" appears inside "backend engineer" only mid-word — the
    # guard must NOT fire (this would be a specialization collision).
    assert not match_module._near_duplicate_of_existing(
        "backend engineer", {"end engineer"}
    )


def test_near_duplicate_ignores_single_word_labels() -> None:
    # A one-word label ("engineer") sits inside almost anything — dropping
    # on it would erase whole categories of suggestions.
    assert not match_module._near_duplicate_of_existing(
        "platform engineer", {"engineer"}
    )


def test_near_duplicate_unrelated_labels_pass() -> None:
    assert not match_module._near_duplicate_of_existing(
        "senior data scientist", {"founding engineer", "staff frontend engineer"}
    )


@pytest.mark.asyncio
async def test_suggest_and_match_drops_near_duplicates_of_own_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end through suggest_and_match: a word-extension near-dup of a
    label the user follows is dropped before any catalog lookup; the other
    suggestion flows through normally."""
    supabase = MagicMock()
    # Only ONE catalog lookup should happen (for the non-dup suggestion) —
    # a single-response mock would fail loudly if the dropped suggestion
    # were looked up too.
    supabase.table.return_value.select.return_value.eq.return_value.limit.return_value.execute = (
        AsyncMock(side_effect=[MagicMock(data=[])])
    )
    supabase.rpc.return_value.execute = AsyncMock(return_value=MagicMock(data=[]))

    monkeypatch.setattr(match_module, "_user_target_ids", AsyncMock(return_value={"t9"}))
    monkeypatch.setattr(
        match_module,
        "_user_target_labels",
        AsyncMock(return_value={"senior frontend engineer"}),
    )

    llm = MockLLMClient(scripted={DEFAULT_PURPOSE: _scripted_suggestions()})

    matched, _ = await suggest_and_match(
        supabase, llm, payload=OptimizedPayload(), user_id="user-1"
    )

    # "Senior Frontend Engineer" is a (here: exact) containment hit against
    # the user's own label set → dropped; "Staff DevOps Engineer" survives.
    assert [m.suggestion.label for m in matched.matches] == ["Staff DevOps Engineer"]
    assert matched.matches[0].is_new is True
